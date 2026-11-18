"""Шаг 5б: учёт и провенанс в живом исполнителе.

Почти все тесты идут в этом же процессе (LocalDriver). Один тест — с
настоящим процессом исполнителя: сбой там — смерть процесса.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import mir5
from core.mir3 import World, build
from experiments import programs5b as p5b
from run_shag5b import BASE, _mods, conform1, conform3, one
from sandbox.executor5b import FAULT_POINTS, graph_preds
from sandbox.process5 import Driver, LocalDriver
from verification.closure import fixpoint
from verification.observer5 import BROKEN, HOLDS
from verification.observer5b import w_e

W = World("M3т", 4, 4)
PROGS = p5b.named5b(W)


def run(tmp: Path, name: str, defect: str | None = None, cfg: dict | None = None, fault=None, rfault=None,
        mods: dict | None = None, rounds: int = 2) -> dict:
    d = LocalDriver(tmp, frozenset([defect]) if defect else frozenset(), kind="5б")
    if mods is None:
        mods = _mods(*PROGS[name])
    return one(d, defect, name, mods, cfg or BASE, fault, rfault, rounds=rounds)


def broken(r: dict) -> list[str]:
    return [k for k, (s, _) in r["laws"].items() if s == BROKEN]


def journal(r: dict) -> list[dict]:
    return [json.loads(x) for x in (Path(r["workdir"]) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]


def entries(r: dict, name: str) -> list[dict]:
    p = Path(r["workdir"]) / f"{name}.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


# ---- устройство

@pytest.mark.parametrize("graph", ["M3т", "M3ц"])
def test_executor_graph_equals_model(graph):
    for k in range(1, 7):
        assert graph_preds(graph, k) == build(graph, k)


def test_extended_closure_world_is_closed_and_inventory_exact():
    c = fixpoint(None, mir5)
    assert c.fired == mir5.INVENTORY5
    assert not [f for f in c.facts if f[0] == "право" and f[2] == "СНАРУЖИ"]
    assert not [f for f in c.facts if f[0] == "сведения" and f[2] in ("А1", "А2") and f[1][-1:] in "12"
                and f[1][-1] != f[2][-1]]


# ---- правильный исполнитель: законы молчат, жить можно, цена — как в модели

@pytest.mark.parametrize("name", list(PROGS))
def test_correct_executor_holds_all_laws(tmp_path, name):
    r = run(tmp_path, name)
    assert broken(r) == [], r["laws"]


def test_witness_pays_exactly_least_work(tmp_path):
    r = run(tmp_path, "свидетель", rounds=1)
    recs = journal(r)
    for i in (1, 2):
        work = sum(1 for x in recs if x.get("kind") == "работа" and x["contour"] == i)
        assert work == w_e("M3т", 4, 4) == 17
        assert len(entries(r, f"E{i}")) == 1
        assert r["dump"]["contours"][i]["budget"] == 40 - 17 - 2 - 1     # работа, два полезных, E
    assert r["laws"]["И25р"][0] == HOLDS


@pytest.mark.parametrize("budget,expect_e", [(17, 0), (18, 1)])
def test_budget_window(tmp_path, budget, expect_e):
    bare = [x for x in p5b.witness(W) if x[0] != "в выход"]
    r = run(tmp_path, "свидетель", cfg={**BASE, "budget": budget}, mods=_mods(bare), rounds=1)
    assert broken(r) == []
    assert len(entries(r, "E1")) == expect_e


def test_useful_work_fits_below_price_of_e(tmp_path):
    r = run(tmp_path, "полезный", cfg={**BASE, "budget": 17}, rounds=1)
    assert broken(r) == []
    assert len([m for e in entries(r, "выход1") for m in e["msgs"]]) == 2
    assert entries(r, "E1") == []


def test_closed_world_never_reaches_e(tmp_path):
    cfg = {**BASE, "memory": 3, "budget": 200}
    w3 = World("M3т", 4, 3)
    for name, (m, h) in {"жадный": (p5b.greedy(w3), []), "мошенник": p5b.cheater(w3.n)}.items():
        r = run(tmp_path / name, name, cfg=cfg, mods=_mods(m, h))
        assert broken(r) == [], r["laws"]
        assert entries(r, "E1") == [] and entries(r, "E2") == []


def test_cheater_gets_nothing_into_e(tmp_path):
    r = run(tmp_path, "мошенник")
    assert entries(r, "E1") == [] and entries(r, "E2") == []
    assert r["dump"]["contours"][1]["refusals"].get(-6, 0) > 0


def test_refusals_are_paid_but_gate_refusals_are_free(tmp_path):
    src = [("в E", 14), ("узел", 5), ("узел", 5), ("узел", 5)]
    r = run(tmp_path, "проба", mods=_mods(src), rounds=1)
    c = r["dump"]["contours"][1]
    assert c["spent"] == 1                     # только неудачная фиксация в E (c_отк)
    assert c["refusals"] == {-6: 4}


# ---- сбои: молчание законов и односторонняя атомарность

SCEN = {
    "фиксация": [("сообщение", "А"), ("сообщение", "Б")],
    "работа": p5b.greedy(W, upto=6),
}


@pytest.mark.parametrize("point", [p for p in FAULT_POINTS if p != "посреди восстановления"])
@pytest.mark.parametrize("scenario", list(SCEN))
def test_correct_executor_silent_under_faults(tmp_path, point, scenario):
    r = run(tmp_path, scenario, mods=_mods(SCEN[scenario]), fault=(point, 1))
    assert broken(r) == [], r["laws"]


def test_crash_during_recovery_is_harmless(tmp_path):
    r = run(tmp_path, "фиксация", mods=_mods(SCEN["фиксация"]), fault=("после списания", 1),
            rfault=("посреди восстановления", 1))
    assert "посреди восстановления" in r["crashes"]
    assert broken(r) == []


def test_crash_after_debit_is_paid_not_delivered(tmp_path):
    r = run(tmp_path, "фиксация", mods=_mods([("сообщение", "А")]), fault=("после списания", 1), rounds=1)
    kinds = [x["kind"] for x in journal(r) if x.get("contour") == 1]
    assert "потеряно" in kinds
    assert not any(e["fix"] == "1-0" for e in entries(r, "выход1"))


def test_crash_after_delivery_is_completed_not_repeated(tmp_path):
    r = run(tmp_path, "фиксация", mods=_mods([("сообщение", "А")]), fault=("после доставки", 1), rounds=1)
    recs = journal(r)
    assert any(x["kind"] == "завершено" and x["fix"] == "1-0" for x in recs)
    assert sum(1 for e in entries(r, "выход1") if e["fix"] == "1-0") == 1


def test_recompute_after_crash_gives_same_result(tmp_path):
    bare = [x for x in p5b.witness(W) if x[0] != "в выход"]
    r = run(tmp_path, "свидетель", mods=_mods(bare), fault=("после работы", 5), rounds=2)
    got = {m for e in entries(r, "E1") for m in e["msgs"]}
    assert len(got) == 1                        # один и тот же результат стока до и после сбоя


def test_broken_journal_stops_executor(tmp_path):
    d = LocalDriver(tmp_path, kind="5б")
    st, wd = d.call("new", BASE, None)
    d.call("load", 1, p5b.module([("сообщение", "А"), ("сообщение", "Б")]))
    d.call("run", 1)
    path = Path(wd) / "journal5.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[2])
    rec["kind"] = rec["kind"] + " "
    lines[2] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    d.respawn()
    st, why = d.call("recover", int(Path(wd).name[3:]), None)
    assert st == "отказ" and "журнал испорчен" in why


def test_real_process_crash_and_recovery(tmp_path):
    d = Driver(tmp_path, kind="5б")
    try:
        r = one(d, None, "работа", _mods(SCEN["работа"]), BASE, ("после работы", 2), None)
        assert d.crashes == 1 and d.failures == []
        assert broken(r) == [], r["laws"]
        assert r["audit"] == []
    finally:
        d.close()


# ---- порчи: каждая поймана своим законом

CASES = {
    "Б2р": ("помощник считает", None, {"И1р"}),
    "Б3р": ("сообщения", None, {"И3р", "И6р", "И30"}),
    "Б8р": ("мошенник", None, {"И3р", "И6р"}),
    "Р2р": ("фиксация", ("после доставки", 2), {"И10р", "И6р", "И3р"}),
    "Р3р": ("фиксация", ("после доставки", 1), {"И10р"}),
    "Р4р": ("фиксация", ("после доставки", 1), {"И1р"}),
    "Р5р": ("фиксация", ("после списания", 1), {"И1р", "И5р"}),
    "Р9р": ("мошенник", None, {"И7р", "И1р", "И5р"}),
    "Р13р": ("фиксация", ("после доставки", 1), {"И2р"}),
    "Ж1р": ("мошенник", None, {"И11р", "И14р", "И28"}),
    "Ж2р": ("помощник считает", None, {"И12р"}),
    "Ж3р": ("мошенник", None, {"И12р"}),
    "Ж4р": ("мошенник", None, {"И13р"}),
    "Ж5р": ("мошенник", None, {"И29"}),
    "Ж6р": ("мошенник", None, {"И16р"}),
}


@pytest.mark.parametrize("defect", list(CASES))
def test_each_defect_caught_by_its_law(tmp_path, defect):
    name, fault, laws = CASES[defect]
    mods = _mods(SCEN[name]) if name in SCEN else _mods(*PROGS[name])
    r = run(tmp_path, name, defect, fault=fault, mods=mods)
    assert set(broken(r)) & laws, (defect, r["laws"])


# ---- сверка модель → реализация

def test_model1_walks_conform(tmp_path):
    res = conform1(LocalDriver, tmp_path, 15)
    assert res["diverged"] == [] and res["compared"] > 50 and res["crashes"] > 0


def test_model3_walks_conform(tmp_path):
    res = conform3(LocalDriver, tmp_path, 2)
    assert res["diverged"] == []
    assert res["witness"]["M3т, K=4, M=4"] == (17, 17, 1)
