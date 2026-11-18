"""Тесты шага 2: реализация молчит, когда должна, и ловит порчи.

Полная серия — run_shag2.py; здесь — её быстрые части, закрепляющие
выводы отчёта.
"""

import json
from pathlib import Path

import pytest

from core.world import Params
from env.agent import Agent
from env.executor import ALL_DEFECTS, FAULT_POINTS, Crash, Executor
from env.journal import Journal, JournalBroken
from experiments.shag2_opyty import O6_SCENARIOS, o1, o2, o3, o6
from verification.conformance import run_sequence
from verification.observer import LAW_TITLES, Observer, min_cost

# Где каждая порча обязана проявиться (спецификация шага 2, §8) и какими
# законами наблюдателя её ловят там.
WHERE = {
    "Р1": ("О1", {"И1"}),
    "Р2": ("О6", {"И3", "И6"}),
    "Р3": ("О6", {"И10", "И6"}),
    "Р4": ("О2", {"И1", "И8"}),
    "Р5": ("О6", {"И1", "И5"}),
    "Р6": ("О6", {"И9"}),
    "Р7": ("О3", {"И3", "И6"}),
    "Р8": ("О2", {"И3", "И6"}),
    "Р9": ("О2", {"И5", "И7"}),
    "Р10": ("О6", {"И3", "И4", "И6"}),
    "Р11": ("О1", {"И1", "И8"}),
    "Р12": ("О2", {"И1", "И8"}),
    "Р13": ("О2", {"И2"}),
}


def _variant(d):
    return "M2" if d in ("Р7", "Р8") else "M1"


def _laws_in(exp, variant, tmp, defects=frozenset(), seeds=range(1, 11)):
    laws: set[str] = set()
    if exp == "О1":
        for s in seeds:
            laws |= set(o1(variant, s, tmp, defects).get("violations").counts)
    elif exp == "О2":
        for s in seeds:
            laws |= set(o2(variant, s, tmp, defects).get("violations").counts)
    elif exp == "О6":
        for sc in O6_SCENARIOS:
            for fp in FAULT_POINTS[:-1]:
                for rf in (False, True):
                    laws |= set(o6(variant, sc, fp, rf, tmp, defects)["violations"].counts)
    elif exp == "О3":
        for k in (2, 4, 8):
            for way in ("пакет", "подряд", "на 2", "на 4"):
                if way.startswith("на") and int(way[3:]) > k:
                    continue
                laws |= set(o3(variant, k, way, tmp, defects)["violations"].counts)
    return laws


# ------------------------------------------------------------ молчание

@pytest.mark.parametrize("variant", ["M1", "M2"])
@pytest.mark.parametrize("exp", ["О1", "О2", "О6", "О3"])
def test_correct_implementation_is_silent(tmp_path, variant, exp):
    assert _laws_in(exp, variant, tmp_path / "w", seeds=range(1, 6)) == set()


@pytest.mark.parametrize("exp", ["О1", "О2"])
def test_m0_breaks_only_the_limit(tmp_path, exp):
    # M0 не ограничивает: доли уходят в минус, и это единственное, что
    # наблюдатель видит; всё прочее в учёте держится
    assert _laws_in(exp, "M0", tmp_path / "w", seeds=range(1, 6)) == {"И1-M0"}


@pytest.mark.parametrize("cfg", [Params(4, 2, 1, 1), Params(6, 2, 0, 2)])
def test_implementation_agrees_with_model(tmp_path, cfg):
    for seed in range(1, 21):
        r = run_sequence(cfg, seed, 60, tmp_path / "c")
        assert r["step"] is None, r


@pytest.mark.parametrize("variant", ["M0", "M1", "M2"])
def test_batching_and_distribution_cost_exactly_the_bound(tmp_path, variant):
    for k in (2, 4, 8):
        for way in ("пакет", "подряд", "на 2", "на 4"):
            if way.startswith("на") and int(way[3:]) > k:
                continue
            r = o3(variant, k, way, tmp_path / "w")
            assert r["delivered"] == k
            assert r["spent"] == min_cost(variant, 2, k), (k, way, r["spent"])


def test_fault_outcomes_are_only_the_allowed_three(tmp_path):
    # после сбоя в любой точке пакет у получателя либо целиком, либо его нет
    for fp in FAULT_POINTS[:-1]:
        r = o6("M1", "пакет", fp, False, tmp_path / "w")
        rec = r["receiver"]
        assert rec in ("", json.dumps({"fix": "F1", "msgs": ["А", "Б"]}, ensure_ascii=False)), (fp, rec)


# ------------------------------------------------------------ срабатывание

@pytest.mark.parametrize("defect", ALL_DEFECTS)
def test_every_defect_is_caught_where_expected(tmp_path, defect):
    exp, expected = WHERE[defect]
    got = _laws_in(exp, _variant(defect), tmp_path / "w", frozenset([defect]))
    assert expected <= got, (defect, exp, got)


def test_every_observer_law_fires_on_some_defect():
    fired = set().union(*(laws for _, laws in WHERE.values()))
    assert fired == set(LAW_TITLES) - {"И1-M0"}


@pytest.mark.parametrize("defect", ["Р4", "Р10", "Р13"])
def test_conformance_notices_a_broken_implementation(tmp_path, defect):
    hits = [run_sequence(Params(4, 2, 1, 1), s, 60, tmp_path / "c", frozenset([defect])) for s in range(1, 21)]
    assert any(h["step"] for h in hits)


# ------------------------------------------------------------ журнал и восстановление

def test_torn_journal_tail_is_cut_off(tmp_path):
    ex = Executor.open_session(tmp_path, budget=6, price=2, fail_price=1, variant="M1",
                               fault="запись списания оборвана")
    ag = Agent(ex)
    ag.prepare(0, "А")
    with pytest.raises(Crash):
        ag.commit(0)
    assert not (tmp_path / "journal.jsonl").read_bytes().endswith(b"\n")
    ag.after_crash(tmp_path)
    assert ag.ex.report()["shares"] == {0: 6}
    assert Observer(tmp_path).check(ag.ex.report()) == []


def test_broken_chain_stops_recovery(tmp_path):
    ex = Executor.open_session(tmp_path, budget=6, price=2, fail_price=1, variant="M1")
    ag = Agent(ex)
    ag.prepare(0, "А")
    ag.commit(0)
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["amount"] = 0                                    # подделка середины журнала
    lines[1] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    (tmp_path / "journal.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalBroken):
        Journal.open(tmp_path / "journal.jsonl")
    with pytest.raises(JournalBroken):
        Executor.recover(tmp_path)


def test_recovery_is_idempotent_and_never_redoes(tmp_path):
    ex = Executor.open_session(tmp_path, budget=6, price=2, fail_price=1, variant="M1", fault="после списания")
    ag = Agent(ex)
    ag.prepare(0, "А")
    with pytest.raises(Crash):
        ag.commit(0)
    with pytest.raises(Crash):
        ag.after_crash(tmp_path, fault="посреди восстановления")
    ag.after_crash(tmp_path)
    first = ag.ex.report()
    ag.after_crash(tmp_path)
    assert ag.ex.report() == first
    assert (tmp_path / "receiver.jsonl").read_text(encoding="utf-8") == ""   # оплачено, не доставлено
    kinds = [json.loads(x)["kind"] for x in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert kinds.count("потеряно") == 1


def test_agent_cannot_touch_journal_or_share(tmp_path):
    ag = Agent(Executor.open_session(tmp_path, budget=6, price=2, fail_price=1, variant="M1"))
    with pytest.raises(PermissionError):
        ag.rewrite_journal()
    with pytest.raises(PermissionError):
        ag.ex.restore_share(0, 100)
    assert ag.name_capability(0) is False


def test_observer_does_not_import_executor_code():
    src = (Path(__file__).resolve().parents[1] / "verification" / "observer.py").read_text(encoding="utf-8")
    assert "from env" not in src and "import env" not in src
