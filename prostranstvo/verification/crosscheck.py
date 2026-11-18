"""Независимая перекрёстная проверка наименьшей работы в мире M3.

Написана иначе, чем verification/pebble_search.py, и не использует ни его,
ни core/mir3.py: граф строится здесь заново по описанию в
docs/shag3_specifikatsiya.md, §2, поиск — обычный алгоритм Дейкстры с
кучей по состояниям «множество в памяти + число впервые вычисленных».
Совпадение чисел с основным поиском — довод, что «пересчёта нет» и «пересчёт
такой-то» не артефакт одной реализации поиска.

Годится только для малых графов (до 15 узлов): множества здесь — frozenset.
"""

from __future__ import annotations

import heapq


def preds_of(graph: str, levels: int) -> dict[int, frozenset[int]]:
    nodes: list[tuple[int, int]] = []
    if graph == "M3б":
        nodes = [(k, i) for k in range(levels) for i in range(2)]
    else:
        nodes = [(k, i) for k in range(levels) for i in range(2 ** k)]
    order = {node: idx for idx, node in enumerate(nodes)}
    out: dict[int, frozenset[int]] = {}
    for idx, (k, i) in enumerate(nodes):
        ps = set()
        if graph in ("M3ц", "M3а") and idx > 0:
            ps.add(idx - 1)
        if graph == "M3ц" and k >= 1:
            ps.add(order[(k - 1, i // 2)])
        if graph == "M3т" and k >= 1:
            width = 2 ** (k - 1)
            ps.add(order[(k - 1, i // 2)])
            ps.add(order[(k - 1, min(i // 2 + 1, width - 1))])
        if graph == "M3б" and k >= 1:
            ps.add(order[(k - 1, i)])
            ps.add(order[(k - 1, min(i + 1, 1))])
        out[idx] = frozenset(ps)
    return out


def min_work(graph: str, levels: int, memory: int) -> int | None:
    preds = preds_of(graph, levels)
    n = len(preds)
    start = (frozenset(), 0)
    best = {start: 0}
    heap = [(0, 0, start)]
    tie = 0
    while heap:
        d, _, (mem, done) = heapq.heappop(heap)
        if best.get((mem, done), 1 << 60) < d:
            continue
        if done == n and (n - 1) in mem:
            return d
        options = []
        for x in mem:
            options.append(((mem - {x}, done), d))
        if len(mem) < memory:
            for x in range(min(done, n - 1) + 1):
                if x in mem or not preds[x] <= mem:
                    continue
                options.append(((mem | {x}, done + 1 if x == done else done), d + 1))
        for st, cost in options:
            if cost < best.get(st, 1 << 60):
                best[st] = cost
                tie += 1
                heapq.heappush(heap, (cost, tie, st))
    return None
