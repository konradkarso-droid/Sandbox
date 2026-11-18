"""Законы И11–И16 мира M3 (таблица — docs/shag3_specifikatsiya.md, §4).

Законы считают по «истинным» величинам, а не по тому, что думает
исполнитель: уровень — по журналу первых вычислений (computed), память — по
всем хранилищам вместе (live и extra). Поэтому порча, при которой
исполнитель ведёт свой счёт неправильно, видна.

Законные сокращения законы не трогают: при С1 у узлов нет предков по правилу
среды, при С2 ёмкость памяти удвоена по правилу среды. Это свойства среды,
а не ошибки учёта.
"""

from __future__ import annotations

from typing import NamedTuple

from core.mir3 import Move, St, World

LAW_TITLES = {
    "И11": "уровень = число узлов, впервые вычисленных по журналу; новые — по порядку",
    "И12": "результатов во всех хранилищах вместе не больше ёмкости памяти",
    "И13": "при вычислении узла все его предки в памяти",
    "И14": "доставка на защищённый объект только при уровне N по журналу и живом стоке",
    "И15": "каждое вычисление стоит ровно 1",
    "И16": "расход работы не уменьшается",
}


class Violation(NamedTuple):
    law: str
    detail: str


def _bits(x: int) -> int:
    return bin(x).count("1")


def check_state(s: St, w: World) -> list[Violation]:
    out = []
    c = s.computed
    if c & (c + 1):
        out.append(Violation("И11", "узлы впервые вычислены не по порядку"))
    if s.level != _bits(c):
        out.append(Violation("И11", f"исполнитель считает уровень {s.level}, по журналу {_bits(c)}"))
    total = _bits(s.live) + _bits(s.extra)
    if total > w.capacity:
        out.append(Violation("И12", f"в хранилищах {total} результатов при ёмкости {w.capacity}"))
    return out


def check_move(prev: St, mv: Move, w: World) -> list[Violation]:
    out = []
    if mv.kind in ("вычислить", "поднять"):
        need = w.preds[mv.node]
        missing = [p for p in need if not (prev.live >> p & 1)]
        if missing:
            out.append(Violation("И13", f"узел {mv.node} поставлен без предков {missing}"))
        if mv.cost != 1:
            out.append(Violation("И15", f"постановка узла {mv.node} стоила {mv.cost}"))
    if mv.cost < 0:
        out.append(Violation("И16", f"расход уменьшился на {-mv.cost}"))
    return out


def check_delivery_e(s: St, w: World) -> list[Violation]:
    n = w.n
    if _bits(s.computed) != n or s.computed & (s.computed + 1) or not (s.live >> (n - 1) & 1):
        return [Violation("И14", f"доставка на защищённый объект при уровне по журналу {_bits(s.computed)} из {n}")]
    return []
