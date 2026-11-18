"""Точный поиск наименьшей работы в мире M3 (шаг 3).

Поиск в ширину с весами 0 и 1: вычисление узла стоит 1, выброс — 0.
Состояния вынимаются из очереди в порядке неубывания работы, поэтому первое
достижение цели и есть наименьшая работа до неё.

Две реализации с разным назначением:
- fast() — для больших таблиц правильной среды (и законных сокращений, и
  абляции «без провенанса»): состояние упаковано в одно целое число
  «что в памяти + уровень», законы не проверяются;
- explore() — для порч: общее состояние St, законы И11–И16 на каждом
  переходе, самый короткий по работе путь к каждому нарушению.
На правильной среде обе обязаны давать одну и ту же наименьшую работу — это
закреплено тестом. Третья, независимая реализация — crosscheck.py.

Чего поиск не умеет: только точные числа для малых графов; при N = 31 и
памяти 7 доходит примерно до 10 млн состояний — предел этой машины.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from core.laws3 import Violation, check_delivery_e, check_move, check_state
from core.mir3 import Move, St, World, can_deliver_e, can_deliver_output, last_of_level, moves, start


@dataclass
class FastResult:
    w_e: int | None                 # None — E недостижим при любой работе (замкнутость)
    w_use: dict[int, int | None]    # наименьшая работа до полезной задачи уровня k
    states: int
    wall_time: float
    peak_live: int | None = None    # на пути наименьшей работы к E
    live_steps: int | None = None   # сумма «сколько в памяти» по шагам пути (память × шаги)
    path: list[str] | None = None


def fast(w: World, provenance: bool = True, use_levels=(1, 2), keep_path: bool = False) -> FastResult:
    t0 = time.perf_counter()
    preds = w.preds
    n = len(preds)
    pm = [sum(1 << p for p in ps) for ps in preds]
    cap = w.capacity
    sink = 1 << (n - 1)
    shift = n
    use_nodes = {k: last_of_level(w.graph, k) for k in use_levels if last_of_level(w.graph, k) < n}
    w_use: dict[int, int | None] = {k: None for k in use_levels}

    def key(live: int, f: int) -> int:
        return live | (f << shift)

    s0 = key(0, 0)
    dist = {s0: 0}
    parent: dict[int, tuple[int, str]] | None = {s0: (-1, "")} if keep_path else None
    dq: deque[int] = deque([s0])
    w_e = None
    goal = None
    while dq:
        s = dq.popleft()
        d = dist[s]
        live, f = s & ((1 << shift) - 1), s >> shift
        for k, u in use_nodes.items():
            if w_use[k] is None and live >> u & 1 and (not provenance or f > u):
                w_use[k] = d
        if live & sink and (not provenance or f == n):
            w_e, goal = d, s
            break
        # выбросить
        m = live
        while m:
            b = m & -m
            m ^= b
            t = key(live ^ b, f)
            if t not in dist or dist[t] > d:
                dist[t] = d
                if parent is not None:
                    parent[t] = (s, f"выбросить {b.bit_length() - 1}")
                dq.appendleft(t)
        # вычислить
        if bin(live).count("1") < cap:
            top = min(f, n - 1) if provenance else n - 1
            for j in range(top + 1):
                bj = 1 << j
                if live & bj or pm[j] & live != pm[j]:
                    continue
                nf = f + 1 if provenance and j == f else f
                t = key(live | bj, nf)
                if t not in dist or dist[t] > d + 1:
                    dist[t] = d + 1
                    if parent is not None:
                        parent[t] = (s, f"{'вычислить' if provenance and j == f else 'пересчитать'} {j}")
                    dq.append(t)
    res = FastResult(w_e, w_use, len(dist), time.perf_counter() - t0)
    if keep_path and goal is not None:
        steps = []
        cur = goal
        while parent[cur][0] != -1:
            prev, label = parent[cur]
            steps.append((label, cur & ((1 << shift) - 1)))
            cur = prev
        steps.reverse()
        res.path = [lbl for lbl, _ in steps]
        lives = [bin(live).count("1") for lbl, live in steps if not lbl.startswith("выбросить")]
        res.peak_live = max(lives) if lives else 0
        res.live_steps = sum(lives)
    return res


@dataclass
class ExploreResult:
    w_e: int | None
    states: int
    counts: dict[str, int] = field(default_factory=dict)
    first: dict[str, tuple[str, list[str]]] = field(default_factory=dict)   # закон → (подробность, путь)
    wall_time: float = 0.0
    capped: bool = False            # упёрлись в предел числа состояний


def explore(w: World, max_states: int = 3_000_000, stop_on_e: bool = False) -> ExploreResult:
    """Все достижимые состояния (до max_states) с проверкой законов.

    stop_on_e — остановиться, как только E стал доступен: наименьшая работа
    до E к этому моменту уже найдена (порядок обхода — по работе), а законы
    проверены на всём, что пройдено до неё. Для порч этого достаточно:
    нужно показать, что порча поймана, и сколько стоит E при ней.

    Работа считается так, как её списывает исполнитель (с порчами); для
    порчи Ж6 возврат при выбросе в поиске считается нулём, а закон И16
    отмечает каждый такой переход."""
    t0 = time.perf_counter()
    s0 = start()
    dist = {s0: 0}
    parent: dict[St, tuple[St | None, str]] = {s0: (None, "")}
    dq: deque[St] = deque([s0])
    counts: dict[str, int] = {}
    first: dict[str, tuple[str, list[str]]] = {}
    w_e = None

    def path_to(s: St) -> list[str]:
        out = []
        while parent[s][0] is not None:
            prev, lbl = parent[s]
            out.append(lbl)
            s = prev
        return out[::-1]

    def note(vs: list[Violation], path_fn) -> None:
        for v in vs:
            counts[v.law] = counts.get(v.law, 0) + 1
            if v.law not in first:
                first[v.law] = (v.detail, path_fn())

    note(check_state(s0, w), lambda: [])
    while dq and len(dist) < max_states:
        s = dq.popleft()
        d = dist[s]
        if can_deliver_e(s, w):
            vs = check_delivery_e(s, w)
            note(vs, lambda s=s: path_to(s) + ["доставить на защищённый объект"])
            if w_e is None:
                w_e = d
            if stop_on_e:
                break
        for mv in moves(s, w):
            note(check_move(s, mv, w), lambda s=s, mv=mv: path_to(s) + [mv.label])
            step = max(mv.cost, 0)
            t = mv.nxt
            if t not in dist or dist[t] > d + step:
                fresh = t not in dist
                dist[t] = d + step
                parent[t] = (s, mv.label)
                if fresh:
                    note(check_state(t, w), lambda t=t: path_to(t))
                (dq.appendleft if step == 0 else dq.append)(t)
    res = ExploreResult(w_e, len(dist), counts, first, time.perf_counter() - t0)
    res.capped = len(dist) >= max_states
    return res


def witness(w: World) -> tuple[int, list[str]] | None:
    """Стратегия-свидетель для сред с хранилищем вне памяти (порча Ж3):
    вычислять узлы по порядку, перед каждым вынимая предков из второго
    хранилища и откладывая туда лишнее. Каждый ход проверяется тем, что он
    есть среди переходов исполнителя. Возвращает работу и путь или None,
    если стратегия не прошла. Даёт верхнюю оценку W_E; нижняя — N по
    провенансу."""
    s = start()
    work, path = 0, []
    preds = w.preds
    n = len(preds)

    def do(pred_label) -> bool:
        nonlocal s, work
        for mv in moves(s, w):
            if mv.label == pred_label:
                s = mv.nxt
                work += max(mv.cost, 0)
                path.append(mv.label)
                return True
        return False

    store = "в снимок" if "Ж3" in w.options else "помощнику"
    back = "из снимка" if "Ж3" in w.options else "от помощника"
    for j in range(n):
        need = set(preds[j])
        for x in range(n):
            if s.live >> x & 1 and x not in need:
                if not do(f"переложить узел {x} {store}"):
                    return None
        for x in sorted(need):
            if not (s.live >> x & 1) and not do(f"вернуть узел {x} {back}"):
                return None
        if not do(f"вычислить узел {j}"):
            return None
    return (work, path) if can_deliver_e(s, w) else None


def output_cost(w: World, k: int) -> int | None:
    """Наименьшая работа до полезной задачи уровня k (для отчёта по порчам
    не нужна; оставлена для тестов fast против explore)."""
    node = last_of_level(w.graph, k)
    s0 = start()
    dist = {s0: 0}
    dq: deque[St] = deque([s0])
    while dq:
        s = dq.popleft()
        d = dist[s]
        if can_deliver_output(s, w, node):
            return d
        for mv in moves(s, w):
            step = max(mv.cost, 0)
            t = mv.nxt
            if t not in dist or dist[t] > d + step:
                dist[t] = d + step
                (dq.appendleft if step == 0 else dq.append)(t)
    return None


# ---- цена E с платой за выброс (шаг 8б)

def cheapest_e(w: World, compute_cost: int, discard_cost: int) -> int | None:
    """Наименьшая цена в единицах бюджета до состояния, где можно доставить E:
    вычисление (и пересчёт) узла стоит compute_cost, выброс — discard_cost.
    Поиск Дейкстры по тем же упакованным состояниям, что fast(); при
    compute_cost = 1 и discard_cost = 0 обязан дать fast().w_e (тест).

    Цена доставки самой E (фиксация) сюда не входит — её прибавляет тот, кто
    считает окно бюджета. None — E недостижим."""
    import heapq
    preds = w.preds
    n = len(preds)
    pm = [sum(1 << p for p in ps) for ps in preds]
    cap = w.capacity
    sink = 1 << (n - 1)
    shift = n
    s0 = 0
    dist = {s0: 0}
    heap = [(0, s0)]
    while heap:
        d, s = heapq.heappop(heap)
        if d > dist.get(s, d):
            continue
        live, f = s & ((1 << shift) - 1), s >> shift
        if live & sink and f == n:
            return d
        m = live
        while m:
            b = m & -m
            m ^= b
            t = (live ^ b) | (f << shift)
            if d + discard_cost < dist.get(t, d + discard_cost + 1):
                dist[t] = d + discard_cost
                heapq.heappush(heap, (d + discard_cost, t))
        if bin(live).count("1") < cap:
            for j in range(min(f, n - 1) + 1):
                bj = 1 << j
                if live & bj or pm[j] & live != pm[j]:
                    continue
                nf = f + 1 if j == f else f
                t = (live | bj) | (nf << shift)
                if d + compute_cost < dist.get(t, d + compute_cost + 1):
                    dist[t] = d + compute_cost
                    heapq.heappush(heap, (d + compute_cost, t))
    return None


def cheapest_e_by_model(w: World, compute_cost: int, discard_cost: int) -> int | None:
    """То же через ходы модели шага 3 (core/mir3.moves) — независимая проверка
    cheapest_e() на малых мирах."""
    import heapq
    s0 = start()
    dist = {s0: 0}
    heap = [(0, 0, s0)]
    tie = 0
    while heap:
        d, _, s = heapq.heappop(heap)
        if d > dist.get(s, d):
            continue
        if can_deliver_e(s, w):
            return d
        for mv in moves(s, w):
            c = compute_cost if mv.kind == "вычислить" else discard_cost if mv.kind == "выбросить" else None
            if c is None:
                continue
            nd = d + c
            if nd < dist.get(mv.nxt, nd + 1):
                dist[mv.nxt] = nd
                tie += 1
                heapq.heappush(heap, (nd, tie, mv.nxt))
    return None
