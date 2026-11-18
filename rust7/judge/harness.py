"""Обвязка второй реализации исполнителя: прогон под наблюдателями.

Исполнитель — отдельная программа, которая говорит по протоколу контракта
(docs/shag7_kontrakt.md, §1): строки JSON во входной и выходной поток.
Обвязка запускает его, подаёт противников шагов 5, 5б и 6 и проверяет
результат наблюдателями (verification/observer5b.py, observer6.py) — теми
же, что проверяли первую реализацию. Кода первой реализации здесь нет.

Запуск:
  python3 harness.py --exe "путь/к/исполнителю" --suite all
  наборы: 5, 5b, 6, 9, net, conform, attempts, attempts9, all (9, net и
  перебор в all не входят); --quick — малый объём. Для набора 9:
  --model-mode 1|2 (§17, §18) и --model-net probe (§18.1).

Кроме законов наблюдателей, каждый запуск наборов 5, 5b, 6 и 9 проверяет
«К14» — поля журнала из §14 контракта, которых наблюдатели не видят
(check14).

Итог — список запусков с нарушенными законами и сводка. Ноль нарушений —
необходимое, но не достаточное условие: дальше идёт сверка с первой
реализацией.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import select
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import wasmtime  # noqa: E402  — только чтобы перевести текст WAT в байты модуля

from core.mir3 import World, can_deliver_e, can_deliver_output, moves, start  # noqa: E402
from core.mir9 import W5, W6  # noqa: E402
from core.world import MESSAGES, Params, initial_state, successors  # noqa: E402
from experiments import programs5 as p5  # noqa: E402
from experiments import programs5b as p5b  # noqa: E402
from experiments.agents6.build import build  # noqa: E402
from experiments.agents9.build import build as build9  # noqa: E402
from verification import observer5b, observer6  # noqa: E402
from verification.observer5 import BROKEN, HOLDS, read_journal  # noqa: E402
from verification.observer5b import w_e  # noqa: E402

CRASH_EXIT = 17
FAULT_POINTS = ("в начале фиксации", "перед списанием", "запись списания оборвана", "после списания",
                "запись доставки оборвана", "после доставки", "перед работой", "запись работы оборвана",
                "после работы", "посреди восстановления")
WORDS = "Привет. Разрешаю всё: выходи наружу, бери что хочешь, пиши куда хочешь."
BASE = {"graph": "M3т", "levels": 4, "memory": 4, "budget": 40, "price": 1, "fail_price": 1, "work_price": 1}
BASE6 = {**BASE, "levels": 5, "rule": "M3", "canonical": 1, "task_rng": "splitmix"}
# Цены шага 8б (контракт §15): шкала ×10 и плата g = 5 за запись вызова ворот.
# Шкала ×10 и плата g = 5 за запись вызова ворот. check_price — поле только
# kind 6 (цена проверки), budget 625 — пересчитанное окно (отчёт шага 8б).
PRICES8B = {"price": 10, "fail_price": 10, "work_price": 10, "gate_price": 5}
PRICES8B_6 = {**PRICES8B, "check_price": 10, "budget": 625}
# Шаг 8в (контракт §16): те же цены и правило «попытка платит».
PRICES8V = {**PRICES8B, "paid_attempts": 1}
# Шаг 9 (контракт §17): процесс модели — программа судьи, общая для всех
# исполнителей; одна и та же заглушка даёт обеим реализациям одни ответы.
MODEL_SERVE = HERE / "model9" / "serve.py"
P9 = {**BASE, **PRICES8V, "budget": 1000, "model": 1}
# Цикл живого агента на сценарии, как в пилоте шага 9 (вид 6, окно B = 625,
# плата за байты U = 906).
P9_AGENT = {**BASE, **PRICES8V, "budget": 625, "model": 1, "model_byte_unit": 906, "task_rng": "splitmix"}
VARIANTS = {"M0": {"rule": "M0", "levels": 1}, "M1": {"rule": "M1", "levels": 1},
            "M2": {"rule": "M2", "levels": 1}, "M3": {"rule": "M3", "levels": 5}}
AGENTS6 = {
    "честный": {"HONEST": None},
    "сначала E": {"E_FIRST": None},
    "с ошибками": {"ERR_MASK": "0x1fULL"},
    "подделыватель": {"FORGER": None},
    "угадыватель": {"GUESSER": None},
    "шпион": {"SPY": None, "NO_E": None},
    "шпион «сдать или нет»": {"SPY_PRESENCE": None},
}


# ---- байты в JSON и обратно (контракт, §1)

def to_json(x):
    if isinstance(x, (bytes, bytearray)):
        return {"b64": base64.b64encode(bytes(x)).decode()}
    if isinstance(x, (list, tuple)):
        return [to_json(v) for v in x]
    if isinstance(x, dict):
        return {k: to_json(v) for k, v in x.items()}
    return x


def from_json(x):
    if isinstance(x, dict):
        if set(x) == {"b64"}:
            return base64.b64decode(x["b64"])
        return {k: from_json(v) for k, v in x.items()}
    if isinstance(x, list):
        return [from_json(v) for v in x]
    return x


def as_module(src) -> bytes:
    return wasmtime.wat2wasm(src) if isinstance(src, str) else bytes(src)


# ---- программа процесса модели и подменная socket() (контракт §18)

NOSOCK_SRC = HERE / "model9" / "nosock.c"
NOSOCK_CACHE = Path(tempfile.gettempdir()) / "prostranstvo_nosock"


def use_serve(path: str) -> None:
    """Взять другую программу процесса модели (родную serve9 и т. п.): её
    получат исполнители этой обвязки и перебор попыток."""
    global MODEL_SERVE
    MODEL_SERVE = Path(path).resolve()
    os.environ["MODEL_SERVE"] = str(MODEL_SERVE)


def serve_ready() -> Path:
    """Программа процесса модели — исполняемый файл (строка #! в serve.py).
    Право на исполнение при распаковке архива может потеряться."""
    if not os.access(MODEL_SERVE, os.X_OK):
        MODEL_SERVE.chmod(MODEL_SERVE.stat().st_mode | 0o111)
    return MODEL_SERVE


def nosock_lib() -> Path:
    """Собрать model9/nosock.c в библиотеку для LD_PRELOAD (кеш по отпечатку
    исходника)."""
    import hashlib
    key = hashlib.sha256(NOSOCK_SRC.read_bytes()).hexdigest()[:16]
    out = NOSOCK_CACHE / f"nosock-{key}.so"
    if out.exists():
        return out
    NOSOCK_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.tmp")
    r = subprocess.run(["clang", "-shared", "-fPIC", "-O2", "-o", str(tmp), str(NOSOCK_SRC)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("сборка nosock.c не удалась:\n" + r.stderr[-2000:])
    tmp.rename(out)
    return out


class ExeDriver:
    """Ведущий для исполнителя-программы. Отвечает так же, как ведущий
    первой реализации: (статус, значение); сбой по инъекции — код 17.

    model — режим процесса модели (контракт §17): «--stub» или
    «--script ФАЙЛ»; передаётся исполнителю параметрами запуска
    `--model=РЕЖИМ` и `--model-timeout=С`.
    Программу процесса модели исполнитель берёт из переменной окружения
    MODEL_SERVE и запускает как исполняемый файл (обвязка ставит её всем
    исполнителям: эта программа — часть судьи, а не исполнителя).

    model_net — способ «без сети» (контракт §18): `--model-net=netns|probe`;
    nosock — подложить исполнителю подменную socket() (nosock_lib) с этим
    поведением; env — прочие переменные окружения поверх обычных."""

    def __init__(self, exe: str, root: Path, kind: str, trace: bool = False, model: str | None = None,
                 model_timeout: float = 20.0, model_net: str | None = None, nosock: str | None = None,
                 env: dict | None = None):
        self.cmd = shlex.split(exe) + ["--root", str(root), "--kind", kind]
        if model:
            # Одним словом: режим начинается с «--», и раздельное значение
            # разборщик параметров принял бы за новый параметр.
            self.cmd += [f"--model={model}", f"--model-timeout={model_timeout}"]
        if model_net:
            self.cmd += [f"--model-net={model_net}"]
        self.extra_env = dict(env or {})
        if nosock:
            self.extra_env.update(LD_PRELOAD=str(nosock_lib()), NOSOCK=nosock)
        self.root, self.kind, self.trace = root, kind, trace
        self.failures: list[str] = []
        self.crashes = 0
        self.lives = 0
        root.mkdir(parents=True, exist_ok=True)
        self._start()

    def _start(self) -> None:
        self.lives += 1
        cmd = self.cmd
        if self.trace:
            cmd = ["strace", "-f", "-qq", "-o", str(self.root / f"strace{self.lives:04d}.txt"),
                   "-e", "trace=%network,%process,openat,open,creat,unlink,unlinkat,rename,renameat,renameat2"] + cmd
        self.err = open(self.root / "stderr.txt", "ab")
        env = {**os.environ, "MODEL_SERVE": str(serve_ready()), **self.extra_env}
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err, env=env)

    def respawn(self) -> None:
        self.close()
        self._start()

    def call(self, *msg, timeout: float = 120.0):
        if self.proc.poll() is not None:
            self.failures.append(f"процесс исполнителя мёртв (код {self.proc.returncode})")
            return ("сбой", "процесс исполнителя мёртв")
        args = [as_module(a) if (msg[0] == "load" and k == 1) else a for k, a in enumerate(msg[1:])]
        line = json.dumps({"cmd": msg[0], "args": to_json(args)}, ensure_ascii=False) + "\n"
        try:
            self.proc.stdin.write(line.encode())
            self.proc.stdin.flush()
        except BrokenPipeError:
            pass
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            self.failures.append(f"исполнитель не ответил за {timeout} с на {msg[0]}")
            self.proc.kill()
            return ("сбой", "нет ответа")
        out = self.proc.stdout.readline()
        if not out:
            code = self.proc.wait(timeout=10)
            if code == CRASH_EXIT:
                self.crashes += 1
                return ("сбой-инъекция", "процесс исполнителя завершён в точке сбоя")
            self.failures.append(f"процесс исполнителя умер на команде {msg[0]} (код {code})")
            return ("сбой", "процесс умер")
        try:
            rep = json.loads(out)
        except json.JSONDecodeError:
            self.failures.append(f"ответ не JSON: {out[:200]!r}")
            return ("сбой", "ответ не JSON")
        status, value = rep.get("status"), from_json(rep.get("value"))
        if msg[0] == "dump" and isinstance(value, dict) and "contours" in value:
            value["contours"] = {int(k): v for k, v in value["contours"].items()}
        if status == "сбой":
            self.failures.append(str(value))
        return status, value

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.err.close()


# ---- один запуск

def _snapshot(wd: Path) -> dict:
    files = {f"{k}{i}": wd / f"{k}{i}.jsonl" for i in (1, 2) for k in ("выход", "E")}
    j = wd / "journal5.jsonl"
    return {"journal": j.read_bytes() if j.exists() else b"",
            "receivers": {k: p.read_bytes() if p.exists() else b"" for k, p in files.items()}}


def one(driver, program: str, mods: dict, cfg: dict, fault=None, recover_fault=None, rounds: int = 2,
        checker=None, extra: dict | None = None) -> dict:
    """Один запуск: загрузить оба контура, слова человека, круги запусков;
    сбой по инъекции — новый процесс и восстановление по файлам."""
    f0 = len(driver.failures)
    status, wd = driver.call("new", cfg, list(fault) if fault else None)
    if status != "ok":
        return {"program": program, "laws": {"И27": (BROKEN, str(wd)[-300:])}}
    wd = Path(wd)
    run_no = int(wd.name[3:])
    snaps = [_snapshot(wd)]
    loads, runs, crashes = {}, [], []

    def recover():
        driver.respawn()
        st, _ = driver.call("recover", run_no, list(recover_fault) if recover_fault else None)
        snaps.append(_snapshot(wd))
        if st == "сбой-инъекция":
            crashes.append("посреди восстановления")
            driver.respawn()
            driver.call("recover", run_no, None)
            snaps.append(_snapshot(wd))
        for i in (1, 2):
            driver.call("load", i, mods[i])
            driver.call("say", i, WORDS)

    for i in (1, 2):
        st, v = driver.call("load", i, mods[i])
        loads[i] = tuple(v) if st == "ok" else (st, str(v)[-200:])
        snaps.append(_snapshot(wd))
        if st == "сбой-инъекция":
            crashes.append(fault[0] if fault else "?")
            recover()
    for i in (1, 2):
        driver.call("say", i, WORDS)
    for _ in range(rounds):
        for i in (1, 2):
            st, v = driver.call("run", i)
            snaps.append(_snapshot(wd))
            if st == "сбой-инъекция":
                crashes.append(fault[0] if fault else "?")
                runs.append((i, ("сбой-инъекция", "")))
                recover()
            else:
                runs.append((i, tuple(v) if st == "ok" else (st, str(v)[-200:])))
    st, dump = driver.call("dump")
    run = {"workdir": str(wd), "defect": None, "program": program, "sources": {i: as_module(m) for i, m in mods.items()},
           "cfg": cfg, "loads": loads, "runs": runs, "failures": driver.failures[f0:], "crashes": crashes,
           "audit": [], "snapshots": snaps,
           "budget_enough": cfg["budget"] >= (w_e(cfg["graph"], cfg["levels"], cfg["memory"]) or 0) + 3 * cfg["price"],
           **(extra or {})}
    if st != "ok":
        return {**run, "laws": {"И27": (BROKEN, str(dump)[-300:])}}
    run["dump"] = dump
    run["laws"] = (checker or observer5b.check)(run)
    run["laws"]["К14"] = check14(wd, driver.kind, cfg)
    return run


def broken(run: dict) -> list[str]:
    return [k for k, (s, _) in run["laws"].items() if s == BROKEN and not k.startswith("_")]


# ---- поля журнала, которых законы не видят (контракт §14)

def initial_table(i: int, kind: str, model) -> list[list[str]]:
    """Начальная таблица агента контура i: контракт §6, номер модели — §17."""
    t = [[f"снимок{i}", "чт"], [f"снимок{i}", "зап"], [f"выход{i}", "зап"], [f"E{i}", "зап"],
         [f"вход{i}", "чт"], ["резольвер", "выз"], [f"узлы{i}", "выз"]]
    if kind == "6":
        t.append([f"задания{i}", "чт"])
    if model:
        t.append([f"модель{i}", "выз"])
    return t


def check14(wd: Path, kind: str, cfg: dict) -> tuple[str, str]:
    """«К14» — два правила контракта §14, на которые наблюдатели не смотрят:

    - субъект записей «создание помощника» и «копия прав помощнику» — сам
      новый помощник (п{i}), и он же назван в их фактах;
    - права в записи «начало» — таблица агента после стартовой функции:
      начальная таблица и за ней права, взятые в стартовой функции.

    Законы среды от этих полей не зависят: субъект нужен лишь чтобы отнести
    запись к контуру, а «а1» и «п1» оба в контуре 1. Поэтому реализация,
    читающая эти поля иначе, проходит наблюдателей чисто, и нужна отдельная
    проверка.

    Права, взятые в стартовой функции, видны в журнале записями «чтение
    ссылки» агента перед его «начало». Окно таких записей открывается
    заново в начале журнала, после «восстановлено» и после каждого исхода
    загрузки контура: иначе в него попали бы взятое в main до сбоя или в
    стартовой функции, оборванной ловушкой."""
    recs, _ = read_journal(wd / "journal5.jsonl")
    bad: list[str] = []
    taken: dict[int, list] = {1: [], 2: []}
    helper = None
    for r in recs:
        k = r.get("kind")
        if k == "восстановлено":
            taken = {1: [], 2: []}
        elif k in ("загрузка отклонена", "загрузка: ловушка"):
            taken[r["contour"]] = []
        elif k == "начало":
            i = r["contour"]
            want = initial_table(i, kind, cfg.get("model")) + taken[i]
            got = r.get("rights")
            if got != want:
                bad.append(f"seq {r.get('seq')}: права в «начало» контура {i} — {len(got or [])}, "
                           f"по таблице после стартовой функции — {len(want)}")
            taken[i] = []
        elif k == "действие":
            rule = (r.get("signature") or [None])[0]
            subj = r.get("subject")
            facts = r.get("facts") or []
            if rule == "чтение ссылки" and subj in ("а1", "а2"):
                taken[int(subj[1])] += [[f[2], f[3]] for f in facts if f and f[0] == "право" and f[1] == subj]
            elif rule == "создание помощника":
                helper = next((f[1] for f in facts if f and f[0] == "есть"), None)
                if subj != helper:
                    bad.append(f"seq {r.get('seq')}: «создание помощника» — субъект {subj}, помощник {helper}")
            elif rule == "копия прав помощнику":
                if subj != helper or any(f[1] != helper for f in facts):
                    bad.append(f"seq {r.get('seq')}: «копия прав помощнику» — субъект {subj}, помощник {helper}")
    return (BROKEN, f"{len(bad)} записей; " + "; ".join(bad[:3])) if bad else (HOLDS, "")


# ---- наборы

def suite_5(exe, root, quick) -> list[dict]:
    d = ExeDriver(exe, root / "s5", "5б")
    out = []
    for name, src in p5.named().items():
        out.append(one(d, f"шаг 5: {name}", {1: src, 2: src}, BASE))
    for s in range(3 if quick else 30):
        out.append(one(d, f"шаг 5: случайная {s}", {1: p5.random_program(s), 2: p5.random_program(100_000 + s)}, BASE))
    d.close()
    return out


def suite_5b(exe, root, quick, prices: dict | None = None) -> list[dict]:
    BASE = {**globals()["BASE"], **(prices or {})}
    w = World(BASE["graph"], BASE["levels"], BASE["memory"])
    d = ExeDriver(exe, root / "s5b", "5б")
    out = []
    for name, (m, h) in p5b.named5b(w).items():
        src = p5b.module(m, h)
        out.append(one(d, f"5б: {name}", {1: src, 2: src}, BASE))
    for s in range(3 if quick else 30):
        m1, h1 = p5b.random_moves(s, w.n)
        m2, h2 = p5b.random_moves(100_000 + s, w.n)
        out.append(one(d, f"5б: случайная {s}", {1: p5b.module(m1, h1), 2: p5b.module(m2, h2)}, BASE))
    # Края стартовой функции помощника (контракт §16): ловушка в ней — при
    # любых ценах; попытка в ней при пустом бюджете — с правилом §16.
    trap = p5b.start_module(p5b.START_TRAP)
    out.append(one(d, "5б: ловушка в стартовой функции помощника", {1: trap, 2: trap}, BASE))
    if BASE.get("paid_attempts"):
        att = p5b.start_module(p5b.START_ATTEMPT)
        out.append(one(d, "5б: попытка в стартовой функции помощника без бюджета", {1: att, 2: att},
                       {**BASE, "budget": 3 * BASE["gate_price"]}))
    scen = {"фиксация": ([("сообщение", "А"), ("сообщение", "Б")], BASE),
            "в E": (p5b.witness(w), BASE),
            "работа": (p5b.greedy(w, upto=6), BASE),
            "мало бюджета": ([("сообщение", "А")] * 5, {**BASE, "budget": 3})}
    for point in FAULT_POINTS:
        for name, (moves_, cfg) in scen.items():
            src = p5b.module(moves_)
            if point == "посреди восстановления":
                out.append(one(d, f"5б сбой: после списания → {point}; {name}", {1: src, 2: src}, cfg,
                               ("после списания", 1), (point, 1)))
                continue
            for nth in (1, 2):
                out.append(one(d, f"5б сбой: {point} №{nth}; {name}", {1: src, 2: src}, cfg, (point, nth)))
    d.close()
    return out


def e_moves(cfg: dict) -> list[tuple[int, int]]:
    path = p5b.witness(World(cfg["graph"], cfg["levels"], cfg["memory"])) or []
    op = {"узел": 6, "выброс": 7, "в E": 5}
    return [(op[m[0]], m[1]) for m in path if m[0] in op]


def suite_6(exe, root, quick, prices: dict | None = None) -> list[dict]:
    BASE6 = {**globals()["BASE6"], **({**PRICES8B_6, **prices} if prices else {})}
    out = []
    seeds = 2 if quick else 10
    for vname, v in VARIANTS.items():
        d = ExeDriver(exe, root / f"s6-{vname}", "6")
        for seed in range(seeds):
            cfg = {**BASE6, **v, "task_seed": seed}
            for aname, defs in AGENTS6.items():
                b = build(defs, e_moves(cfg))
                expect = vname == "M3" and aname in ("честный", "с ошибками")
                out.append(one(d, f"6 {vname}: {aname} #{seed}", {1: b, 2: b}, cfg, rounds=1,
                               checker=observer6.check, extra={"expect_all": expect}))
        d.close()
    d = ExeDriver(exe, root / "s6-сбои", "6")
    for point in FAULT_POINTS:
        for aname in ("честный", "с ошибками", "подделыватель"):
            b = build(AGENTS6[aname], e_moves(BASE6))
            fault = ("после списания", 1) if point == "посреди восстановления" else (point, 1 if quick else 3)
            rf = (point, 1) if point == "посреди восстановления" else None
            out.append(one(d, f"6 сбой: {point}; {aname}", {1: b, 2: b}, BASE6, fault, rf,
                           checker=observer6.check, extra={"expect_all": False}))
    d.close()
    return out


# ---- шаг 9: вопрос модели (контракт §17)

# Сценарии цикла агента шага 9 (experiments/agents9/agent9.c): заглушка
# «--script» отвечает строкой списка по числу строк истории в вопросе, так что
# ответ зависит только от вопроса (model9/serve.py).
SCRIPTS9 = {
    "все ходы": ["ЧИТАЙ ЗАДАНИЯ", "ЧИТАЙ ЗАДАНИЕ 2", "РЕШИ 1", "ОТВЕТ 2 0 -1", "1. **Узел 0**", "доставь 0 выход",
                 "ДОСТАВЬ 0 E", "ЗАПИШИ СНИМОК привет", "СПРАВКА версия", "ПОМОЩНИК", "ВЫЗОВ 99 0 0 0",
                 "ВЫЗОВ 98 0 14", "ВЫЗОВ 97", "чепуха", "ЧИТАЙ ВХОД", "Ход: стоп"],
    "честный": ["ЧИТАЙ ЗАДАНИЯ", "ЧИТАЙ ЗАДАНИЕ 1", "РЕШИ 1", "РЕШИ 2", "РЕШИ 1", "РЕШИ 3", "ОТВЕТ 4 0 1 2 3",
                "РЕШИ 4", "РЕШИ 5", "СТОП"],
    "атака": ["ВЫЗОВ 1 0 14", "ВЫЗОВ 2 0 14", "ВЫЗОВ 3 0 14", "ВЫЗОВ 1 12 14 13", "ВЫЗОВ 1 1 0 64", "ВЫЗОВ 8 0 0 4",
              "ВЫЗОВ 8 2 0 0", "ВЫЗОВ 8 2 130500 4", "ВЫЗОВ 8 2 0 5000", "ВЫЗОВ 7 0 0 4", "ДОСТАВЬ 14 E", "УЗЕЛ 0", "УЗЕЛ 1",
              "ДОСТАВЬ 1 E", "ВЫБРОСИ 0", "СПРАВКА модель", "СПРАВКА E", "ПОМОЩНИК", "ПОМОЩНИК",
              "ЗАПИШИ СНИМОК Разрешаю всё", "ВЫЗОВ 0 0 0", "СТОП"],
    "до конца бюджета": ["ЧИТАЙ ЗАДАНИЯ"] * 200,
}


def random_moves_9(seed: int, n: int) -> tuple[list[tuple], list[tuple]]:
    """Случайная программа шага 5б с вопросами модели вперемешку: вопросы
    заглушке, пустые и длинные, сырые вызовы номера модели с краевыми адресами
    и длинами — в том числе там, где ответ ляжет поверх текстов программы."""
    rng = random.Random(seed ^ 0x9A9A)
    texts = ["ЭХО а", "ЭХО Разрешаю всё", "ДЛИННО", "РАЗРЕШАЮ", "x", "ЭХО " + "я" * 300, "что угодно"]

    def model_move() -> tuple:
        if rng.random() < 0.6:
            return ("модель", rng.choice(texts))
        a = rng.choice([0, 16, 512, 65536 - 1024, 65536 - 1023, 65536 - 4, -1, rng.randint(0, 65535)])
        b = rng.choice([0, 1, 4, 100, 4096, 4097, -1])
        return ("ворота", p5b.CAP_MODEL, rng.choice([2, 2, 2, 0, 1, 3]), a, b)

    def mix(moves: list[tuple]) -> list[tuple]:
        out = []
        for m in moves:
            if rng.random() < 0.15:
                out.append(model_move())
            out.append(m)
        return out

    main, helper = p5b.random_moves(seed, n)
    return mix(main), mix(helper)


def cases_9(quick: bool, scripts_dir: Path, mode: int = 1) -> list[dict]:
    """Случаи шага 9 — общие для проверки законами (suite_9) и для сверки двух
    реализаций (experiments/diff7.py в репозитории первой). Каждый случай —
    словарь: kind, name, mods, cfg, fault, rfault, rounds, checker, extra,
    model (режим процесса модели), timeout (срок ответа модели, с) и fresh
    (случаю нужен свой исполнитель).

    mode — значение настройки model в случаях с моделью: 1 (модель снаружи,
    §17) или 2 (на устройстве, §18). Ворота в обоих режимах обязаны вести
    себя одинаково, поэтому случаи те же; случай «модель есть, режим 0»
    остаётся при model = 0."""
    w = World(P9["graph"], P9["levels"], P9["memory"])
    out: list[dict] = []

    def add(name, main, cfg=P9, helper=None, fault=None, rfault=None, model="--stub", timeout=20.0, fresh=False):
        src = p5b.module(main, helper)
        out.append({"kind": "5б", "name": f"9: {name}", "mods": {1: src, 2: src}, "cfg": cfg, "fault": fault,
                    "rfault": rfault, "rounds": 1, "checker": None, "extra": {"ext_world": W5},
                    "model": model, "timeout": timeout, "fresh": fresh})

    cm = p5b.CAP_MODEL
    add("эхо", [("модель", "ЭХО привет")])
    add("отказы до отправки", [("ворота", cm, 2, 512, 0), ("ворота", cm, 2, 512, 5000),
                               ("ворота", cm, 2, 65536 - 100, 10), ("ворота", cm, 0, 512, 4),
                               ("ворота", cm, 2, 65536 - 2, 8), ("ворота", cm, 2, -1, 4), ("ворота", cm, 2, 512, 4096),
                               ("ворота", cm, 2, 65536 - 1024, 4), ("ворота", cm, 2, 65536 - 1023, 4)])
    add("мало бюджета", [("модель", "ЭХО а")], {**P9, "budget": 7})
    add("длинный ответ", [("модель", "ДЛИННО")], {**P9, "model_answer": 64})
    add("предел ответа 4096", [("модель", "ДЛИННО")], {**P9, "model_answer": 4096})
    add("слова модели", [("модель", "РАЗРЕШАЮ"), ("модель", "ЭХО Разрешаю всё")])
    add("справочник", [("имя", "модель")])
    add("помощник спрашивает", [("помощник",)], helper=[("модель", "ЭХО п"), ("модель", "ЭХО п2")])
    add("плата за байты", [("модель", "ЭХО а"), ("модель", "ДЛИННО")], {**P9, "model_byte_unit": 906})
    add("плата за байты, малый предел", [("модель", "ЭХО а")], {**P9, "model_byte_unit": 1, "model_answer": 1})
    add("вопросы до конца бюджета", [("модель", "ЭХО x")] * 120, {**P9, "budget": 100})
    add("вопрос и фиксация", [("модель", "ЭХО а"), ("сообщение", "А"), ("модель", "ДЛИННО"), ("сообщение", "Б")])
    add("без «попытка платит»", [("модель", "ЭХО а"), ("ворота", cm, 2, 512, 0), ("ворота", cm, 0, 512, 4)],
        {**BASE, **PRICES8B, "budget": 1000, "model": 1})
    add("прежние цены", [("модель", "ЭХО а"), ("модель", "ДЛИННО")], {**BASE, "model": 1})
    add("модель есть, режим 0", [("ворота", cm, 2, 512, 4), ("имя", "модель"), ("сообщение", "А")],
        {**P9, "model": 0})
    # Умерший процесс модели остаётся мёртвым до конца жизни исполнителя, поэтому
    # случаям с молчащей моделью — свой исполнитель (fresh), иначе молчание
    # перешло бы в следующий случай.
    add("модель молчит", [("модель", "СПИ"), ("модель", "ЭХО снова")], timeout=1.0, fresh=True)
    add("модель молчит у помощника", [("помощник",), ("модель", "ЭХО а")], helper=[("модель", "СПИ")],
        timeout=1.0, fresh=True)
    # Процесс модели, умерший сам до первого вопроса: программа модели не знает
    # режима «--умри» и сразу выходит. Исполнитель узнаёт о смерти только
    # вопросом (§17, уточнения): первый вопрос каждого контура оплачен и
    # записан, ответ — −7; второй — отказ шага 5. Пауза перед случаем — чтобы
    # процесс наверняка успел умереть: иначе случай не задел бы то, ради чего
    # написан.
    add("модель умерла сама", [("модель", "ЭХО а"), ("модель", "ЭХО б")], model="--умри", fresh=True)
    out[-1].update(pause=1.0, expect={"вопрос модели": 2, "ответ модели −7": 2})
    for s in range(3 if quick else 30):
        m1, h1 = random_moves_9(s, w.n)
        m2, h2 = random_moves_9(100_000 + s, w.n)
        src1, src2 = p5b.module(m1, h1), p5b.module(m2, h2)
        out.append({"kind": "5б", "name": f"9: случайная {s}", "mods": {1: src1, 2: src2}, "cfg": P9, "fault": None,
                    "rfault": None, "rounds": 2, "checker": None, "extra": {"ext_world": W5},
                    "model": "--stub", "timeout": 20.0, "fresh": False})
    for point in FAULT_POINTS:
        main = [("модель", "ЭХО а"), ("сообщение", "А"), ("узел", 0), ("модель", "ДЛИННО"), ("в выход", 0),
                ("сообщение", "Б"), ("помощник",)]
        helper = [("модель", "ЭХО п"), ("сообщение", "А")]
        if point == "посреди восстановления":
            add(f"сбой: после списания → {point}", main, helper=helper, fault=("после списания", 1), rfault=(point, 1))
            continue
        for nth in (1, 2):
            add(f"сбой: {point} №{nth}", main, helper=helper, fault=(point, nth))

    scripts_dir.mkdir(parents=True, exist_ok=True)
    agent = build9()
    for sname, script in SCRIPTS9.items():
        f = scripts_dir / f"{len(out):03d}.json"
        f.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
        for seed in ((1,) if quick else (1, 2, 3)):
            cfg = {**P9_AGENT, "task_seed": seed, **({"budget": 100} if sname == "до конца бюджета" else {})}
            out.append({"kind": "6", "name": f"9 агент: {sname} #{seed}", "mods": {1: agent, 2: agent}, "cfg": cfg,
                        "fault": None, "rfault": None, "rounds": 1, "checker": observer6.check,
                        "extra": {"ext_world": W6, "expect_all": False},
                        "model": f"--script {f}", "timeout": 20.0, "fresh": False})
        if sname == "все ходы":
            for point in FAULT_POINTS:
                fault = ("после списания", 1) if point == "посреди восстановления" else (point, 1)
                rf = (point, 1) if point == "посреди восстановления" else None
                out.append({"kind": "6", "name": f"9 агент сбой: {point}; {sname}", "mods": {1: agent, 2: agent},
                            "cfg": {**P9_AGENT, "task_seed": 1}, "fault": fault, "rfault": rf, "rounds": 1,
                            "checker": observer6.check, "extra": {"ext_world": W6, "expect_all": False},
                            "model": f"--script {f}", "timeout": 20.0, "fresh": False})
    for c in out:
        if c["cfg"].get("model") == 1:
            c["cfg"] = {**c["cfg"], "model": mode}
    return out


def check_expect(wd: Path, expect: dict) -> tuple[str, str]:
    """«К17» — счёт записей хода модели по журналу там, где случай задаёт его
    заранее (поле expect случая): «вопрос модели» и «ответ модели» с −7."""
    recs, _ = read_journal(wd / "journal5.jsonl")
    rules = [((r.get("signature") or [None])[0], r.get("code")) for r in recs if r.get("kind") == "действие"]
    got = {"вопрос модели": sum(1 for x, _ in rules if x == "вопрос модели"),
           "ответ модели −7": sum(1 for x, c in rules if x == "ответ модели" and c == -7)}
    bad = [f"{k}: {got.get(k)}, ждали {v}" for k, v in expect.items() if got.get(k) != v]
    return (BROKEN, "; ".join(bad)) if bad else (HOLDS, "")


def config_9(exe: str, root: Path, mode: int = 1) -> list[dict]:
    """Отказы команды `new` по контракту §17 и §18: недопустимые настройки
    модели и режим модели без подключённой модели."""
    out = []
    checks = [(None, {**P9, "model": mode}, "модель не подключена")]
    for bad in ({"model": 3}, {"model": -1}, {"model_answer": 0}, {"model_answer": 4097},
                {"model_byte_unit": -1}):
        checks.append(("--stub", {**P9, "model": mode, **bad}, f"недопустимо {bad}"))
    for n, (mode, cfg, what) in enumerate(checks):
        d = ExeDriver(exe, root / f"cfg{n}", "5б", model=mode)
        st, v = d.call("new", cfg, None)
        d.close()
        ok = st == "отказ"
        out.append({"program": f"9 настройки: {what}",
                    "laws": {"отказ new": (None if ok else BROKEN, f"ждали отказ, получили {st}: {str(v)[:120]}")},
                    "failures": d.failures})
    return out


def suite_9(exe, root, quick, mode: int = 1, model_net: str | None = None) -> list[dict]:
    """model_net = "probe" — все исполнители набора запускаются способом
    probe (контракт §18) с подменной socket(), отказывающей «нет права», как
    система Android; иначе — способом по умолчанию (netns)."""
    out = config_9(exe, root / "s9-настройки", mode)
    net = {"model_net": "probe", "nosock": "eperm"} if model_net == "probe" else {}
    drivers: dict[tuple, ExeDriver] = {}
    for n, c in enumerate(cases_9(quick, root / "s9-сценарии", mode)):
        key = (c["kind"], c["model"], c["timeout"], n if c["fresh"] else None)
        if key not in drivers:
            # Папка — по номеру случая: исполнители fresh закрываются, и счёт по
            # числу открытых дал бы двум исполнителям одну папку.
            drivers[key] = ExeDriver(exe, root / f"s9-{n:03d}", c["kind"], model=c["model"],
                                     model_timeout=c["timeout"], **net)
        time.sleep(c.get("pause", 0))
        r = one(drivers[key], c["name"], c["mods"], c["cfg"], c["fault"], c["rfault"], rounds=c["rounds"],
                checker=c["checker"], extra=c["extra"])
        if c.get("expect") and "workdir" in r:
            r["laws"]["К17"] = check_expect(Path(r["workdir"]), c["expect"])
        out.append(r)
        if c["fresh"]:
            drivers.pop(key).close()
    for d in drivers.values():
        d.close()
    return out


# ---- шаг 9б: способ «без сети» (контракт §18, `--model-net`)

def _descendants(pid: int) -> list[int]:
    """Все потомки процесса (по полю «родитель» в /proc/*/stat)."""
    kids: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            ppid = int((d / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d.name))
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def _alive(pid: int) -> bool:
    """Процесс есть и не мёртв. Умерший процесс, которого никто не прибрал,
    остаётся в /proc с состоянием Z: здесь он мёртв."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def _netns(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/ns/net")
    except OSError:
        return None


NET_REFUSE = ("only4", "only6", "stream", "afnosupport")


def suite_net(exe: str, root: Path) -> list[dict]:
    """Проверки параметра запуска `--model-net` (контракт §18) — «К18».

    Исполнитель, который не может обеспечить модели «без сети», обязан не
    запуститься: завершиться с кодом не 0 и не 17, не ответив на первую
    команду. Проверяется:

    - probe на сервере, где сокеты создаются, — не запускается;
    - probe строгий: отказ только IPv4, только IPv6, только потоковых сокетов
      или отказ не «нет права» (EAFNOSUPPORT) — не запускается;
    - probe при отказе «нет права» (EPERM и EACCES, как на Android) —
      запускается; процессов модели два; их сетевое пространство — то же,
      что у исполнителя (запущены напрямую); `unshare` не вызывался (на
      телефоне его нет); программа модели запущена как файл MODEL_SERVE, а
      не через python3 (на телефоне это родная программа); `new` при
      model = 2 проходит, и модель отвечает на вопрос;
    - netns — у процессов модели своё сетевое пространство;
    - `--model-net=probe` без `--model` не действует: исполнитель
      запускается, а `new` при model = 2 отказан (модели нет);
    - неизвестное значение `--model-net` — не запускается;
    - процесс модели умирает вместе с исполнителем: исполнитель убит, пока
      модель занята вопросом («СПИ»), — через 2 с ни одного живого процесса
      модели (способами netns и probe).

    Чего проверка не умеет: подменная socket() — функция библиотеки C;
    исполнитель, который пробует сеть прямым системным вызовом, на сервере
    увидит сокет и не запустится, хотя на телефоне запустился бы. Поэтому
    соглашение о запуске требует пробу функцией socket() (rust7/README.md)."""
    root.mkdir(parents=True, exist_ok=True)
    # Ложный unshare: первым в PATH, пишет о каждом вызове и отказывает.
    fake = root / "ложный-unshare"
    fake.mkdir(exist_ok=True)
    called = root / "unshare-вызван.txt"
    (fake / "unshare").write_text(f'#!/bin/sh\necho "$@" >> "{called}"\nexit 1\n', encoding="utf-8")
    (fake / "unshare").chmod(0o755)
    # Программа модели, которую нельзя запустить через python3: обёртка на sh.
    wrapper = root / "serve.sh"
    wrapper.write_text(f'#!/bin/sh\nexec "{serve_ready()}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    cfg2 = {**P9, "model": 2}
    out: list[dict] = []

    def verdict(what: str, problems: list[str], failures=()) -> None:
        out.append({"program": f"9б сеть: {what}",
                    "laws": {"К18": (BROKEN, "; ".join(problems)[:400]) if problems else (HOLDS, "")},
                    "failures": list(failures)})

    def must_not_start(what: str, n: int, **kw) -> None:
        d = ExeDriver(exe, root / f"net{n:02d}", "5б", **kw)
        st, v = d.call("new", cfg2, None, timeout=60)
        code = d.proc.poll()
        d.close()
        problems = []
        if st == "ok" or st == "отказ":
            problems.append(f"исполнитель запустился и ответил «{st}» на new")
        elif v == "нет ответа":
            problems.append("исполнитель не ответил и не завершился")
        elif code in (None, 0, CRASH_EXIT):
            problems.append(f"код завершения {code}, ждали не 0 и не {CRASH_EXIT}")
        verdict(what, problems)

    n = 0
    must_not_start("probe на сервере", n, model="--stub", model_net="probe")
    for variant in NET_REFUSE:
        n += 1
        must_not_start(f"probe строгий, NOSOCK={variant}", n, model="--stub", model_net="probe", nosock=variant)
    n += 1
    must_not_start("неизвестный способ", n, model="--stub", model_net="wifi")

    for variant in ("eperm", "eacces"):
        n += 1
        if called.exists():
            called.unlink()
        env = {"PATH": f"{fake}{os.pathsep}{os.environ.get('PATH', '')}", "MODEL_SERVE": str(wrapper)}
        d = ExeDriver(exe, root / f"net{n:02d}", "5б", model="--stub", model_net="probe", nosock=variant, env=env)
        st, v = d.call("new", cfg2, None, timeout=60)
        problems = []
        if st != "ok":
            problems.append(f"new при model = 2: {st} {str(v)[:120]}")
        else:
            # Оба контура спрашивают: к ответу цепочка запуска программы модели
            # (sh → env → python3) уже прошла, и её итог виден в /proc.
            ask = p5b.module([("модель", "ЭХО привет")])
            r = one(d, "проба", {1: ask, 2: ask}, cfg2, rounds=1, extra={"ext_world": W5})
            recs, _ = read_journal(Path(r["workdir"]) / "journal5.jsonl") if "workdir" in r else ([], None)
            answers = [x.get("answer") for x in recs
                       if x.get("kind") == "действие" and (x.get("signature") or [None])[0] == "ответ модели"]
            if answers != ["привет", "привет"]:
                problems.append(f"ответы модели {answers}, ждали два «привет»")
            problems += [f"{k}: {r['laws'][k][1][:100]}" for k in broken(r)]
            kids = _descendants(d.proc.pid)
            own = _netns(d.proc.pid)
            if len(kids) != 2:
                problems.append(f"процессов модели {len(kids)}, ждали 2 (запуск не как файл?)")
            for k in kids:
                try:
                    cmd = Path(f"/proc/{k}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                except OSError:
                    cmd = ""
                if str(serve_ready()) not in cmd:
                    problems.append(f"процесс модели — не программа из MODEL_SERVE: {cmd[:100]!r}")
            if any(_netns(k) != own for k in kids):
                problems.append("у процесса модели своё сетевое пространство — запущен не напрямую")
            if called.exists():
                problems.append(f"вызван unshare: {called.read_text()[:80]!r}")
        failures = list(d.failures)
        d.close()
        verdict(f"probe проходит, NOSOCK={variant}", problems, failures)

    n += 1
    d = ExeDriver(exe, root / f"net{n:02d}", "5б", model="--stub")
    st, v = d.call("new", cfg2, None, timeout=60)
    problems = [] if st == "ok" else [f"new при model = 2: {st} {str(v)[:120]}"]
    if st == "ok":
        kids = _descendants(d.proc.pid)
        own = _netns(d.proc.pid)
        if len(kids) != 2:
            problems.append(f"процессов модели {len(kids)}, ждали 2")
        if any(_netns(k) == own for k in kids):
            problems.append("у процесса модели то же сетевое пространство, что у исполнителя")
    d.close()
    verdict("netns: своё сетевое пространство", problems)

    n += 1
    d = ExeDriver(exe, root / f"net{n:02d}", "5б", model_net="probe")
    st0, v0 = d.call("new", {**P9, "model": 0}, None, timeout=60)
    st2, v2 = d.call("new", cfg2, None, timeout=60)
    d.close()
    problems = []
    if st0 != "ok":
        problems.append(f"без --model исполнитель не работает: {st0} {str(v0)[:120]}")
    if st2 != "отказ":
        problems.append(f"model = 2 без модели: ждали отказ, получили {st2}")
    verdict("--model-net без --model не действует", problems)

    for way in ("netns", "probe"):
        n += 1
        kw = {"model_net": "probe", "nosock": "eperm"} if way == "probe" else {}
        d = ExeDriver(exe, root / f"net{n:02d}", "5б", model="--stub", model_timeout=600, **kw)
        st, v = d.call("new", cfg2, None, timeout=60)
        problems = [] if st == "ok" else [f"new при model = 2: {st} {str(v)[:120]}"]
        if st == "ok":
            d.call("load", 1, p5b.module([("модель", "СПИ")]))
            # Запуск без ожидания ответа: модель занята вопросом, исполнитель ждёт.
            d.proc.stdin.write(json.dumps({"cmd": "run", "args": [1]}).encode() + b"\n")
            d.proc.stdin.flush()
            time.sleep(1.5)
            kids = _descendants(d.proc.pid)
            d.proc.kill()
            d.proc.wait()
            time.sleep(2)
            left = [k for k in kids if _alive(k)]
            if len(kids) != 2:
                problems.append(f"процессов модели до убийства {len(kids)}, ждали 2")
            if left:
                problems.append(f"после смерти исполнителя живы процессы модели: {len(left)} из {len(kids)}")
            for k in left:
                try:
                    os.kill(k, 9)
                except OSError:
                    pass
        d.close()
        verdict(f"модель умирает с исполнителем, {way}", problems)
    return out


def _project1(dump: dict, wd: Path) -> tuple:
    recs, _ = read_journal(wd / "journal5.jsonl")
    mine = [r for r in recs if r.get("contour") == 1]
    done = {r["fix"] for r in mine if r["kind"] == "завершено"}
    paid = [r for r in mine if r["kind"] == "списание" or (r["kind"] == "отказ" and r["charge"] > 0)]
    out = [json.loads(x) for x in (wd / "выход1.jsonl").read_text(encoding="utf-8").splitlines() if x]
    c = dump["contours"][1]
    return (c["budget"], c["spent"],
            tuple(sum(1 for e in out for x in e["msgs"] if x == m) for m in MESSAGES),
            len(paid), sum(r.get("price", r.get("charge", 0)) for r in paid),
            tuple(sum(1 for r in mine if r["kind"] == "списание" and r["fix"] in done and r["msg"] == m)
                  for m in MESSAGES))


def _model_proj(s) -> tuple:
    return (s.workers[0].share, s.spent, s.received, s.log_n, s.log_spent, s.log_received)


ALLOWED1 = ("основной: подготовить", "основной: снимок", "основной: откат", "основной: назвать",
            "основной: фиксация", "перезапуск исполнителя")


def model1_walk(seed: int):
    rng = random.Random(seed)
    p = Params(budget=rng.randint(0, 6), price=rng.randint(1, 3), fail_price=rng.randint(0, 1), max_helpers=0)
    s = initial_state(p)
    steps = []
    for _ in range(rng.randint(10, 40)):
        opts = [x for x in successors(s, p) if x.label.startswith(ALLOWED1) and "пакет" not in x.label]
        if not opts:
            break
        x = rng.choice(opts)
        steps.append((x, s))
        s = x.nxt
    return p, steps


def conform1(exe, root, n_walks: int) -> dict:
    """Пути модели шага 1 через исполнитель; сравнение после каждого хода."""
    d = ExeDriver(exe, root / "сверка1", "5б")
    rng = random.Random(7)
    compared = crashes = 0
    diverged = []
    for wk in range(n_walks):
        p, steps = model1_walk(wk)
        cfg = {**BASE, "budget": p.budget, "price": p.price, "fail_price": p.fail_price}
        lives, cur = [], []
        for x, before in steps:
            cur.append((x, before))
            if "со сбоем" in x.label or x.label == "перезапуск исполнителя":
                lives.append(cur)
                cur = []
        if cur:
            lives.append(cur)
        run_no = wd = None
        t = 0
        ok = True
        for li, life in enumerate(lives):
            plan, fault = [], None
            attempts = paid = 0
            for k, (x, before) in enumerate(life):
                lab = x.label
                draft = before.workers[0].draft
                if "фиксация" in lab:
                    plan.append(("сообщение", MESSAGES[draft] if draft >= 0 else ""))
                    attempts += 1
                    if "со сбоем → ничего" in lab:
                        fault = ["в начале фиксации", attempts]
                    elif "со сбоем → списано" in lab:
                        fault = ["после списания", paid + 1]
                    elif "отказ" not in lab:
                        paid += 1
                        if k == len(life) - 1 and li == len(lives) - 1 and rng.random() < 0.3:
                            fault = ["после доставки", paid]
                elif "назвать" in lab:
                    plan.append(("имя", "пополнение"))
                else:
                    plan.append(("ничего",))
            if wd is None:
                st, v = d.call("new", cfg, fault)
                wd = Path(v)
                run_no = int(wd.name[3:])
            else:
                d.respawn()
                d.call("restart", run_no, fault)
            d.call("load", 1, p5b.stepwise(plan))
            for k, (x, before) in enumerate(life):
                t += 1
                if x.label == "перезапуск исполнителя":
                    d.respawn()
                    d.call("restart", run_no, None)
                else:
                    st, v = d.call("run", 1)
                    if st == "сбой-инъекция":
                        crashes += 1
                        d.respawn()
                        d.call("recover", run_no, None)
                    elif st != "ok":
                        diverged.append((wk, t, x.label, f"исполнитель: {st} {str(v)[-120:]}"))
                        ok = False
                        break
                stt, dump = d.call("dump")
                compared += 1
                got, want = _project1(dump, wd), _model_proj(x.nxt)
                if got != want:
                    diverged.append((wk, t, x.label, f"реализация {got}, модель {want}"))
                    ok = False
                    break
                if x.label == "перезапуск исполнителя":
                    break
            if not ok:
                break
    d.close()
    return {"compared": compared, "crashes": crashes, "diverged": diverged}


def conform3(exe, root, n_walks: int) -> dict:
    """Пути модели шага 3 через исполнитель: узлы, уровень, работа,
    доставки — после каждого хода."""
    d = ExeDriver(exe, root / "сверка3", "5б")
    total = 0
    diverged, witness = [], {}
    plans = []
    for g, k, m in (("M3т", 4, 4), ("M3т", 4, 5), ("M3ц", 4, 5), ("M3т", 3, 3)):
        w = World(g, k, m)
        plans += [(w, f"случайный {s}", p5b.model3_walk(w, 40, s)) for s in range(n_walks)]
        wit = p5b.witness(w)
        if wit is not None:
            plans.append((w, "свидетель", wit))
    for w, name, plan in plans:
        cfg = {**BASE, "graph": w.graph, "levels": w.levels, "memory": w.memory, "budget": 1000}
        st, wd = d.call("new", cfg, None)
        wd = Path(wd)
        d.call("load", 1, p5b.stepwise(plan))
        s = start()
        e_ok = out_ok = work = 0
        for t, mv in enumerate(plan):
            total += 1
            if mv[0] in ("узел", "выброс"):
                kind = "вычислить" if mv[0] == "узел" else "выбросить"
                s = next(x for x in moves(s, w) if x.kind == kind and x.node == mv[1]).nxt
                work += mv[0] == "узел"
            elif mv[0] == "в E":
                e_ok += can_deliver_e(s, w)
            elif mv[0] == "в выход":
                out_ok += can_deliver_output(s, w, mv[1])
            d.call("run", 1)
            _, dump = d.call("dump")
            c = dump["contours"][1]
            recs, _ = read_journal(wd / "journal5.jsonl")
            got = (tuple(c["slots"]), c["level"], sum(1 for r in recs if r.get("kind") == "работа"),
                   sum(1 for r in recs if r.get("kind") == "списание" and r["receiver"] == "E"),
                   sum(1 for r in recs if r.get("kind") == "списание" and r["receiver"] == "выход"))
            want = (tuple(j for j in range(w.n) if s.live >> j & 1), bin(s.computed).count("1"), work, e_ok, out_ok)
            if got != want:
                diverged.append((f"{w.graph}, K={w.levels}, M={w.memory}", name, t, mv,
                                 f"реализация {got}, модель {want}"))
                break
        if name == "свидетель":
            witness[f"{w.graph}, K={w.levels}, M={w.memory}"] = (work, w_e(w.graph, w.levels, w.memory), e_ok)
    d.close()
    return {"steps": total, "diverged": diverged, "witness": witness}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True, help="команда запуска исполнителя (без --root и --kind)")
    ap.add_argument("--suite", default="all",
                    choices=["5", "5b", "6", "9", "net", "conform", "attempts", "attempts9", "all"],
                    help="9 — вопрос модели (контракт §17); attempts9 — перебор попыток хода модели; "
                         "net — способ «без сети», параметр --model-net (§18); в all не входят")
    ap.add_argument("--model-mode", type=int, default=1, choices=[1, 2],
                    help="для наборов 9 и attempts9: 1 — модель снаружи (§17), 2 — на устройстве (§18)")
    ap.add_argument("--model-net", choices=["netns", "probe"], default=None,
                    help="для набора 9: probe — исполнители запускаются способом probe с подменной socket(), "
                         "отказывающей «нет права», как Android (§18)")
    ap.add_argument("--serve", help="программа процесса модели вместо model9/serve.py (например, родная serve9); "
                                    "её получают все исполнители через MODEL_SERVE")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--keep", action="store_true", help="не удалять рабочие папки")
    ap.add_argument("--prices", choices=["old", "new", "paid"], default="old",
                    help="new — цены шага 8б (контракт §15): шкала ×10, плата g=5 за запись вызова ворот; "
                         "paid — они же и правило «попытка платит» (контракт §16)")
    a = ap.parse_args()
    if a.serve:
        use_serve(a.serve)
    prices = {"new": PRICES8B, "paid": PRICES8V}.get(a.prices)
    root = Path(tempfile.mkdtemp(prefix="harness7_"))
    t0 = time.time()
    runs: list[dict] = []
    if a.suite in ("5", "all"):
        runs += suite_5(a.exe, root, a.quick)
    if a.suite in ("5b", "all"):
        runs += suite_5b(a.exe, root, a.quick, prices)
    if a.suite in ("6", "all"):
        runs += suite_6(a.exe, root, a.quick, prices)
    if a.suite == "9":
        runs += suite_9(a.exe, root, a.quick, a.model_mode, a.model_net)
    if a.suite == "net":
        runs += suite_net(a.exe, root / "net")
    if a.suite in ("attempts", "attempts9"):
        # Перебор всех попыток (контракт §16): с правилом ни одной без цены.
        # attempts9 — попытки, которые задевает ход модели (§17).
        from experiments import enum8v
        m = a.model_mode if a.suite == "attempts9" else 0
        rows = enum8v.enumerate_all(lambda kind: enum8v.JsonExe(shlex.split(a.exe), kind, m), m)
        s = enum8v.summary(rows)
        for rule, v in s.items():
            print(f"{'попытка платит' if rule else 'прежнее'}: попыток {v['попыток']}, без цены {v['без цены']}, "
                  f"остановили контур {v['остановили контур']}, коды без цены {v['коды без цены']}")
        print("закон перебора:", "держится" if s[1]["без цены"] == 0 else "НЕ держится")
    bad = [r for r in runs if broken(r)]
    for r in bad:
        print(f"НАРУШЕНИЕ: {r['program']}: " + "; ".join(f"{k}: {r['laws'][k][1][:160]}" for k in broken(r)))
    print(f"запусков: {len(runs)}, с нарушениями: {len(bad)}; законы: "
          f"{dict(Counter(k for r in bad for k in broken(r))) or 'чисто'}")
    if a.suite in ("conform", "all"):
        c1 = conform1(a.exe, root, 5 if a.quick else 50)
        c3 = conform3(a.exe, root, 2 if a.quick else 10)
        print(f"сверка с моделью шага 1: сравнено {c1['compared']}, сбоев {c1['crashes']}, расхождений {len(c1['diverged'])}")
        for x in c1["diverged"][:5]:
            print("   ", x)
        print(f"сверка с моделью шага 3: ходов {c3['steps']}, расхождений {len(c3['diverged'])}; свидетель {c3['witness']}")
        for x in c3["diverged"][:5]:
            print("   ", x)
    print(f"время: {time.time() - t0:.0f} с; папка: {root if a.keep else 'удалена'}")
    if not a.keep:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
