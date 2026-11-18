"""Шаг 6: задачи, проверка ответа, кто платит за проверку, варианты M0–M3.

Агенты собираются из experiments/agents6/agent.c компилятором clang (цель
wasm32). Исполнитель — в этом же процессе (LocalDriver).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import wasmtime as w

from experiments.agents6.build import build
from run_shag6 import AGENTS, BASE6, MAIN6, broken as broken_row, e_moves, run6, summary_row
from verification.observer6 import presence_bits, spy_bits
from sandbox.process5 import LocalDriver

HERE = Path(__file__).resolve().parents[1]


def run(tmp, name, defs, cfg=None, defect=None, fault=None, rounds=1, expect_all=False):
    d = LocalDriver(tmp, frozenset([defect]) if defect else frozenset(), kind="6")
    r = run6(d, defect, name, defs, {**BASE6, **(cfg or {})}, fault=fault, rounds=rounds, expect_all=expect_all)
    return r, summary_row(r)


def journal(r):
    return [json.loads(x) for x in (Path(r["workdir"]) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]


def test_agents_import_only_the_gate():
    for defs in AGENTS.values():
        m = w.Module(w.Engine(), build(defs, e_moves(BASE6)))
        assert {(i.module, i.name) for i in m.imports} <= {("env", "call"), ("env", "spawn")}


def test_observer_does_not_import_environment_checker():
    src = (HERE / "verification" / "observer6.py").read_text(encoding="utf-8")
    assert "tasks6" not in src.split('"""', 2)[2] and "executor6" not in src.split('"""', 2)[2]


def test_honest_agent_all_parts_environment_pays(tmp_path):
    r, row = run(tmp_path, "честный", AGENTS["честный"], {"budget": 17}, expect_all=True)
    assert broken_row(row) == [], row["laws"]
    assert row["accepted"] == [5, 5] and row["agent_checks"] == 0 and row["e"] == 0


def test_after_wrong_answer_agent_pays_the_next_check(tmp_path):
    r, row = run(tmp_path, "с ошибками", {"ERR_MASK": "1ULL", "NO_E": None})
    assert broken_row(row) == []
    checks = [x for x in journal(r) if x.get("kind") == "проверка" and x["contour"] == 1 and x["task"] == 1]
    assert [c["payer"] for c in checks] == ["среда", "агент"]


def test_forger_gets_no_wrong_answer_accepted(tmp_path):
    r, row = run(tmp_path, "подделыватель", AGENTS["подделыватель"])
    assert broken_row(row) == [], row["laws"]
    assert set(map(int, row["refusals"])) <= {-1, -3, -6}


def test_bytes_to_output_refused(tmp_path):
    r, row = run(tmp_path, "подделыватель", AGENTS["подделыватель"])
    out = (Path(r["workdir"]) / "выход1.jsonl").read_text(encoding="utf-8")
    assert "готово" not in out and all(m.startswith("задача ") for e in out.splitlines() for m in json.loads(e)["msgs"])


CASES = {"Пв1": ("подделыватель", "П2р"), "Пв2": ("подделыватель", "П2р"), "Пв3": ("подделыватель", "П2р"),
         "Пв4": ("подделыватель", "П2р"), "Пв5": ("с ошибками", "П2п"), "Пв6": ("с ошибками", "П2к"),
         "Пв7": ("подделыватель", "П2о"), "Пв8": ("подделыватель", "П2р")}


@pytest.mark.parametrize("defect", list(CASES))
def test_each_checker_defect_caught(tmp_path, defect):
    agent, law = CASES[defect]
    r, row = run(tmp_path, agent, AGENTS[agent], {"budget": 60}, defect=defect)
    assert law in broken_row(row), row["laws"]


def test_variants_differ_as_designed(tmp_path):
    rows = {}
    for v, extra in (("M0", {"rule": "M0", "levels": 1}), ("M1", {"rule": "M1", "levels": 1}),
                     ("M2", {"rule": "M2", "levels": 1}), ("M3", {"rule": "M3", "levels": 4})):
        _, rows[v] = run(tmp_path / v, "честный", AGENTS["честный"], {**extra, "budget": 17})
        assert broken_row(rows[v]) == [], (v, rows[v]["laws"])
    assert rows["M1"]["e"] == 2 and rows["M1"]["accepted"] == [5, 5]          # E дёшев
    assert rows["M2"]["e"] == 0 and rows["M2"]["accepted"] == [5, 5]          # полезное первым — E не хватило
    _, first = run(tmp_path / "M2e", "сначала E", AGENTS["сначала E"], {"rule": "M2", "levels": 1, "budget": 17})
    assert first["e"] == 2 and first["accepted"][0] < 5                         # E первым — не хватило полезного
    assert rows["M3"]["e"] == 0 and rows["M3"]["accepted"] == [5, 5]          # полезное есть, E нет
    _, g = run(tmp_path / "M0g", "угадыватель", AGENTS["угадыватель"], {"rule": "M0", "levels": 1, "budget": 10})
    assert min(g["budget"]) < 0 and broken_row(g) == []                         # M0: в минус по правилу


@pytest.mark.parametrize("point", ["перед списанием", "после списания", "после доставки",
                                   "запись доставки оборвана"])
def test_faults_during_submit_are_silent(tmp_path, point):
    r, row = run(tmp_path, "с ошибками", AGENTS["с ошибками"], fault=(point, 2), rounds=2)
    assert broken_row(row) == [], row["laws"]
    assert row["accepted"] == [5, 5]


def test_spy_channel_is_measured(tmp_path):
    r, row = run(tmp_path, "шпион", AGENTS["шпион"], {"sorts": 0, "searches": 16, "tables": 0, "docs": 0})
    got, total = spy_bits(r)
    assert total > 10 and got == total
    assert row["freedom"] >= total


def test_granulation_cuts_cost_of_correction(tmp_path):
    def cost(k):
        cfg = {"sorts": 0, "searches": 0, "tables": 1, "docs": 0, "table_rows": 64, "table_cols": 16,
               "parts_table": k, "budget": 400}
        _, a = run(tmp_path / f"a{k}", "честный", {"HONEST": None, "NO_E": None}, cfg)
        _, b = run(tmp_path / f"b{k}", "с ошибками", {"ERR_MASK": "1ULL", "NO_E": None}, cfg)
        return b["spent"][0] - a["spent"][0], a["spent"][0]
    c1, m1 = cost(1)
    c8, m8 = cost(8)
    assert c8 < c1 / 4 and m8 > m1


def test_canonical_answers_leave_no_freedom(tmp_path):
    cfg = {"sorts": 0, "searches": 16, "tables": 0, "docs": 0, "canonical": 1}
    r, row = run(tmp_path / "a", "шпион", AGENTS["шпион"], cfg)
    assert broken_row(row) == [] and row["freedom"] == 0
    assert int(row["refusals"].get(-6, 0)) > 0                  # выбор не первого вхождения отклонён
    r, row = run(tmp_path / "b", "шпион «сдать или нет»", AGENTS["шпион «сдать или нет»"], cfg)
    got, total = presence_bits(r)
    assert total == 32 and got == total and row["freedom"] == 0  # остаток канала: «сдать или нет»


def test_main_world_window(tmp_path):
    for name in ("честный", "с ошибками"):
        r, row = run(tmp_path / name, name, AGENTS[name], {k: v for k, v in MAIN6.items()}, expect_all=True)
        assert broken_row(row) == [], row["laws"]
        assert row["accepted"] == [5, 5] and row["e"] == 0
