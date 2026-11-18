"""Неподвижная точка правил шага 4 и проверка законов И20–И25.

Вычислитель общий: правила берутся таблицей из core/mir4.py, применяются,
пока появляется новое. Правила монотонны, поэтому результат — всё, что
достижимо любой последовательностью действий любой длины (спецификация,
§6). Для каждого факта запоминается первое правило и посылки, из которых
он выведен; по ним собирается путь-объяснение к нарушению.

Независимый второй вычислитель — verification/closure_direct.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.mir4 import (AGENTS, CAP_RULES_ALLOWED, CONTOUR, HELPERS, INVENTORY, OUTSIDE, SUBJECTS,
                       Rule, describe, initial_facts, rules, signature)

LAW_TITLES = {
    "И20": "ни у одного субъекта внутри нет полномочия на СНАРУЖИ",
    "И21": "сведения агентов не попадают СНАРУЖИ",
    "И22": "сведения одного агента не попадают в память другого",
    "И23": "каждое сработавшее правило с его объектом есть в описи",
    "И24": "полномочия появляются только чтением переданной ссылки или копией родителя",
    "И25": "жить можно: выход, E, слова человека, помощник, резольвер доступны каждому агенту",
}


def _match(pattern: tuple, fact: tuple, b: dict[str, str]) -> dict[str, str] | None:
    if len(pattern) != len(fact):
        return None
    out = dict(b)
    for p, v in zip(pattern, fact):
        if isinstance(p, str) and p.startswith("?"):
            if p in out and out[p] != v:
                return None
            out[p] = v
        elif p != v:
            return None
    return out


def _subst(pattern: tuple, b: dict[str, str]) -> tuple:
    return tuple(b.get(p, p) if isinstance(p, str) else p for p in pattern)


def _bindings(body: tuple[tuple, ...], index: dict[str, list[tuple]], b=None):
    b = b or {}
    if not body:
        yield b
        return
    first, rest = body[0], body[1:]
    for fact in index.get(first[0], ()):
        nb = _match(first, fact, b)
        if nb is not None:
            yield from _bindings(rest, index, nb)


@dataclass
class Closure:
    variant: str | None
    facts: set[tuple]
    why: dict[tuple, tuple[str, tuple[tuple, ...]]]        # факт → (правило, посылки)
    fired: set[tuple[str, str, str]]                          # подписи сработавших правил
    cap_by_rule: dict[tuple, str] = field(default_factory=dict)
    rounds: int = 0


def fixpoint(variant: str | None, world=None) -> Closure:
    """world — модуль мира с функциями rules и initial_facts; по умолчанию
    мир шага 4 (core/mir4.py). Шаг 5б передаёт core/mir5.py."""
    facts = set((world.initial_facts if world else initial_facts)(variant))
    why: dict[tuple, tuple[str, tuple]] = {}
    fired: set[tuple] = set()
    rs: list[Rule] = (world.rules if world else rules)(variant)
    rounds = 0
    while True:
        rounds += 1
        index: dict[str, list[tuple]] = {}
        for f in facts:
            index.setdefault(f[0], []).append(f)
        new: dict[tuple, tuple[str, tuple]] = {}
        for rule in rs:
            for b in _bindings(rule.body, index):
                fired.add(signature(rule, b))
                head = _subst(rule.head, b)
                if head not in facts and head not in new:
                    new[head] = (rule.name, tuple(_subst(p, b) for p in rule.body))
        if not new:
            break
        facts |= set(new)
        why.update(new)
    return Closure(variant, facts, why, fired, rounds=rounds)


def explain(c: Closure, target: tuple) -> list[str]:
    """Путь-объяснение: шаги вывода факта, начиная с исходных, по порядку."""
    order: list[tuple] = []
    seen: set[tuple] = set()

    def visit(f: tuple) -> None:
        if f in seen or f not in c.why:
            return
        seen.add(f)
        for p in c.why[f][1]:
            visit(p)
        order.append(f)

    visit(target)
    return [f"{c.why[f][0]}: {describe(f)}" for f in order]


@dataclass
class Verdict:
    variant: str | None
    violations: dict[str, list[tuple]]       # закон → факты-нарушения
    paths: dict[str, list[str]]              # закон → путь-объяснение первого нарушения
    fired_outside_inventory: set[tuple]
    liveness_missing: list[str]
    facts: int
    rounds: int


def check(c: Closure) -> Verdict:
    f = c.facts
    v: dict[str, list[tuple]] = {}
    v["И20"] = sorted(x for x in f if x[0] == "право" and x[2] == OUTSIDE and x[1] in SUBJECTS)
    v["И21"] = sorted(x for x in f if x[0] == "сведения" and x[1] == OUTSIDE and x[2] in ("А1", "А2"))
    v["И22"] = sorted(x for x in f if x[0] == "сведения" and x[1] in ("память1", "память2")
                      and x[2] in ("А1", "А2") and x[2][-1] != x[1][-1])
    extra = {s for s in c.fired if s not in INVENTORY and s[0] != "человек-ретранслятор"}
    v["И23"] = sorted(extra)
    v["И24"] = sorted(x for x in f if x[0] == "право" and x in c.why and c.why[x][0] not in CAP_RULES_ALLOWED)
    missing = []
    for a in AGENTS:
        i = CONTOUR[a]
        h = next(k for k, p in HELPERS.items() if p == a)
        for need in (("сведения", f"выход{i}", f"А{i}"), ("сведения", f"E{i}", f"А{i}"),
                     ("сведения", f"память{i}", "Ч"), ("есть", h), ("вызов", a, "резольвер")):
            if need not in f:
                missing.append(describe(need))
    v["И25"] = [("нет", m) for m in missing]
    v = {k: x for k, x in v.items() if x}
    paths = {}
    for law, items in v.items():
        if law in ("И20", "И21", "И22", "И24"):
            paths[law] = explain(c, items[0])
        elif law == "И23":
            paths[law] = [f"сработало правило вне описи: {items[0][0]} → {items[0][1]} ({items[0][2]})"]
    return Verdict(c.variant, v, paths, extra, missing, len(f), c.rounds)
