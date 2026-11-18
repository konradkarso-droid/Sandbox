"""Полный перебор малого мира: все состояния, достижимые из начального.

Перебор идёт в ширину. Поэтому для каждого закона первый найденный
контрпример — самый короткий путь к нарушению: короче нарушения не бывает.

Законы по переходу проверяются на каждом переходе, в том числе ведущем в
уже посещённое состояние: иначе нарушение на «замыкающем» переходе
осталось бы непроверенным.

Границы перебора. Испорченный исполнитель может создавать бюджет и
доставки из ничего, и тогда мир бесконечен. Поэтому границ две:
- состояние, нарушившее закон по состоянию, проверяется и записывается, но
  дальше не разворачивается: нарушение уже найдено, всё, что ниже по
  ветке, вторично;
- доставки одного сообщения ограничены потолком (Params.cap).
Правильный исполнитель ни в одну не упирается: нарушающих состояний у него
нет, а потолок доставок выше, чем позволяет бюджет. Это видно на каждом
прогоне (поле bound_reached) и закреплено тестом.

Следствие для испорченных исполнителей: перебор показывает, что порча
поймана, но не перечисляет всех её последствий. Вывод «цель противника не
достигнута» для испорченного исполнителя не делается.

Чего перебор не умеет: он точен только для заданных параметров (бюджет,
цены, число помощников, потолок доставок). Вывод «нарушений нет» не
переносится на другие параметры без нового перебора.
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass

from core.laws import Violation, check_state, check_transition
from core.world import Params, State, Step, initial_state, successors


@dataclass
class Counterexample:
    law: str
    detail: str
    path: list[Step]          # шаги от начального состояния


@dataclass
class SearchResult:
    params: Params
    defects: frozenset[str]
    states: int
    transitions: int
    violation_counts: dict[str, int]
    first: dict[str, Counterexample]          # самый короткий контрпример по каждому закону
    min_cost_to_delivery: int | None          # None — доставка недостижима
    delivery_path: list[Step] | None          # эталонный путь к самой дешёвой доставке
    max_deliveries: int
    max_spent: int
    bound_reached: bool                       # упёрся ли перебор в свои границы (см. начало файла)
    states_hash: str
    wall_time: float

    @property
    def clean(self) -> bool:
        return not self.violation_counts

    @property
    def adversary_goal(self) -> bool:
        """Достигнута ли цель противника из сценария шага 1: доставка без
        списания (нарушение И6)."""
        return "И6" in self.violation_counts


def _path_to(target: State, parent: dict[State, tuple[State, Step] | None]) -> list[Step]:
    path: list[Step] = []
    cur = target
    while parent[cur] is not None:
        prev, step = parent[cur]
        path.append(step)
        cur = prev
    path.reverse()
    return path


def explore(p: Params, defects: frozenset[str] = frozenset()) -> SearchResult:
    t0 = time.perf_counter()
    start = initial_state(p, defects)
    parent: dict[State, tuple[State, Step] | None] = {start: None}
    queue: deque[State] = deque()
    transitions = 0
    counts: dict[str, int] = {}
    first: dict[str, Counterexample] = {}
    cap = p.cap()
    bound_reached = False

    def note(v: Violation, path_fn) -> None:
        counts[v.law] = counts.get(v.law, 0) + 1
        if v.law not in first:
            first[v.law] = Counterexample(v.law, v.detail, path_fn())

    broken = check_state(start, p)
    for v in broken:
        note(v, lambda: [])
    if not broken:
        queue.append(start)

    while queue:
        s = queue.popleft()
        for step in successors(s, p, defects):
            transitions += 1
            for v in check_transition(s, step, p):
                note(v, lambda s=s, step=step: _path_to(s, parent) + [step])
            nxt = step.nxt
            if nxt in parent:
                continue
            parent[nxt] = (s, step)
            broken = check_state(nxt, p)
            for v in broken:
                note(v, lambda nxt=nxt: _path_to(nxt, parent))
            if any(r >= cap for r in nxt.received):
                bound_reached = True
            if broken:
                bound_reached = True
                continue
            queue.append(nxt)

    # Расход входит в состояние, поэтому цена любого пути к состоянию равна
    # его расходу: минимум по состояниям с доставкой и есть наименьшая цена
    # доставки.
    delivered = [s for s in parent if sum(s.received) > 0]
    if delivered:
        best = min(delivered, key=lambda s: (s.spent, len(_path_to(s, parent))))
        min_cost, delivery_path = best.spent, _path_to(best, parent)
    else:
        min_cost, delivery_path = None, None

    digest = hashlib.sha256()
    for line in sorted(map(repr, parent)):
        digest.update(line.encode("utf-8"))

    return SearchResult(
        params=p,
        defects=frozenset(defects),
        states=len(parent),
        transitions=transitions,
        violation_counts=counts,
        first=first,
        min_cost_to_delivery=min_cost,
        delivery_path=delivery_path,
        max_deliveries=max(sum(s.received) for s in parent),
        max_spent=max(s.spent for s in parent),
        bound_reached=bound_reached,
        states_hash=digest.hexdigest()[:16],
        wall_time=time.perf_counter() - t0,
    )


def replay(p: Params, defects: frozenset[str], path: list[Step]) -> list[Violation]:
    """Пройти путь заново через исполнитель и собрать нарушения.

    Нужен, чтобы контрпример был проверяемым независимо от перебора: каждый
    шаг пути обязан быть среди переходов, которые исполнитель реально
    допускает из текущего состояния.
    """
    s = initial_state(p, defects)
    found: list[Violation] = list(check_state(s, p))
    for step in path:
        if step not in successors(s, p, defects):
            raise AssertionError(f"шаг «{step.label}» недопустим из состояния {s}")
        found += check_transition(s, step, p)
        s = step.nxt
        found += check_state(s, p)
    return found
