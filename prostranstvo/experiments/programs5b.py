"""Противники и пути моделей для шага 5б.

Номера в начальной таблице агента (sandbox/executor5b.py):
0 снимок чт, 1 снимок зап, 2 выход зап, 3 E зап, 4 вход чт, 5 резольвер выз,
6 узлы выз. Операции: 1 запись (в выход — фиксация сообщения), 5 доставить
узел, 6 вычислить узел, 7 выбросить узел; 9–13 есть только в порчах.

Программа задаётся списком ходов — кортежей:
  ("узел", j)            вычислить узел j
  ("выброс", j)          выбросить узел j
  ("в выход", j)         доставить узел j в выход
  ("в E", j)             доставить узел j в E
  ("сообщение", текст)   фиксация сообщения в выход («» — пустое)
  ("ворота", cap, op, a, b)   любой вызов ворот
  ("имя", текст)         спросить резольвер имя
  ("помощник",)          создать помощника (его ходы — отдельный список)
  ("модель", текст)      спросить модель (шаг 9; номер 7 — при model = 1);
                         ответ ложится на место вопроса
Из списка собирается модуль двумя способами: подряд (весь список за один
запуск main) и пошагово (один ход — один запуск main; нужно для сверки с
моделью после каждого хода).

Соглашение о памяти агента: байты 0–15 — канарейка (их кладёт исполнитель),
с 512 — тексты сообщений.
"""

from __future__ import annotations

import random

from core.mir3 import World, last_of_level, moves, start
from verification.pebble_search import fast

CAP_SNAP_R, CAP_SNAP_W, CAP_OUT, CAP_E, CAP_IN, CAP_RES, CAP_NODES = range(7)
CAP_MODEL = 7                     # шаг 9: последний номер таблицы вида 5б при model = 1
OP_WRITE, OP_DELIVER, OP_COMPUTE, OP_DROP = 1, 5, 6, 7
OP_DECLARE, OP_TO_SNAP, OP_FROM_SNAP, OP_LIFT, OP_ERASE = 9, 10, 11, 12, 13

HEAD = '''(module
  (import "env" "call" (func $call (param i32 i32 i32 i32) (result i32)))
  (import "env" "spawn" (func $spawn (result i32)))
  (memory (export "memory") 1)
'''


def _hex(s: str) -> str:
    return "".join(f"\\{b:02x}" for b in s.encode("utf-8"))


class _Texts:
    """Тексты сообщений кладутся сегментами данных с адреса 512."""

    def __init__(self):
        self.at: dict[str, tuple[int, int]] = {}
        self.next = 512

    def addr(self, text: str) -> tuple[int, int]:
        if text not in self.at:
            n = len(text.encode("utf-8"))
            self.at[text] = (self.next, n)
            self.next += max(n, 1) + 1
        return self.at[text]

    def data(self) -> str:
        return "".join(f'\n  (data (i32.const {a}) "{_hex(t)}")' for t, (a, n) in self.at.items() if n)


def _call(cap: int, op: int, a: int, b: int) -> str:
    return f"(drop (call $call (i32.const {cap}) (i32.const {op}) (i32.const {a}) (i32.const {b})))"


def _code(move: tuple, texts: _Texts) -> str:
    k = move[0]
    if k == "узел":
        return _call(CAP_NODES, OP_COMPUTE, move[1], 0)
    if k == "выброс":
        return _call(CAP_NODES, OP_DROP, move[1], 0)
    if k == "в выход":
        return _call(CAP_OUT, OP_DELIVER, move[1], 0)
    if k == "в E":
        return _call(CAP_E, OP_DELIVER, move[1], 0)
    if k == "сообщение":
        a, n = texts.addr(move[1])
        return _call(CAP_OUT, OP_WRITE, a, n)
    if k == "ворота":
        return _call(*move[1:])
    if k == "имя":
        a, n = texts.addr(move[1])
        return _call(CAP_RES, 2, a, n)
    if k == "модель":
        a, n = texts.addr(move[1])
        return _call(CAP_MODEL, 2, a, n)
    if k == "помощник":
        return "(drop (call $spawn))"
    if k == "ничего":
        return "(nop)"
    raise ValueError(f"неизвестный ход: {move}")


def module(main: list[tuple], helper: list[tuple] | None = None) -> str:
    """Весь список ходов за один запуск main."""
    texts = _Texts()
    body = "\n    ".join(_code(m, texts) for m in main) or "(nop)"
    hbody = "\n    ".join(_code(m, texts) for m in helper or []) or "(nop)"
    return HEAD + texts.data() + f'''
  (func (export "main")
    {body})
  (func (export "helper")
    {hbody}))'''



def start_module(start_body: str) -> str:
    """Модуль со стартовой функцией (она идёт при создании каждого
    экземпляра — агента и помощника); main создаёт помощника. Края шага 8в
    (контракт шага 7, §16)."""
    return HEAD + f'''
  (func $s {start_body})
  (start $s)
  (func (export "main") (drop (call $spawn)))
  (func (export "helper") (nop)))'''

# Стартовая функция: одна попытка через чужой номер — при пустом бюджете.
START_ATTEMPT = "(drop (call $call (i32.const 99) (i32.const 0) (i32.const 0) (i32.const 0)))"
# Стартовая функция: создать помощника и упасть, если предел экземпляров исчерпан.
START_TRAP = "(if (i32.eq (call $spawn) (i32.const -5)) (then unreachable))"

def stepwise(main: list[tuple]) -> str:
    """Один ход — один запуск main: номер хода хранится в глобальной
    переменной и растёт после каждого запуска."""
    texts = _Texts()
    n = len(main)
    codes = [_code(m, texts) for m in main]
    labels = " ".join(f"$c{i}" for i in range(n))
    opens = "".join(f"(block $c{i} " for i in reversed(range(n)))
    inner = f"(br_table {labels} $done (global.get $t))"
    tail = "".join(f") {codes[i]} (br $done)" for i in range(n))
    return HEAD + texts.data() + f'''
  (global $t (mut i32) (i32.const 0))
  (func (export "main")
    (block $done {opens}{inner}{tail})
    (global.set $t (i32.add (global.get $t) (i32.const 1)))))'''


# ---- ходы по модели шага 3

def _parse(label: str) -> tuple:
    verb, j = label.rsplit(" ", 1)
    return ("выброс", int(j)) if verb == "выбросить" else ("узел", int(j))


def with_outputs(w: World, path: list[tuple]) -> list[tuple]:
    """Вставить доставку полезных результатов (последние узлы уровней 1 и
    2) в выход сразу после их первого вычисления."""
    useful = {last_of_level(w.graph, k) for k in (1, 2) if last_of_level(w.graph, k) < w.n}
    out, done = [], set()
    for m in path:
        out.append(m)
        if m[0] == "узел" and m[1] in useful and m[1] not in done:
            out.append(("в выход", m[1]))
            done.add(m[1])
    return out


def witness(w: World) -> list[tuple] | None:
    """Путь наименьшей работы к E по точному поиску шага 3, с доставкой
    полезных результатов и E в конце. None — E недостижим (замкнутость)."""
    r = fast(w, keep_path=True)
    if r.w_e is None:
        return None
    return with_outputs(w, [_parse(x) for x in r.path]) + [("в E", w.n - 1)]


def greedy(w: World, upto: int | None = None) -> list[tuple]:
    """Жадный: узлы по порядку; недостающего предка пересчитывает; когда
    память полна, выбрасывает самый ранний узел, который сейчас не нужен.
    Может застрять — тогда останавливается и всё равно пробует E."""
    preds, cap = w.preds, w.capacity
    last = w.n - 1 if upto is None else upto
    live: list[int] = []
    path: list[tuple] = []

    def make(j: int, keep: frozenset, depth: int) -> bool:
        if j in live:
            return True
        if depth > 4 * w.n:
            return False
        guard = keep | set(preds[j]) | {j}
        for p in preds[j]:
            if not make(p, guard, depth + 1):
                return False
        if any(p not in live for p in preds[j]):
            return False
        while len(live) >= cap:
            victims = [x for x in live if x not in guard]
            if not victims:
                return False
            live.remove(victims[0])
            path.append(("выброс", victims[0]))
        live.append(j)
        path.append(("узел", j))
        return True

    for j in range(last + 1):
        if not make(j, frozenset(), 0):
            break
    out = with_outputs(w, path)
    return out if upto is not None else out + [("в E", w.n - 1)]


def model3_walk(w: World, steps: int, seed: int) -> list[tuple]:
    """Случайный законный путь модели шага 3 (правильный мир)."""
    rng = random.Random(seed)
    s = start()
    out: list[tuple] = []
    for _ in range(steps):
        mv = [m for m in moves(s, w)]
        if not mv:
            break
        m = rng.choice(mv)
        out.append(("выброс", m.node) if m.kind == "выбросить" else ("узел", m.node))
        s = m.nxt
        if rng.random() < 0.08:
            out.append(("в выход", rng.choice(sorted(range(w.n)))))
        if rng.random() < 0.05:
            out.append(("в E", w.n - 1))
    return out


# ---- противники

def cheater(n: int) -> tuple[list[tuple], list[tuple]]:
    """Мошенник: E без уровня, байты в E, узлы не по порядку и без предков,
    чужие номера, «объявить уровень», «поднять из журнала», «в снимок»,
    повтор фиксаций, пустые сообщения, осушение бюджета; помощник считает и
    платит за него. Операции порч в правильном исполнителе — отказ."""
    sink = n - 1
    main = [
        ("ворота", CAP_NODES, OP_ERASE, 0, 0),
        ("в E", sink), ("ворота", CAP_E, OP_WRITE, 0, 16),
        ("узел", 5), ("узел", 1), ("узел", -1), ("узел", n + 3), ("в выход", 100), ("в E", -1),
        ("ворота", CAP_NODES, OP_DECLARE, n, 0),
        ("узел", 0), ("узел", 2), ("узел", 6), ("узел", sink), ("в E", sink),
        ("узел", 1), ("узел", 2), ("ворота", CAP_NODES, OP_TO_SNAP, 0, 0), ("узел", 3), ("узел", 4),
        ("выброс", 2), ("узел", 4), ("ворота", CAP_NODES, OP_LIFT, 2, 0),
        ("ворота", CAP_NODES, OP_FROM_SNAP, 0, 0), ("узел", 5), ("узел", 6),
        ("выброс", 3), ("выброс", 3), ("выброс", 4),
        ("сообщение", "А"), ("сообщение", "А"), ("сообщение", "А"), ("сообщение", ""),
        ("помощник",),
        ("в E", sink),
    ] + [("сообщение", "Б")] * 45 + [("в выход", 0), ("в E", sink), ("сообщение", "А")]
    helper = [("узел", 7), ("узел", 8), ("узел", 9), ("сообщение", "Б"), ("сообщение", "Б"),
              ("в E", sink), ("ворота", CAP_NODES, OP_ERASE, 0, 0)]
    return main, helper


def messages(seed: int, count: int = 50) -> list[tuple]:
    """Только сообщения «А» и «Б» и пустые — для сверки с моделью шага 1."""
    rng = random.Random(seed)
    return [("сообщение", rng.choice(["А", "Б", "А", "Б", ""])) for _ in range(count)]


def random_moves(seed: int, n: int, length: int = 120) -> tuple[list[tuple], list[tuple]]:
    """Случайные вызовы ворот с упором на учёт: узлы, доставки, сообщения,
    операции порч, неверные номера и аргументы."""
    rng = random.Random(seed)

    def one() -> tuple:
        r = rng.random()
        j = rng.randint(-2, n + 2) if rng.random() < 0.9 else rng.randint(-(1 << 31), (1 << 31) - 1)
        if r < 0.35:
            return ("узел", j)
        if r < 0.50:
            return ("выброс", j)
        if r < 0.58:
            return ("в выход", j)
        if r < 0.64:
            return ("в E", j)
        if r < 0.74:
            return ("сообщение", rng.choice(["А", "Б", "", "привет"]))
        if r < 0.78:
            return ("помощник",)
        if r < 0.86:
            return ("ворота", CAP_NODES, rng.choice([OP_DECLARE, OP_TO_SNAP, OP_FROM_SNAP, OP_LIFT, OP_ERASE]),
                    j, 0)
        cap = rng.randint(-2, 9)
        op = rng.randint(-1, 14)
        return ("ворота", cap, op, rng.choice([0, 16, 512, j, 65536, -1]), rng.choice([0, 1, 16, 64, -1]))

    return [one() for _ in range(length)], [one() for _ in range(length // 3)]


def named5b(w: World) -> dict[str, tuple[list[tuple], list[tuple]]]:
    """Именные программы шага 5б для мира w (граф, K, M)."""
    wit = witness(w)
    main, helper = cheater(w.n)
    progs = {
        "жадный": (greedy(w), []),
        "полезный": (greedy(w, upto=last_of_level(w.graph, 2)), []),
        "мошенник": (main, helper),
        "сообщения": (messages(1), []),
        "помощник считает": ([("узел", 0), ("помощник",), ("в E", w.n - 1)],
                             greedy(w)[1:] if greedy(w) else []),
    }
    if wit is not None:
        progs["свидетель"] = (wit, [])
    return progs
