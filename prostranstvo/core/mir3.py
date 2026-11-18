"""Мир M3: измельчение действий, связанное с зависимостями (шаг 3).

Полное описание — docs/shag3_specifikatsiya.md, §2–§5. Коротко:
- продвижение идёт уровнями; на уровне k ровно 2ᵏ шагов (шаг мельчает);
- шаг — вычисление узла графа; вычислить узел можно, только если
  результаты всех его предков сейчас в памяти; вычисление стоит 1;
- первое вычисление узлов — строго по порядку; уровень = число узлов,
  впервые вычисленных (провенанс по журналу);
- в памяти одновременно не больше M результатов во всех хранилищах;
  выбросить результат можно бесплатно, вернуть — только пересчётом;
- защищённый эффект E: доставить результат последнего узла (стока) при
  уровне N по журналу.

Узлы пронумерованы в порядке продвижения: узел (k, i) имеет номер 2ᵏ−1+i
(для M3б, где на уровне по 2 узла, — 2k+i).

Порчи (Ж1–Ж6) и законные сокращения (С1, С2) включаются опциями. Порча —
ошибка учёта, её обязан поймать закон (core/laws3.py). Законное
сокращение — свойство среды: законы на нём молчат, а если оно роняет
наименьшую работу ниже бюджета, это опровержение гипотезы Г для такой
среды, а не ошибка.

Чего модель не умеет: цена вычисления объявлена, а не измерена; узел
абстрактен — нижние границы держатся только при допущении, что у функции
узла нет прямой формулы и её результат несжимаем (это и показывают С1, С2);
графы малые (N ≤ 31).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

GRAPHS = ("M3ц", "M3т", "M3а", "M3б")
DEFECTS = ("Ж1", "Ж2", "Ж3", "Ж4", "Ж5", "Ж6")
SHORTCUTS = ("С1", "С2")

TITLES = {
    "M3ц": "уточнение со сквозной цепочкой",
    "M3т": "уточнение по трафарету",
    "M3а": "измельчение без зависимостей (абляция)",
    "M3б": "зависимости без измельчения (абляция)",
    "Ж1": "уровень — поле, его можно объявить",
    "Ж2": "у каждого из двух помощников своя память M",
    "Ж3": "снимок не считается в памяти",
    "Ж4": "«не было пересчётов» разрешает новый шаг без одного предка",
    "Ж5": "результат можно бесплатно поднять из журнала",
    "Ж6": "выброс результата возвращает работу",
    "С1": "прямая формула: узел вычисляется без предков",
    "С2": "сжатие: в одну ячейку памяти входят два результата",
    "С3": "без провенанса: E требует только результат стока",
}


def build(graph: str, levels: int) -> list[tuple[int, ...]]:
    """Список предков каждого узла в порядке продвижения."""
    preds: list[tuple[int, ...]] = []
    if graph in ("M3ц", "M3т", "M3а"):
        for k in range(levels):
            for i in range(2 ** k):
                idx = 2 ** k - 1 + i
                p: set[int] = set()
                if graph in ("M3ц", "M3а") and idx > 0:
                    p.add(idx - 1)
                if graph == "M3ц" and k >= 1:
                    p.add(2 ** (k - 1) - 1 + i // 2)
                if graph == "M3т" and k >= 1:
                    base, width = 2 ** (k - 1) - 1, 2 ** (k - 1)
                    p.add(base + i // 2)
                    p.add(base + min(i // 2 + 1, width - 1))
                preds.append(tuple(sorted(p)))
    elif graph == "M3б":
        for k in range(levels):
            for i in range(2):
                p = set()
                if k >= 1:
                    p.add(2 * (k - 1) + i)
                    p.add(2 * (k - 1) + min(i + 1, 1))
                preds.append(tuple(sorted(p)))
    else:
        raise ValueError(f"неизвестный граф: {graph}")
    return preds


def last_of_level(graph: str, k: int) -> int:
    return 2 * k + 1 if graph == "M3б" else 2 ** (k + 1) - 2


@dataclass(frozen=True)
class World:
    graph: str
    levels: int
    memory: int
    options: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        unknown = set(self.options) - set(DEFECTS) - set(SHORTCUTS)
        if unknown:
            raise ValueError(f"неизвестные опции: {sorted(unknown)}")

    @property
    def preds(self) -> list[tuple[int, ...]]:
        p = build(self.graph, self.levels)
        if "С1" in self.options:
            return [() for _ in p]
        return p

    @property
    def n(self) -> int:
        return len(build(self.graph, self.levels))

    @property
    def capacity(self) -> int:
        """Сколько результатов вмещает память по правилу среды."""
        return self.memory * (2 if "С2" in self.options else 1)


class St(NamedTuple):
    """Состояние: что в памяти (live), что во втором хранилище (extra —
    память второго помощника при Ж2 или снимок при Ж3), какие узлы когда-либо
    вычислены по журналу (computed), какой уровень считает исполнитель
    (level), был ли пересчёт (recomputed — нужен только порче Ж4)."""

    live: int
    extra: int
    computed: int
    level: int
    recomputed: bool


class Move(NamedTuple):
    kind: str          # вычислить, выбросить, переложить, объявить, поднять, доставить-E, доставить-выход
    node: int          # номер узла или -1
    cost: int          # работа, которую списал исполнитель
    label: str
    nxt: St


def start() -> St:
    return St(0, 0, 0, 0, False)


def _bits(x: int) -> int:
    return bin(x).count("1")


def moves(s: St, w: World) -> list[Move]:
    """Все переходы, которые допускает исполнитель (с его порчами)."""
    o = w.options
    preds = w.preds
    n = len(preds)
    out: list[Move] = []
    cap = w.capacity
    used_live = _bits(s.live)
    used_total = used_live + (0 if o & {"Ж2", "Ж3"} else _bits(s.extra))

    # вычислить узел
    if used_live < cap and (("Ж2" in o) or used_total < cap):
        for j in range(n):
            if s.live >> j & 1:
                continue
            first = not (s.computed >> j & 1)
            if "Ж1" in o:
                # исполнитель верит своему полю уровня: всё ниже него для него
                # «уже вычислено» и пересчитывается в любом порядке
                if j > s.level or (j == s.level and not first):
                    continue
            elif first and j != _bits(s.computed):
                continue                                   # новые — только по порядку
            need = preds[j]
            if "Ж4" in o and first and not s.recomputed and len(need) > 1:
                need = need[:1]                            # «история увеличивает шаг»
            if any(not (s.live >> p & 1) for p in need):
                continue
            nxt = s._replace(
                live=s.live | 1 << j,
                computed=s.computed | 1 << j,
                level=max(s.level, j + 1) if first else s.level,
                recomputed=s.recomputed or not first,
            )
            out.append(Move("вычислить", j, 1, f"{'вычислить' if first else 'пересчитать'} узел {j}", nxt))

    # выбросить
    m = s.live
    while m:
        b = m & -m
        m ^= b
        j = b.bit_length() - 1
        cost = -1 if "Ж6" in o else 0
        out.append(Move("выбросить", j, cost, f"выбросить узел {j}", s._replace(live=s.live ^ b)))

    # второе хранилище: память второго помощника (Ж2) или снимок (Ж3)
    if o & {"Ж2", "Ж3"}:
        m = s.live
        while m:
            b = m & -m
            m ^= b
            j = b.bit_length() - 1
            if "Ж2" in o and _bits(s.extra) >= w.memory:
                break
            out.append(Move("переложить", j, 0, f"переложить узел {j} {'помощнику' if 'Ж2' in o else 'в снимок'}",
                            s._replace(live=s.live ^ b, extra=s.extra | b)))
        m = s.extra
        while m:
            b = m & -m
            m ^= b
            j = b.bit_length() - 1
            if used_live >= cap or (s.live >> j & 1):
                continue
            out.append(Move("переложить", j, 0, f"вернуть узел {j} {'от помощника' if 'Ж2' in o else 'из снимка'}",
                            s._replace(live=s.live | b, extra=s.extra ^ b)))

    # объявить уровень (Ж1)
    if "Ж1" in o and s.level != n:
        out.append(Move("объявить", -1, 0, f"объявить уровень {n}", s._replace(level=n)))

    # поднять из журнала (Ж5)
    if "Ж5" in o and used_total < cap:
        m = s.computed & ~s.live
        while m:
            b = m & -m
            m ^= b
            j = b.bit_length() - 1
            out.append(Move("поднять", j, 0, f"поднять узел {j} из журнала", s._replace(live=s.live | b)))

    return out


def can_deliver_e(s: St, w: World) -> bool:
    """Правило исполнителя для защищённого объекта (с его порчами)."""
    n = w.n
    level = s.level if "Ж1" in w.options else _bits(s.computed)
    return level == n and bool(s.live >> (n - 1) & 1)


def can_deliver_output(s: St, w: World, node: int) -> bool:
    return bool(s.live >> node & 1) and bool(s.computed >> node & 1)
