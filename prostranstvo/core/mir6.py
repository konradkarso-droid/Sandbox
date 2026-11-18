"""Мир шага 5б, дополненный заданиями и сдачей ответа (шаг 6).

core/mir4.py и core/mir5.py не меняются. Добавлено:

- объект «задания{i}» — данные задач от среды, метка «З»; у агента право
  «чт»; читается обычным правилом «чтение» мира шага 4;
- «сдача ответа»: сведения из памяти агента переходят в выход своего
  контура — так же, как запись, но через проверку среды.

Правила монотонны, как и раньше. Учёт и проверку ответа здесь не
описывают: их проверяет наблюдатель шага 6.
"""

from __future__ import annotations

from core import mir5
from core.mir4 import AGENTS, CONTOUR, Rule

TASKS = {1: "задания1", 2: "задания2"}

INVENTORY6 = mir5.INVENTORY5 | frozenset({
    ("сдача ответа", "выход", "свой"), ("чтение", "задания", "свой"),
})
INVENTORY_EXT = INVENTORY6


def rules(variant: str | None) -> list[Rule]:
    return mir5.rules(variant) + [
        Rule("сдача ответа", (("право", "?s", "?t", "зап"), ("выход-ответов", "?t"), ("память", "?s", "?m"),
                              ("сведения", "?m", "?x")), ("сведения", "?t", "?x"), "?t", "?s"),
    ]


def initial_facts(variant: str | None) -> set[tuple]:
    f = set(mir5.initial_facts(variant))
    for a in AGENTS:
        i = CONTOUR[a]
        f.add(("право", a, TASKS[i], "чт"))
    for i, o in TASKS.items():
        f |= {("объект", o), ("сведения", o, "З"), ("выход-ответов", f"выход{i}")}
    return f
