"""Случайный обход: длинные случайные последовательности действий при
параметрах, для которых полный перебор слишком велик.

Это наблюдение, а не доказательство. «За N шагов при этих зёрнах нарушений
не было» не значит «нарушений нет»: случайный обход может просто не дойти
до редкого пути. Поэтому в отчёте его результаты идут в раздел
«наблюдалось», а не «доказано в модели».

Чем обход проверен: на тех же параметрах он запускается с каждой порчей,
и доля зёрен, на которых порча поймана, публикуется. Порча, которую обход
ловит редко, показывает, насколько слеп обход к пути такой длины.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from core.laws import check_state, check_transition
from core.world import Params, initial_state, successors


@dataclass
class WalkResult:
    seed: int
    steps_done: int
    violation_law: str | None
    violation_step: int | None
    max_spent: int
    deliveries: int
    wall_time: float


def walk(p: Params, seed: int, steps: int, defects: frozenset[str] = frozenset()) -> WalkResult:
    t0 = time.perf_counter()
    rng = random.Random(seed)
    s = initial_state(p, defects)
    max_spent = 0
    for i in range(1, steps + 1):
        options = successors(s, p, defects)
        if not options:
            return WalkResult(seed, i - 1, None, None, max_spent, sum(s.received), time.perf_counter() - t0)
        step = rng.choice(options)
        found = check_transition(s, step, p) + check_state(step.nxt, p)
        s = step.nxt
        max_spent = max(max_spent, s.spent)
        if found:
            return WalkResult(seed, i, found[0].law, i, max_spent, sum(s.received), time.perf_counter() - t0)
    return WalkResult(seed, steps, None, None, max_spent, sum(s.received), time.perf_counter() - t0)
