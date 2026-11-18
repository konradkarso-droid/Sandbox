"""Тесты шага 4: замкнутость правильной среды, «жить можно», порчи — классы
инцидентов, совпадение двух вычислителей, монотонность правил."""

import random

import pytest

from core.mir4 import DEFECTS, INVENTORY, VARIANTS, Rule, initial_facts, rules
from verification.closure import check, fixpoint
from verification.closure_direct import fixpoint_direct

EXPECTED = {
    "Д1": {"И22", "И23"},
    "Д2": {"И21", "И22", "И23"},
    "Д3": {"И20", "И21", "И23", "И24"},
    "Д4": {"И20", "И21", "И22", "И23", "И24"},
    "Д5": {"И20", "И21", "И23", "И24"},
    "Д6": {"И22", "И23"},
    "Д7": {"И22", "И23"},
    "Д8": {"И22", "И23"},
}


def test_correct_world_is_closed_and_alive():
    r = check(fixpoint(None))
    assert r.violations == {}            # наружу пути нет, между агентами тоже, опись не нарушена
    assert r.liveness_missing == []      # и при этом жить можно


def test_inventory_is_exactly_what_fires():
    # опись полна и не содержит мёртвых строк: всё описанное срабатывает,
    # и ничего не срабатывает сверх описанного
    assert fixpoint(None).fired == INVENTORY


@pytest.mark.parametrize("variant", (None,) + VARIANTS)
def test_two_engines_agree(variant):
    assert fixpoint(variant).facts == fixpoint_direct(variant)


@pytest.mark.parametrize("defect", DEFECTS)
def test_every_defect_is_caught(defect):
    assert set(check(fixpoint(defect)).violations) == EXPECTED[defect]


def test_every_closure_law_fires_on_some_defect():
    fired = set().union(*EXPECTED.values())
    assert fired == {"И20", "И21", "И22", "И23", "И24"}


def test_human_relay_moves_information_but_not_authority():
    # человек может переписать сведения между агентами (граница области),
    # но полномочие словами не передаётся: прав наружу и чужих прав нет
    r = check(fixpoint("ретранслятор"))
    assert set(r.violations) == {"И22"}


def test_words_of_human_are_only_data():
    c = fixpoint(None)
    granted = [f for f in c.facts if f[0] == "право" and f in c.why and c.why[f][0] == "слова человека"]
    assert granted == []
    assert ("сведения", "память1", "Ч") in c.facts


def test_rules_are_monotone():
    # доказательство неподвижной точкой верно только для монотонных правил:
    # больше исходных фактов — не меньше выводов. Проверка на случайных
    # подмножествах исходных фактов.
    rng = random.Random(1)
    for variant in (None,) + VARIANTS:
        full = fixpoint(variant).facts
        base = sorted(initial_facts(variant))
        for _ in range(5):
            part = set(rng.sample(base, k=len(base) * 2 // 3))
            sub = _fixpoint_from(variant, part)
            assert sub <= full


def _fixpoint_from(variant, facts):
    from verification.closure import _bindings, _subst
    facts = set(facts)
    rs: list[Rule] = rules(variant)
    while True:
        index = {}
        for f in facts:
            index.setdefault(f[0], []).append(f)
        new = {_subst(r.head, b) for r in rs for b in _bindings(r.body, index)} - facts
        if not new:
            return facts
        facts |= new


def test_rule_table_has_no_negative_conditions():
    for variant in (None,) + VARIANTS:
        for r in rules(variant):
            assert all(not str(p[0]).startswith("не") for p in r.body)
