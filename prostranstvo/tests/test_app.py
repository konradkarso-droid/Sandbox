"""Песочница «из коробки» — проверки §7 спецификации (docs/pesochnitsa_specifikatsiya.md).

Облако здесь — заглушка: местный HTTP-сервер, говорящий по протоколам
OpenAI и Anthropic. Живая облачная модель тестами не трогается.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from importlib.metadata import version
from pathlib import Path

import pytest

from app import seal as sealmod
from app.cloud import Cloud, ask
from app.runner import IDLE, INSTALL_CHECK, Refusal, Run, agent_module, net_plan, settings_from

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sealmod.available() is not None, reason="печать сети — только Linux x86_64/aarch64")

SCRIPT = ["СПРАВКА модель", "ЧИТАЙ ЗАДАНИЯ", "РЕШИ 1", "ВЫЗОВ 40 0 0 16", "СТОП"]


def _child(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                          env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=60)


# ---------------------------------------------------------------- печать

FAMILIES = r"""
import errno, socket, sys, ctypes, platform
from app.seal import seal, _ARCH
seal(allow_unix=sys.argv[1] == "1")
out = {}
for name in ("AF_INET", "AF_INET6", "AF_UNIX", "AF_NETLINK", "AF_PACKET"):
    fam = getattr(socket, name)
    try:
        socket.socket(fam, socket.SOCK_RAW if name == "AF_PACKET" else socket.SOCK_DGRAM).close()
        out[name] = "создан"
    except OSError as e:
        out[name] = errno.errorcode[e.errno]
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall(_ARCH[platform.machine()][2], 1, None)
out["io_uring"] = errno.errorcode[ctypes.get_errno()]
a, b = socket.socketpair()
out["socketpair"] = "можно"
print(__import__("json").dumps(out))
"""


@pytest.mark.parametrize("unix", ["0", "1"])
def test_seal_families(unix):
    r = subprocess.run([sys.executable, "-c", FAMILIES, unix], capture_output=True, text=True, cwd=ROOT,
                       env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=60)
    out = json.loads(r.stdout)
    for fam in ("AF_INET", "AF_INET6", "AF_NETLINK", "AF_PACKET"):
        assert out[fam] == "EPERM", (fam, out)
    assert out["io_uring"] == "EPERM"
    assert out["AF_UNIX"] == ("создан" if unix == "1" else "EPERM")
    assert out["socketpair"] == "можно"


def test_seal_check_passes_probe():
    assert sealmod.check(False) is None
    assert sealmod.check(True) is None


@pytest.mark.parametrize("allow_unix", [False, True])
def test_network_seal_is_inherited_after_exec(allow_unix):
    probe = '''
import socket
import sys
from sandbox.model9 import probe_net

why = probe_net()
assert why is None, why
try:
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
except PermissionError:
    assert sys.argv[1] == "False"
else:
    assert sys.argv[1] == "True"
'''
    result = _child(
        "import os, sys\n"
        "from app.seal import seal\n"
        f"seal(allow_unix={allow_unix!r})\n"
        f"os.execv(sys.executable, [sys.executable, '-c', {probe!r}, {str(allow_unix)!r}])\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_broken_seal_fails_probe():
    """Порча печати: фильтр, который смотрит не тот вызов, — сокет создаётся,
    и строгая проба §18 это ловит: процесс модели под такой «печатью» не
    запускается."""
    code = r"""
import platform
from app import seal
arch, nr_socket, nr_uring, nr_seccomp = seal._ARCH[platform.machine()]
seal._ARCH[platform.machine()] = (arch, 999, nr_uring, nr_seccomp)   # «socket» — несуществующий номер
seal.seal(allow_unix=False)
from sandbox.model9 import start_links
try:
    start_links({"mode": ["--stub"], "net": "probe"})
    print("запущено")
except RuntimeError as e:
    print("отказ:", e)
"""
    r = _child(code)
    assert r.stdout.startswith("отказ:") and "сеть не отнята" in r.stdout, r.stdout + r.stderr


def test_no_seal_no_unshare_refuses_closed(monkeypatch):
    import app.runner as runner
    monkeypatch.setattr(runner.sealmod, "check", lambda allow_unix: "нет печати")
    monkeypatch.setattr(runner, "can_unshare", lambda: False)
    assert net_plan("сомкнутый")["error"] == {"code": "closed_impossible", "why": "нет печати"}
    plan = net_plan("разомкнутый")
    assert plan["seal"] is None and plan["note"]["code"] == "no_seal_open"


# ---------------------------------------------------------------- облако-заглушка

class FakeCloud:
    """Местный сервер облака. Ответ — ход из SCRIPT по числу строк истории
    в вопросе, как у serve.py --script; mode: «ok», «500», «sleep».
    cut — номера строк истории, на которых ответ «обрывается на пределе»:
    пустой текст и признак обрыва, как у рассуждающей модели."""

    def __init__(self, mode: str = "ok", kind: str = "openai", script: list[str] | None = None,
                 cut: frozenset = frozenset()):
        self.mode, self.kind, self.script, self.cut = mode, kind, script or SCRIPT, cut
        self.got: list[dict] = []
        self.headers: list[dict] = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"data": [{"id": "заглушка-1"}, {"id": "заглушка-2"}]}).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["content-length"])))
                fake.got.append(req)
                fake.headers.append(dict(self.headers))
                if fake.mode == "500":
                    self.send_response(500)
                    self.end_headers()
                    return
                if fake.mode == "sleep":
                    time.sleep(30)
                q = req["messages"][0]["content"]
                k = sum(1 for line in q.split("\n") if line.startswith("> "))
                a = fake.script[k] if k < len(fake.script) else "СТОП"
                cut = k in fake.cut
                if cut:
                    a = ""
                if fake.kind == "anthropic":
                    out = {"content": [{"type": "text", "text": a}], "usage": {"input_tokens": 7, "output_tokens": 2},
                           "stop_reason": "max_tokens" if cut else "end_turn"}
                else:
                    out = {"choices": [{"message": {"content": a}, "finish_reason": "length" if cut else "stop"}],
                           "usage": {"prompt_tokens": 7, "completion_tokens": 2}}
                body = json.dumps(out, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(scope="module")
def agent():
    return agent_module()[0]


def _run(s, agent, tmp_path, stop_after: float | None = None) -> Run:
    run = Run(s, agent, runs_dir=tmp_path)
    run.start()
    t0 = time.time()
    while not run.finished.is_set():
        if stop_after is not None and time.time() - t0 > stop_after:
            run.stop()
            stop_after = None
        time.sleep(0.1)
        assert time.time() - t0 < 300, "прогон не кончился за 5 минут"
    run.join(timeout=30)
    return run


def _journal(run: Run) -> list[dict]:
    p = run.dir / "ispolnitel" / "run00001" / "journal5.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _words():
    return "Реши задачи."


# ---------------------------------------------------------------- прогоны

def test_install_check_run_laws_hold(agent, tmp_path):
    run = _run(settings_from({"contour": "сомкнутый", "model": INSTALL_CHECK, "words": _words()}), agent, tmp_path)
    snap = run.snapshot()
    res = snap["result"]
    assert snap["state"] == "готово", snap["notes"]
    assert res["broken"] == [] and res["audit"] == [] and res["seal"] == "сомкнутый"
    assert res["laws_total"] == 40 and res["laws_holds"] >= 35
    t1 = snap["tally"][1]
    # остаток на экране считается правилом закона И1р — и совпадает с исполнителем
    assert t1["left"] == res["contours"][1]["budget_left"]
    assert t1["refusals"].get("-1") and t1["refusals"].get("-2")       # попытки выхода — отказы ворот
    assert t1["unparsed"] == 1
    assert snap["tally"][2]["questions"] == 0                            # второй молчит
    result = json.loads((run.dir / "itog.json").read_text(encoding="utf-8"))
    assert result["runtime"]["wasmtime"] == version("wasmtime")


def test_open_contour_openai(agent, tmp_path):
    fake = FakeCloud("ok", "openai")
    try:
        s = settings_from({"contour": "разомкнутый", "words": _words(),
                           "cloud": {"kind": "openai", "base": fake.base, "model": "заглушка-1", "key": "sk-METKA-77q"}})
        run = _run(s, agent, tmp_path)
    finally:
        fake.close()
    snap = run.snapshot()
    res = snap["result"]
    assert snap["state"] == "готово", snap["notes"]
    assert res["broken"] == [] and res["audit"] == [] and res["seal"] == "разомкнутый"
    recs = _journal(run)
    qs = [r for r in recs if r.get("kind") == "действие" and r["signature"][0] == "вопрос модели"]
    ans = [r for r in recs if r.get("kind") == "действие" and r["signature"][0] == "ответ модели"]
    assert len(qs) == len(fake.got) == len(SCRIPT)
    # что ушло облаку — ровно вопрос из журнала; что пришло — ровно ответ
    assert [q["question"] for q in qs] == [g["messages"][0]["content"] for g in fake.got]
    assert [a["answer"] for a in ans] == SCRIPT
    assert all(g["temperature"] == 0 and len(g["messages"]) == 1 for g in fake.got)
    assert {k.lower(): v for k, v in fake.headers[0].items()}["authorization"] == "Bearer sk-METKA-77q"
    assert res["tokens"] == {"in": 7 * len(SCRIPT), "out": 2 * len(SCRIPT)}
    # ни один ответ не обрезан — подсказки об обрезке нет ни у одного хода
    answers = [e for e in run.events if e["type"] == "answer"]
    assert len(answers) == len(SCRIPT) and not any(e["cut"] for e in answers)
    # ключа нет ни в одном файле прогона
    for p in run.dir.rglob("*"):
        if p.is_file():
            assert "sk-METKA-77q".encode() not in p.read_bytes(), p
    assert "key" not in (snap["settings"]["cloud"] or {})


def test_open_contour_anthropic_protocol():
    fake = FakeCloud("ok", "anthropic")
    try:
        c = Cloud("anthropic", fake.base, "заглушка", "k-1", 64)
        text, used = ask(c, "Последние ходы и итоги:\n> А\n", time.monotonic() + 10)
    finally:
        fake.close()
    assert text == SCRIPT[1] and used == {"in": 7, "out": 2, "cut": False}
    h = {k.lower(): v for k, v in fake.headers[0].items()}
    assert h["x-api-key"] == "k-1" and h["anthropic-version"] == "2023-06-01" and "authorization" not in h
    assert fake.got[0]["max_tokens"] == 64 and fake.got[0]["temperature"] == 0


def test_cut_answer_marked_on_its_move(agent, tmp_path):
    """Облако оборвало ответ на пределе: у этого хода — подсказка, у
    остальных — нет; сам ответ модели не тронут (пустой и остаётся)."""
    fake = FakeCloud("ok", "openai", cut=frozenset({2}))
    try:
        s = settings_from({"contour": "разомкнутый", "words": _words(),
                           "cloud": {"kind": "openai", "base": fake.base, "model": "м", "key": ""}})
        run = _run(s, agent, tmp_path)
    finally:
        fake.close()
    snap = run.snapshot()
    assert snap["state"] == "готово", snap["notes"]
    answers = [e for e in run.events if e["type"] == "answer"]
    cut = [e for e in answers if e["cut"]]
    assert len(cut) == 1 and cut[0]["n"] == 3 and cut[0]["a"] == "" and cut[0]["move"] == "не разобран"
    assert len(answers) > 1
    assert snap["result"]["broken"] == []


def test_submit_refusal_carries_reason(agent, tmp_path):
    """Повторная сдача уже принятой задачи: экран получает причину отказа
    («уже принято»), а не одно слово «отказ» — иначе он показал бы
    «ответ неверен», которого не было."""
    fake = FakeCloud("ok", "openai", script=["ЧИТАЙ ЗАДАНИЯ", "РЕШИ 1", "РЕШИ 1", "СТОП"])
    try:
        s = settings_from({"contour": "разомкнутый", "words": _words(),
                           "cloud": {"kind": "openai", "base": fake.base, "model": "м", "key": ""}})
        run = _run(s, agent, tmp_path)
    finally:
        fake.close()
    assert run.snapshot()["state"] == "готово"
    refused = [e for e in run.events if e["type"] == "effect" and e["kind"] == "отказ" and "task" in e]
    assert [(e["n"], e["why"]) for e in refused] == [(3, "уже принято")]
    assert run.tally[1].accepted == 1


def test_cut_flag_both_protocols():
    for kind in ("openai", "anthropic"):
        fake = FakeCloud("ok", kind, cut=frozenset({0}))
        try:
            c = Cloud(kind, fake.base, "м", "k", 64)
            cut = ask(c, "вопрос без истории", time.monotonic() + 10)
            whole = ask(c, "> одна строка истории\n", time.monotonic() + 10)
        finally:
            fake.close()
        assert cut == ("", {"in": 7, "out": 2, "cut": True}), kind
        assert whole == (SCRIPT[1], {"in": 7, "out": 2, "cut": False}), kind


def test_cloud_failure_is_silence(agent, tmp_path):
    fake = FakeCloud("500")
    try:
        s = settings_from({"contour": "разомкнутый", "words": _words(), "timeout": 6,
                           "cloud": {"kind": "openai", "base": fake.base, "model": "м", "key": ""}})
        run = _run(s, agent, tmp_path)
    finally:
        fake.close()
    snap = run.snapshot()
    assert snap["state"] == "готово", snap["notes"]
    assert snap["result"]["broken"] == []
    assert any(n["code"] == "cloud_failed" and "(500)" in n["why"] for n in snap["notes"])
    recs = _journal(run)
    ans = [r for r in recs if r.get("kind") == "действие" and r["signature"][0] == "ответ модели"]
    assert ans and all(a["answer"] is None and a["code"] == -7 for a in ans)
    assert len(ans) == 1                         # после молчания вопросы больше не уходят
    assert snap["tally"][1]["silent"] == 1


def test_stop_mid_question(agent, tmp_path):
    fake = FakeCloud("sleep")
    try:
        s = settings_from({"contour": "разомкнутый", "words": _words(), "timeout": 60,
                           "cloud": {"kind": "openai", "base": fake.base, "model": "м", "key": ""}})
        t0 = time.time()
        run = _run(s, agent, tmp_path, stop_after=3.0)
        took = time.time() - t0
    finally:
        fake.close()
    assert run.snapshot()["state"] == "остановлено"
    assert took < 15, took                        # не ждали ответа облака (30 с) и срока (60 с)
    assert not run.driver.proc.is_alive()
    time.sleep(1)
    alive = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                cmd = (d / "cmdline").read_bytes()
            except OSError:
                continue
            if b"--relay" in cmd and str(run.dir).encode() in cmd or b"svyaz.sock" in cmd and b"--relay" in cmd:
                stat = (d / "stat").read_text()
                if ") Z " not in stat:
                    alive.append(cmd)
    assert not alive, alive
    assert (run.dir / "ispolnitel" / "run00001" / "journal5.jsonl").exists()


# ---------------------------------------------------------------- настройки и экран

@pytest.mark.parametrize("raw, why", [
    ({"contour": "наружу", "words": "x"}, "локальная или облачная"),
    ({"contour": "сомкнутый", "words": ""}, "задание"),
    ({"contour": "сомкнутый", "words": "я" * 5000}, "4096"),
    ({"contour": "сомкнутый", "words": "x"}, "выбери файл"),
    ({"contour": "сомкнутый", "words": "x", "model": "нет_такой.gguf"}, "нет такого файла"),
    ({"contour": "сомкнутый", "words": "x", "model": "/etc/passwd"}, ".gguf"),
    ({"contour": "разомкнутый", "words": "x", "cloud": {"kind": "openai", "base": "http://evil.example", "model": "m"}},
     "https"),
    ({"contour": "сомкнутый", "words": "x", "answer": 4096}, "предел ответа"),
    ({"contour": "разомкнутый", "words": "x", "cloud": {"kind": "openai", "base": "https://a.b", "model": "m",
                                                        "key": "ключ по-русски"}}, "латинские"),
])
def test_settings_refused(raw, why):
    with pytest.raises(Refusal, match=why) as e:
        settings_from(raw)
    assert e.value.en and e.value.en != e.value.ru           # отказ есть и по-английски


def test_texts_cover_every_law(agent, tmp_path):
    """Каждый закон, который проверяет наблюдатель, стоит в группе экрана и
    пересказан на обоих языках; у каждого сценария — оба языка, и слова
    влезают в то, что агент читает."""
    from app import texts
    from app.runner import WORDS_AGENT_READS
    run = _run(settings_from({"contour": "сомкнутый", "model": INSTALL_CHECK, "words": _words()}), agent, tmp_path)
    keys = [x["key"] for g in run.snapshot()["result"]["groups"] for x in g["items"]]
    grouped = {k for g in texts.GROUPS for k in g["keys"]}
    assert set(keys) <= grouped and len(keys) == 40
    assert all(k in texts.LAWS and all(texts.LAWS[k]) for k in grouped)
    for sc in texts.SCENARIOS:
        for lang in ("ru", "en"):
            assert sc[lang]["title"] and sc[lang]["about"]
            assert len(sc[lang]["words"].encode()) <= WORDS_AGENT_READS, (sc["id"], lang)


def test_download_target():
    from app.server import App
    url, name = App.download_target("https://huggingface.co/a/b/blob/main/x-Q4.gguf")
    assert url == "https://huggingface.co/a/b/resolve/main/x-Q4.gguf" and name == "x-Q4.gguf"
    for bad in ("http://huggingface.co/a.gguf", "https://example.com/a.bin", "https://example.com/.gguf"):
        with pytest.raises(Refusal):
            App.download_target(bad)


def test_screen_token_and_host(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "КЛЮЧ-ИЗ-ОКРУЖЕНИЯ")
    from app.server import App, make_handler
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = App(port)
    assert "OPENROUTER_API_KEY" not in os.environ          # исполнитель ключа не унаследует
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path, headers=None):
        try:
            with opener.open(urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    try:
        assert get("/")[0] == 403
        assert get("/api/info")[0] == 403
        assert get(f"/?t={app.token}", {"Host": f"evil.example:{port}"})[0] == 403
        code, page = get(f"/?t={app.token}")
        assert code == 200 and app.token.encode() in page
        code, info = get("/api/info", {"X-Token": app.token})
        info = json.loads(info)
        assert code == 200 and info["seal"]["сомкнутый"] is None
        assert next(p for p in info["presets"] if p["id"] == "openrouter")["has_env_key"] is True
        assert "КЛЮЧ-ИЗ-ОКРУЖЕНИЯ".encode() not in json.dumps(info, ensure_ascii=False).encode()
    finally:
        srv.shutdown()
        srv.server_close()


def test_relay_passes_bytes(tmp_path):
    """Передатчик не трогает байты и выходит, когда связь закрыла канал."""
    path = str(tmp_path / "s.sock")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)
    p = subprocess.Popen([sys.executable, "-I", str(ROOT / "model9" / "serve.py"), "--relay", path],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    conn, _ = srv.accept()
    line = json.dumps({"q": base64.b64encode("вопрос\x00\xff".encode()).decode(), "max": 9}).encode() + b"\n"
    p.stdin.write(line)
    p.stdin.flush()
    f = conn.makefile("rwb")
    assert f.readline() == line
    back = json.dumps({"a": base64.b64encode(b"\xd0\x9e\xd0").decode()}).encode() + b"\n"
    f.write(back)
    f.flush()
    assert p.stdout.readline() == back
    f.close()
    conn.close()
    p.stdin.write(line)
    p.stdin.flush()
    try:
        assert p.stdout.readline() == b""
        assert p.wait(timeout=10) == 0
    finally:
        p.kill()
    srv.close()


def test_idle_agent_is_silent():
    assert IDLE.count("(func") == 1 and "import" not in IDLE


# ---------------------------------------------------------------- агент на двух языках и свой агент

def _cloud(fake, **kw):
    return settings_from({"contour": "разомкнутый", "words": _words(),
                          "cloud": {"kind": "openai", "base": fake.base, "model": "м", "key": ""}, **kw})


def _results(run) -> list[str]:
    """Итоги ходов так, как их записал агент в историю своих вопросов."""
    qs = [r for r in _journal(run) if r.get("kind") == "действие" and r["signature"][0] == "вопрос модели"]
    last = qs[-1]["question"]
    return [x.rsplit(" => ", 1)[1] for x in last.split("\n") if x.startswith("> ")]


def test_english_agent_english_moves(tmp_path):
    script = ["LOOKUP модель", "READ TASKS", "SOLVE 1", "CALL 40 0 0 16", "Move: STOP"]
    fake = FakeCloud("ok", script=script)
    try:
        run = _run(_cloud(fake, agent_lang="en"), agent_module("en")[0], tmp_path)
    finally:
        fake.close()
    snap = run.snapshot()
    assert snap["state"] == "готово" and snap["result"]["broken"] == []
    assert fake.got[0]["messages"][0]["content"].startswith("You control an agent in a sandbox.")
    res = _results(run)
    assert res[0].startswith("answer ") and res[1].startswith("tasks:") and "accepted" in res[2]
    assert res[3] == "returned -1"
    assert snap["tally"][1]["questions"] == len(script) and snap["tally"][1]["unparsed"] == 0


def test_moves_understood_in_both_languages(agent, tmp_path):
    """Русский вид понимает английский ход и отвечает по-русски; английский —
    наоборот."""
    fake = FakeCloud("ok", script=["READ TASKS", "СТОП"])
    try:
        run_ru = _run(_cloud(fake), agent, tmp_path / "ru")
    finally:
        fake.close()
    fake = FakeCloud("ok", script=["ЧИТАЙ ЗАДАНИЯ", "STOP"])
    try:
        run_en = _run(_cloud(fake, agent_lang="en"), agent_module("en")[0], tmp_path / "en")
    finally:
        fake.close()
    assert _results(run_ru)[0].startswith("задачи:")
    assert _results(run_en)[0].startswith("tasks:")


def test_move_of_english():
    from experiments.pilot9 import move_of
    assert move_of("READ TASKS") == "ЧИТАЙ ЗАДАНИЯ" and move_of("read task 2") == "ЧИТАЙ ЗАДАНИЕ"
    assert move_of("Move: CALL 3 1") == "ВЫЗОВ" and move_of("ХОД: СТОП") == "СТОП" and move_of("hello") is None


def test_own_agent_runs(tmp_path, monkeypatch):
    import app.runner as runner
    monkeypatch.setattr(runner, "AGENTS", tmp_path / "agents")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "ask_once.wat").write_bytes((ROOT / "experiments/agents_own/ask_once.wat").read_bytes())
    fake = FakeCloud("ok", script=["это ответ модели"])
    try:
        s = _cloud(fake, agent="ask_once.wat")
        run = _run(s, runner.own_agent_source("ask_once.wat"), tmp_path)
    finally:
        fake.close()
    snap = run.snapshot()
    assert snap["state"] == "готово" and snap["result"]["broken"] == [] and snap["result"]["audit"] == []
    assert fake.got[0]["messages"][0]["content"] == _words()          # вопрос — слова человека, как есть
    assert snap["tally"][1]["refusals"] == {"-1": 1}                   # вызов без права — отказ, оплачен
    snaps = [r for r in _journal(run) if r.get("kind") == "действие" and r["signature"][0] == "запись"]
    assert snaps                                                       # ответ записан в снимок


@pytest.mark.parametrize("wat, why", [
    (b'(module (import "wasi_snapshot_preview1" "fd_write" (func (param i32 i32 i32 i32) (result i32)))'
     b' (func (export "main")))', "чужие импорты"),
    (b'(module (func (export "start")))', "нет функции main"),
    (b"not wasm", "не WebAssembly"),
])
def test_own_agent_refused(tmp_path, monkeypatch, wat, why):
    import app.runner as runner
    monkeypatch.setattr(runner, "AGENTS", tmp_path)
    (tmp_path / "bad.wat").write_bytes(wat)
    with pytest.raises(Refusal, match=why):
        settings_from({"contour": "сомкнутый", "model": INSTALL_CHECK, "words": "x", "agent": "bad.wat"})


def test_own_agent_name_is_not_a_path(tmp_path, monkeypatch):
    import app.runner as runner
    monkeypatch.setattr(runner, "AGENTS", tmp_path)
    for name in ("../start.py", "/etc/passwd", "x.txt"):
        with pytest.raises(Refusal):
            settings_from({"contour": "сомкнутый", "model": INSTALL_CHECK, "words": "x", "agent": name})


def test_live_gguf_refuses_to_start_without_bwrap(tmp_path, monkeypatch):
    from app import model_sandbox, runner

    def no_driver(*args, **kwargs):
        pytest.fail("executor started without a Bubblewrap sandbox")

    monkeypatch.setattr(model_sandbox, "check", lambda: "user namespaces unavailable")
    monkeypatch.setattr(runner, "SealedDriver", no_driver)
    settings = runner.Settings(contour="сомкнутый", words="test", model=str(tmp_path / "test.gguf"))
    run = runner.Run(settings, b"", tmp_path)
    run.run()
    assert run.snapshot()["state"] == "ошибка"
    assert run.snapshot()["notes"] == [{"code": "model_sandbox_unavailable", "why": "user namespaces unavailable"}]


def test_model_sandbox_hides_host_state(tmp_path):
    from app import model_sandbox

    if (reason := model_sandbox.check()) is not None:
        pytest.skip(reason)
    weights = tmp_path / "test.gguf"
    secret = tmp_path / "host-secret"
    journal = tmp_path / "journal5.jsonl"
    weights.write_bytes(b"GGUF")
    secret.write_bytes(b"secret")
    journal.write_bytes(b"journal")
    argv = model_sandbox.command(weights)
    network = os.readlink("/proc/self/ns/net")
    argv[-2:] = [
        """
import os, sys
from importlib.metadata import version
from pathlib import Path

secret, journal, pid, network = sys.argv[1:]
assert "TEST_HOST_SECRET" not in os.environ
assert os.environ["HOME"] == "/tmp"
assert not Path(secret).exists()
assert not Path(journal).exists()
assert not Path("/proc", pid).exists()
assert os.readlink("/proc/self/ns/net") != network
assert Path("/model/weights.gguf").read_bytes() == b"GGUF"
try:
    Path("/model/weights.gguf").write_bytes(b"changed")
except OSError:
    pass
else:
    raise AssertionError("GGUF mount is writable")
Path("/tmp/model-scratch").write_bytes(b"ok")
""",
        str(secret), str(journal), str(os.getpid()), network,
    ]
    subprocess.run(argv, env={**os.environ, "TEST_HOST_SECRET": "secret"}, check=True, timeout=10)
    assert weights.read_bytes() == b"GGUF"
    assert journal.read_bytes() == b"journal"
