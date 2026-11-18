"""Тесты шага 3: точные числа малого мира M3, порчи и законные сокращения.

Числа здесь — найденные поиском и перепроверенные второй реализацией
(verification/crosscheck.py), а не подобранные; при изменении правил мира
они обязаны пересчитаться, а не подгоняться.
"""

import pytest

from core.mir3 import DEFECTS, GRAPHS, World
from verification.crosscheck import min_work
from verification.pebble_search import explore, fast, witness

EXPECTED_LAWS = {
    "Ж1": {"И11", "И14"},
    "Ж2": {"И12"},
    "Ж3": {"И12"},
    "Ж4": {"И13"},
    "Ж5": {"И13", "И15"},
    "Ж6": {"И16"},
}


@pytest.mark.parametrize("graph", GRAPHS)
@pytest.mark.parametrize("levels", [2, 3, 4])
def test_three_searches_agree(graph, levels):
    for m in range(1, 7):
        w = World(graph, levels, m)
        a, b, c = fast(w).w_e, min_work(graph, levels, m), (explore(w).w_e if levels < 4 else None)
        assert a == b, (graph, levels, m, a, b)
        if levels < 4:
            assert a == c


@pytest.mark.parametrize("graph", GRAPHS)
def test_provenance_keeps_at_least_n_steps(graph):
    # уровень по журналу: каждый шаг выполнен и записан — меньше N не бывает
    for k in (3, 4):
        for m in range(1, 7):
            r = fast(World(graph, k, m))
            assert r.w_e is None or r.w_e >= World(graph, k, m).n


def test_known_numbers_stencil_k5():
    # плавный размен «память ↔ работа» на трафарете (§10 спецификации)
    got = [fast(World("M3т", 5, m)).w_e for m in (3, 4, 5)]
    assert got == [None, 57, 42]


def test_known_numbers_chain_k4():
    # сквозная цепочка: порог замкнутости, выше — почти без пересчёта
    got = [fast(World("M3ц", 4, m)).w_e for m in (4, 5, 6)]
    assert got == [None, 17, 15]


def test_ablation_without_dependencies_costs_exactly_n():
    for k in (3, 4, 5):
        for m in (2, 3, 4):
            w = World("M3а", k, m)
            assert fast(w).w_e == w.n


def test_correct_world_laws_are_silent():
    for g in GRAPHS:
        r = explore(World(g, 3, 4))
        assert r.counts == {}


@pytest.mark.parametrize("defect", DEFECTS)
def test_every_defect_is_caught_by_its_law(defect):
    r = explore(World("M3т", 3, 3, frozenset([defect])))
    assert set(r.counts) == EXPECTED_LAWS[defect]


@pytest.mark.parametrize("shortcut", ["С1", "С2"])
def test_legitimate_shortcuts_break_no_law_but_open_the_closure(shortcut):
    closed = fast(World("M3т", 4, 3))
    assert closed.w_e is None                              # правильная среда замкнута
    r = explore(World("M3т", 4, 3, frozenset([shortcut])), stop_on_e=True)
    assert r.counts == {}                                  # законы молчат: это не ошибка учёта
    assert r.w_e == 15                                     # замкнутость снята, но N по провенансу держится


def test_snapshot_outside_memory_opens_the_closure_witness():
    w = World("M3т", 4, 3, frozenset(["Ж3"]))
    work, _ = witness(w)
    assert work == w.n


def test_level_by_provenance_matters():
    # без провенанса E требует только результат стока, и работа падает
    # до «конуса» его предков — гораздо меньше N
    with_prov = fast(World("M3т", 5, 6)).w_e
    without = fast(World("M3т", 5, 6), provenance=False).w_e
    assert without < World("M3т", 5, 6).n < with_prov
