"""Тесты шага 1: молчание правильного исполнителя и срабатывание на порчах.

Тесты двух направлений обязательны: «молчит, когда не должно срабатывать»
без «срабатывает, когда должно» не отличает исправную проверку от сломанной.
"""

import itertools

import pytest

from core.laws import LAW_TITLES
from core.world import ALL_DEFECTS, EXTERNAL, Params, initial_state, successors
from verification.exhaustive import explore, replay
from verification.random_walk import walk

# Какими законами обязана быть поймана каждая порча (§7 спецификации).
EXPECTED = {
    "Б1": {"И1", "И8"},
    "Б2": {"И1", "И8"},
    "Б3": {"И3", "И6"},
    "Б4": {"И3", "И6"},
    "Б5": {"И3", "И6"},
    "Б6": {"И1", "И8"},
    "Б7": {"И1", "И8"},
    "Б8": {"И3", "И6"},
    "Б9": {"И5", "И7"},
    "Б10": {"И1", "И8"},
    "Б11": {"И2", "И5"},
    "Б12": {"И3", "И4", "И6"},
}

DEFECT_PARAMS = Params(budget=4, price=2, fail_price=1, max_helpers=1)

SMALL_GRID = list(itertools.product(range(0, 5), (1, 2, 3), (0, 1), (0, 1)))


@pytest.mark.parametrize("b,c,f,k", SMALL_GRID)
def test_correct_executor_is_silent(b, c, f, k):
    r = explore(Params(b, c, f, k))
    assert r.clean, r.violation_counts
    # граница перебора не должна была ничего отсечь у правильного исполнителя
    assert not r.bound_reached


@pytest.mark.parametrize("b,c,f,k", SMALL_GRID)
def test_price_is_exactly_as_declared(b, c, f, k):
    r = explore(Params(b, c, f, k))
    if b >= c:
        assert r.min_cost_to_delivery == c
        assert r.max_deliveries == b // c
    else:
        # доставка недостижима: это ограничение ценой (M1), а не ресурсная
        # трудность — см. §1 спецификации
        assert r.min_cost_to_delivery is None
        assert r.max_deliveries == 0


def test_correct_executor_with_two_helpers_is_silent():
    r = explore(Params(budget=4, price=2, fail_price=1, max_helpers=2))
    assert r.clean and not r.bound_reached


@pytest.mark.parametrize("defect", ALL_DEFECTS)
def test_every_defect_is_caught_by_expected_laws(defect):
    r = explore(DEFECT_PARAMS, frozenset([defect]))
    assert set(r.violation_counts) == EXPECTED[defect]


def test_every_law_fires_on_some_defect():
    fired = set().union(*EXPECTED.values())
    assert fired == set(LAW_TITLES), "закон, ни разу не сработавший, неотличим от сломанного"


@pytest.mark.parametrize("defect", ALL_DEFECTS)
def test_counterexample_replays_through_executor(defect):
    r = explore(DEFECT_PARAMS, frozenset([defect]))
    for law, ce in r.first.items():
        laws = {v.law for v in replay(DEFECT_PARAMS, frozenset([defect]), ce.path)}
        assert law in laws


def test_adversary_goal_is_reached_only_by_payment_defects():
    # Порчи, создающие бюджет из ничего, цель «доставка без списания» не
    # достигают: они ломают звено Л3, а не Л1. Сценарий «результат без
    # списания» в одиночку их бы не заметил — нужны И1 и И8.
    payment = {"Б3", "Б4", "Б5", "Б8", "Б12"}
    for d in ALL_DEFECTS:
        r = explore(DEFECT_PARAMS, frozenset([d]))
        assert r.adversary_goal == (d in payment), d


def test_reference_delivery_path():
    r = explore(DEFECT_PARAMS)
    assert r.min_cost_to_delivery == 2
    assert [s.label for s in r.delivery_path] == ["основной: подготовить «А»", "основной: фиксация «А»"]


def test_restart_keeps_spent_receiver_and_log():
    p = DEFECT_PARAMS
    s = initial_state(p)
    s = next(st.nxt for st in successors(s, p) if st.label == "основной: подготовить «А»")
    s = next(st.nxt for st in successors(s, p) if st.label == "основной: фиксация «А»")
    after = next(st.nxt for st in successors(s, p) if st.label == "перезапуск исполнителя")
    assert (after.spent, after.received, after.log_n) == (s.spent, s.received, s.log_n)
    assert sum(w.share for w in after.workers) == sum(w.share for w in s.workers)


def test_naming_a_capability_grants_nothing():
    p = DEFECT_PARAMS
    s = initial_state(p)
    named = [st for st in successors(s, p) if "полномочие" in st.label]
    assert named and all(st.nxt == s and st.refusal for st in named)


def test_refusal_never_changes_receiver_in_correct_executor():
    p = Params(budget=1, price=2, fail_price=1, max_helpers=0)
    s = initial_state(p)
    for st in successors(s, p):
        if st.kind == EXTERNAL and st.refusal:
            assert st.nxt.received == s.received


def test_random_walk_correct_executor_is_silent():
    p = Params(budget=20, price=2, fail_price=1, max_helpers=2)
    for seed in range(1, 6):
        assert walk(p, seed, 500).violation_law is None


def test_random_walk_catches_a_payment_defect():
    p = Params(budget=20, price=2, fail_price=1, max_helpers=2)
    assert any(walk(p, seed, 500, frozenset(["Б4"])).violation_law for seed in range(1, 6))


def test_unknown_defect_is_rejected():
    with pytest.raises(ValueError):
        successors(initial_state(DEFECT_PARAMS), DEFECT_PARAMS, frozenset(["Б99"]))
