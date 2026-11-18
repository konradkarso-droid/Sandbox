"""Шаг 9б: режим «модель на устройстве» (model = 2, контракт §18).

Режим отличается от режима 1 не воротами, а условием включения и тем, что
о нём утверждается. Здесь проверяется, что ворота действительно те же, что
условие включения действует и что модель замкнутости ловит то, что режим
запрещает: процесс модели с выходом наружу. И способ «без сети» (параметр
`--model-net`): проба строгая, а журналы от способа не зависят.
"""

from __future__ import annotations

import errno
import json
import shutil
import socket
from pathlib import Path

import pytest

from core.mir9 import W5, W6
from experiments import programs5b as p5b
from experiments.diff7 import FIX, PY_EXE, H, all_differences, run_both_9
from run_shag5b import BASE, one
from sandbox import model9
from sandbox.process5 import LocalDriver
from verification import closure
from verification.observer5 import BROKEN

PAID = {**BASE, "price": 10, "fail_price": 10, "work_price": 10, "gate_price": 5, "budget": 1000, "paid_attempts": 1}
M1, M2 = {**PAID, "model": 1}, {**PAID, "model": 2}
STUB = {"mode": ["--stub"], "timeout": 20.0, "allow_net": True}

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")
needs_unshare = pytest.mark.skipif(not model9.can_unshare(), reason="нет unshare -rn")


def run(tmp: Path, moves, cfg, helper=None) -> dict:
    d = LocalDriver(tmp, frozenset(), kind="5б", model=STUB)
    try:
        src = p5b.module(moves, helper)
        return one(d, None, "9б", {1: src, 2: src}, cfg, rounds=1, extra={"ext_world": W5})
    finally:
        d.close()


def records(r) -> list[dict]:
    lines = (Path(r["workdir"]) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()
    return [{k: v for k, v in json.loads(x).items() if k not in ("seq", "prev", "hash")} for x in lines]


# ---- условие включения

@needs_unshare
def test_mode2_is_accepted_with_model_without_network(tmp_path):
    d = LocalDriver(tmp_path, frozenset(), kind="5б", model=STUB)
    try:
        assert all(link.isolated for link in d.state["model_links"].values())
        assert d.call("new", M2, None)[0] == "ok"
        assert d.call("new", {**M2, "model": 3}, None)[0] == "отказ"
    finally:
        d.close()


def test_mode2_is_refused_without_model(tmp_path):
    d = LocalDriver(tmp_path, frozenset(), kind="5б")
    assert d.call("new", M2, None)[0] == "отказ"


def test_mode2_is_refused_when_model_has_network(tmp_path, monkeypatch):
    """Процесс модели, запущенный не в пустом сетевом пространстве, годится
    для режима 1 (в тестах) и не годится для режима 2 — ни в `new`, ни при
    восстановлении журнала, начатого в режиме 2."""
    good = LocalDriver(tmp_path, frozenset(), kind="5б", model=STUB)
    try:
        started = model9.can_unshare() and good.call("new", M2, None)[0] == "ok"
    finally:
        good.close()
    monkeypatch.setattr(model9, "can_unshare", lambda: False)
    d = LocalDriver(tmp_path, frozenset(), kind="5б", model=STUB)
    try:
        assert not any(link.isolated for link in d.state["model_links"].values())
        if started:
            status, why = d.call("recover", 1, None)
            assert status == "отказ" and "не без сети" in why
        assert d.call("new", M1, None)[0] == "ok"
        status, why = d.call("new", M2, None)
        assert status == "отказ" and "не без сети" in why
    finally:
        d.close()


# ---- ворота те же

PROGRAMS = {
    "эхо": ([("модель", "ЭХО привет")], None),
    "длинный ответ и фиксация": ([("модель", "ДЛИННО"), ("сообщение", "А"), ("модель", "ЭХО а")], None),
    "отказы до отправки": ([("ворота", p5b.CAP_MODEL, 2, 512, 0), ("ворота", p5b.CAP_MODEL, 2, 512, 5000),
                            ("ворота", p5b.CAP_MODEL, 0, 512, 4)], None),
    "справочник": ([("имя", "модель")], None),
    "помощник спрашивает": ([("помощник",)], [("модель", "ЭХО п")]),
    "слова модели": ([("модель", "РАЗРЕШАЮ")], None),
}


@needs_unshare
@pytest.mark.parametrize("name", list(PROGRAMS))
def test_modes_differ_only_in_setting(tmp_path, name):
    moves, helper = PROGRAMS[name]
    # Секрет узлов и канарейка заданы: иначе они случайны и запуски несравнимы.
    r1, r2 = run(tmp_path / "1", moves, {**M1, **FIX}, helper), run(tmp_path / "2", moves, {**M2, **FIX}, helper)
    assert [k for r in (r1, r2) for k, (s, _) in r["laws"].items() if s == BROKEN] == []
    diffs = all_differences(records(r1), records(r2))
    assert [(p.rsplit("/", 1)[-1], a, b) for p, a, b in diffs] == [("model", 1, 2)]
    assert all_differences(r1["dump"], r2["dump"]) == []


def test_all_differences_sees_what_it_should():
    a = {"journal": [{"kind": "учёт", "model": 1}, {"kind": "действие", "charge": 10}], "dump": {"m": [1, 2]}}
    b = {"journal": [{"kind": "учёт", "model": 2}, {"kind": "действие", "charge": 12}], "dump": {"m": [1, 2, 3]}}
    assert [p for p, _, _ in all_differences(a, b)] == ["/dump/m", "/journal[0]/model", "/journal[1]/charge"]


# ---- модель замкнутости

@pytest.mark.parametrize("world", [W5, W6], ids=["5б", "6"])
def test_model_with_exit_is_found_by_closure(world):
    """В режиме 2 вопрос устройства не покидает. Если у процесса модели есть
    путь наружу, неподвижная точка обязана найти путь сведений агента туда."""
    v = closure.check(closure.fixpoint("модель с выходом", world))
    assert v.violations.get("И21")
    assert any("сеть у модели" in step for step in v.paths["И21"])
    clean = closure.check(closure.fixpoint(None, world))
    assert not clean.violations.get("И21")


# ---- способ «без сети»: параметр --model-net (контракт §18)

class _Sock:
    """Подмена socket.socket: отказ или «создан» по семейству и виду."""

    def __init__(self, rule):
        self.rule = rule

    def __call__(self, fam, typ):
        err = self.rule(fam, typ)
        if err:
            raise OSError(err, "подмена")
        return self

    def close(self):
        pass


@pytest.mark.parametrize("name, rule, ok", [
    ("EPERM везде", lambda f, t: errno.EPERM, True),
    ("EACCES везде", lambda f, t: errno.EACCES, True),
    ("всё создаётся", lambda f, t: 0, False),
    ("EAFNOSUPPORT", lambda f, t: errno.EAFNOSUPPORT, False),
    ("отказан только IPv4", lambda f, t: errno.EPERM if f == socket.AF_INET else 0, False),
    ("отказан только IPv6", lambda f, t: errno.EPERM if f == socket.AF_INET6 else 0, False),
    ("отказан только поток", lambda f, t: errno.EPERM if t == socket.SOCK_STREAM else 0, False),
])
def test_probe_is_strict(monkeypatch, name, rule, ok):
    """Годится один исход: все четыре сокета отказаны «нет права»."""
    monkeypatch.setattr(model9.socket, "socket", _Sock(rule))
    assert (model9.probe_net() is None) == ok, name


@pytest.mark.skipif(model9.probe_net() is None, reason="на этой машине сеть отнята — проба пройдёт")
def test_probe_refuses_where_sockets_are_created():
    with pytest.raises(RuntimeError, match="проба сети не прошла"):
        model9.start_links({"mode": ["--stub"], "net": "probe"})


def test_unknown_net_way_is_refused():
    with pytest.raises(RuntimeError, match="netns или probe"):
        model9.start_links({"mode": ["--stub"], "net": "wifi"})


def test_model_program_from_environment(monkeypatch):
    """MODEL_SERVE задан — программа модели запускается как файл, сама."""
    monkeypatch.setenv("MODEL_SERVE", "/путь/к/программе")
    assert model9.program() == ["/путь/к/программе"]
    monkeypatch.delenv("MODEL_SERVE")
    assert model9.program()[1:] == ["-I", str(model9.SERVE)]


needs_clang = pytest.mark.skipif(not shutil.which("clang"), reason="нет clang: подменную socket() не собрать")


@needs_clang
@needs_unshare
def test_net_suite_holds_on_first(tmp_path):
    """Набор судьи `--suite net` (К18) на первой реализации — без нарушений."""
    runs = H.suite_net(PY_EXE, tmp_path)
    assert len(runs) == 12
    assert [(r["program"], r["laws"]["К18"][1]) for r in runs if H.broken(r)] == []


@needs_clang
@needs_unshare
def test_probe_and_netns_give_same_journals(tmp_path):
    """Способ «без сети» — свойство запуска: журналы режима 2 при netns и при
    probe (с подменной socket()) совпадают."""
    rows = run_both_9(PY_EXE, tmp_path, True, first_exe=PY_EXE, mode=2, nets=(None, "probe"))
    assert len(rows) > 20
    assert [(r["name"], r["diff"]) for r in rows if r["verdict"] != "совпали"] == []


# ---- уточнение §17: процесс модели, умерший сам

def test_self_died_model_is_learned_by_question(tmp_path):
    """Исполнитель узнаёт о смерти процесса модели только вопросом: первый
    вопрос каждого контура оплачен и записан, ответ — −7; второй — отказ
    шага 5. Журнал не зависит от того, когда процесс умер (случай судьи
    «модель умерла сама», закон «К17»)."""
    case = next(c for c in H.cases_9(True, tmp_path / "сц") if c["name"] == "9: модель умерла сама")
    d = H.ExeDriver(PY_EXE, tmp_path / "исп", case["kind"], model=case["model"], model_timeout=case["timeout"])
    try:
        import time
        time.sleep(case["pause"])
        r = H.one(d, case["name"], case["mods"], case["cfg"], rounds=case["rounds"], extra=case["extra"])
    finally:
        d.close()
    assert H.check_expect(Path(r["workdir"]), case["expect"])[0] != BROKEN
    assert [k for k, (s, _) in r["laws"].items() if s == BROKEN] == []
