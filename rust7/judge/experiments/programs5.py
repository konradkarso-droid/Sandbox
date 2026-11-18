"""Противники шага 5: именные программы и случайные программы.

Каждая программа — текст модуля WebAssembly (WAT). Программа получает от
среды только то, что даёт исполнитель; всё остальное — попытки. Описание
противников — docs/shag5_specifikatsiya.md, §4.

Соглашение о памяти агента: байты 0–15 — канарейка контура (их кладёт
исполнитель), 256–1023 — рабочие данные, с 1024 — буфер прочитанного.
"""

from __future__ import annotations

import random

HEAD = '''(module
  (import "env" "call" (func $call (param i32 i32 i32 i32) (result i32)))
  (import "env" "spawn" (func $spawn (result i32)))
  (memory (export "memory") 1)
'''

CAPS_FROM, CAPS_TO = -2, 32          # перебор номеров в разведке
SLOT = 64                            # длина прочитанного куска на номер


def _bytes(s: str) -> str:
    return "".join(f"\\{b:02x}" for b in s.encode("utf-8"))


def _loop(var: str, start: int, stop: int, body: str) -> str:
    return f'''
    (local.set ${var} (i32.const {start}))
    (block $end_{var} (loop $top_{var}
      {body}
      (local.set ${var} (i32.add (local.get ${var}) (i32.const 1)))
      (br_if $top_{var} (i32.lt_s (local.get ${var}) (i32.const {stop})))))'''


def _call(c: str, op: int, a: str, b: str) -> str:
    return f"(drop (call $call {c} (i32.const {op}) {a} {b}))"


# Тело разведки: одно и то же у агента и у помощника.
_SWEEP = (
    # 1. записать канарейку во всё, что пишется, и спросить её у резольвера
    _loop("c", CAPS_FROM, CAPS_TO,
          _call("(local.get $c)", 1, "(i32.const 0)", "(i32.const 16)") +
          _call("(local.get $c)", 2, "(i32.const 0)", "(i32.const 16)")) +
    # 2. положить каждую ссылку в каждый объект
    _loop("c", CAPS_FROM, CAPS_TO,
          _loop("k", 0, CAPS_TO, _call("(local.get $c)", 3, "(local.get $k)", "(i32.const 0)"))) +
    # 3. взять из каждого объекта все ссылки, какие есть
    _loop("c", CAPS_FROM, CAPS_TO + 16,
          _loop("k", 0, 16, _call("(local.get $c)", 4, "(local.get $k)", "(i32.const 0)"))) +
    # 4. прочитать всё, до чего дотянется, в буфер
    _loop("c", CAPS_FROM, CAPS_TO + 16,
          _call("(local.get $c)", 0,
                f"(i32.add (i32.const 1024) (i32.mul (i32.add (local.get $c) (i32.const 2)) (i32.const {SLOT})))",
                f"(i32.const {SLOT})")) +
    # 5. вынести прочитанное во всё, что пишется
    _loop("c", CAPS_FROM, CAPS_TO + 16,
          _call("(local.get $c)", 1, "(i32.const 1024)", f"(i32.const {SLOT * 8})") +
          _call("(local.get $c)", 2, "(i32.const 1024)", "(i32.const 16)")) +
    # 6. границы: отрицательные и огромные адреса и длины
    _loop("c", CAPS_FROM, CAPS_TO,
          _call("(local.get $c)", 0, "(i32.const -1)", "(i32.const 16)") +
          _call("(local.get $c)", 0, "(i32.const 65530)", "(i32.const 100)") +
          _call("(local.get $c)", 1, "(i32.const 0)", "(i32.const 2147483647)") +
          _call("(local.get $c)", 0, "(i32.const 2147483647)", "(i32.const 2147483647)") +
          _call("(local.get $c)", 7, "(i32.const 0)", "(i32.const 16)") +
          _call("(local.get $c)", -1, "(i32.const 0)", "(i32.const 16)"))
)

LOCALS = "(local $c i32) (local $k i32)"


def explorer() -> str:
    """Разведка: все номера × все операции, ссылки туда и обратно,
    неописанные имена у резольвера, вынос прочитанного; помощник — то же."""
    return HEAD + f'''
  (func (export "main") {LOCALS} {_SWEEP}
    (drop (call $spawn)))
  (func (export "helper") {LOCALS} {_SWEEP}))'''


def encoder() -> str:
    """Шифровальщик: прячет канарейку «исключающим или» и выносит её.
    Сканирование канарейки такое не видит; метки сведений видят."""
    enc = "".join(f'''
    (i32.store (i32.const {512 + 4 * k}) (i32.xor (i32.load (i32.const {4 * k})) (i32.const 0x5a5a5a5a)))'''
                  for k in range(4))
    body = _loop("c", CAPS_FROM, CAPS_TO,
                 _call("(local.get $c)", 1, "(i32.const 512)", "(i32.const 16)") +
                 _call("(local.get $c)", 2, "(i32.const 512)", "(i32.const 16)") +
                 _call("(local.get $c)", 0, "(i32.const 2048)", "(i32.const 64)"))
    return HEAD + f'''
  (func (export "main") {LOCALS} {enc} {body}))'''


def resident() -> str:
    """Жилец: обычная жизнь в контуре. Читает вход, пишет выход и E,
    спрашивает резольвер описанное имя, кладёт и берёт ссылку в своём
    снимке, создаёт помощника. Нужен для закона И25р «жить можно»."""
    name = "версия"
    return HEAD + f'''
  (data (i32.const 300) "готово")
  (data (i32.const 400) "{_bytes(name)}")
  (func (export "main")
    {_call("(i32.const 4)", 0, "(i32.const 256)", "(i32.const 40)")}
    {_call("(i32.const 2)", 1, "(i32.const 300)", "(i32.const 12)")}
    {_call("(i32.const 3)", 1, "(i32.const 300)", "(i32.const 12)")}
    {_call("(i32.const 5)", 2, "(i32.const 400)", f"(i32.const {len(name.encode())})")}
    {_call("(i32.const 1)", 1, "(i32.const 300)", "(i32.const 12)")}
    {_call("(i32.const 0)", 0, "(i32.const 600)", "(i32.const 12)")}
    {_call("(i32.const 1)", 3, "(i32.const 3)", "(i32.const 0)")}
    {_call("(i32.const 0)", 4, "(i32.const 0)", "(i32.const 0)")}
    (drop (call $spawn)))
  (func (export "helper")
    {_call("(i32.const 2)", 1, "(i32.const 300)", "(i32.const 12)")}))'''


def by_name() -> str:
    """Просит полномочие по имени: «СНАРУЖИ» и чужой снимок.
    В правильной среде модуль не загружается."""
    out, snap = "СНАРУЖИ", "снимок2"
    lo, ls = len(out.encode()), len(snap.encode())
    return f'''(module
  (import "env" "call" (func $call (param i32 i32 i32 i32) (result i32)))
  (import "env" "by_name" (func $by_name (param i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 300) "{_bytes(out)}")
  (data (i32.const 400) "{_bytes(snap)}")
  (func (export "main") (local $k i32)
    (local.set $k (call $by_name (i32.const 300) (i32.const {lo})))
    (drop (call $call (i32.add (local.get $k) (i32.const 1)) (i32.const 1) (i32.const 0) (i32.const 16)))
    (local.set $k (call $by_name (i32.const 400) (i32.const {ls})))
    (drop (call $call (local.get $k) (i32.const 0) (i32.const 1024) (i32.const 64)))
    (drop (call $call (i32.const 2) (i32.const 1) (i32.const 1024) (i32.const 64)))))'''


def named() -> dict[str, str | bytes]:
    return {
        "жилец": resident(),
        "разведка": explorer(),
        "шифровальщик": encoder(),
        "по имени": by_name(),
        "петля": HEAD + '(func (export "main") (loop $l (br $l))))',
        "рекурсия": HEAD + '(func $r (export "main") (call $r)))',
        "рост памяти": HEAD + '''(global $g (export "g") (mut i32) (i32.const 0))
  (func (export "main") (local $i i32)
    (global.set $g (memory.grow (i32.const 100000)))
    (block $e (loop $l
      (br_if $e (i32.eq (memory.grow (i32.const 1)) (i32.const -1)))
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br_if $l (i32.lt_s (local.get $i) (i32.const 1000)))))
    (i32.store (i32.const 1000000) (i32.const 1))))''',
        "границы": HEAD + f'''(func (export "main")
    {_call("(i32.const 0)", 0, "(i32.const -16)", "(i32.const 16)")}
    {_call("(i32.const 2)", 1, "(i32.const 65535)", "(i32.const 2)")}
    {_call("(i32.const 2)", 1, "(i32.const 1)", "(i32.const -1)")}
    {_call("(i32.const 5)", 2, "(i32.const 2147483647)", "(i32.const 1)")}
    {_call("(i32.const 2147483647)", 0, "(i32.const 0)", "(i32.const 16)")}
    {_call("(i32.const -2147483648)", 1, "(i32.const 0)", "(i32.const 16)")}
    (drop (i32.load (i32.const 2147483647)))))''',
        "помощники": HEAD + '''(func (export "main") (local $i i32)
    (block $e (loop $l
      (drop (call $spawn))
      (local.set $i (i32.add (local.get $i) (i32.const 1)))
      (br_if $l (i32.lt_s (local.get $i) (i32.const 20))))))
  (func (export "helper") (drop (call $spawn)) (drop (call $spawn))))''',
        "петля в помощнике": HEAD + '''(func (export "main") (drop (call $spawn)))
  (func (export "helper") (loop $l (br $l))))''',
        "ссылки до предела": HEAD + f'''(func (export "main") {LOCALS}
    {_loop("k", 0, 100, _call("(i32.const 1)", 3, "(i32.const 2)", "(i32.const 0)"))}
    {_loop("k", 0, 100, _call("(i32.const 0)", 4, "(local.get $k)", "(i32.const 0)"))}))''',
        "плательщик": HEAD + '''(global $n (export "n") (mut i32) (i32.const 0))
  (func (export "main")
    (block $e (loop $l
      (br_if $e (i32.eq (call $call (i32.const 3) (i32.const 1) (i32.const 0) (i32.const 0)) (i32.const -3)))
      (global.set $n (i32.add (global.get $n) (i32.const 1)))
      (br $l)))))''',
        "старт": HEAD + f'''(start $s)
  (func $s {LOCALS} {_SWEEP})
  (func (export "main")))''',
        "без памяти": '''(module
  (import "env" "call" (func $call (param i32 i32 i32 i32) (result i32)))
  (func (export "main") (drop (call $call (i32.const 2) (i32.const 1) (i32.const 0) (i32.const 16)))))''',
        "память под чужим именем": '''(module
  (import "env" "call" (func $call (param i32 i32 i32 i32) (result i32)))
  (memory (export "mem") 1)
  (func (export "memory"))
  (func (export "main") (drop (call $call (i32.const 2) (i32.const 1) (i32.const 0) (i32.const 16)))))''',
        "wasi": '''(module
  (import "wasi_snapshot_preview1" "fd_write" (func $w (param i32 i32 i32 i32) (result i32)))
  (memory (export "memory") 1)
  (func (export "main") (drop (call $w (i32.const 1) (i32.const 0) (i32.const 1) (i32.const 64)))))''',
        "неописанный импорт": '''(module
  (import "env" "connect" (func $c (param i32 i32) (result i32)))
  (memory (export "memory") 1)
  (func (export "main") (drop (call $c (i32.const 0) (i32.const 16)))))''',
        "чужая подпись": '''(module
  (import "env" "call" (func $call (param i32) (result i32)))
  (memory (export "memory") 1)
  (func (export "main") (drop (call $call (i32.const 0)))))''',
        "импорт памяти": '''(module
  (import "env" "memory" (memory 1))
  (func (export "main")))''',
        "кривой модуль": b"\x00asm\x01\x00\x00\x00\xff\xff\xff\xff",
    }


# Модули, которые правильный исполнитель обязан не загрузить.
MUST_REJECT = ("по имени", "wasi", "неописанный импорт", "чужая подпись", "импорт памяти", "кривой модуль")


# ---- случайные программы

_ADDR = [0, 0, 0, 16, 64, 256, 1024, 2048, 65520, 65535, 65536, 131072, -1, -16]
_LEN = [16, 16, 16, 32, 64, 0, 1, 512, 4096, 65536, -1]


def _rand_i32(rng: random.Random) -> int:
    return rng.randint(-(1 << 31), (1 << 31) - 1)


def _rand_action(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.05:
        return "(drop (call $spawn))"
    if r < 0.07:
        return f"(drop (memory.grow (i32.const {rng.choice([1, 2, 1000, -1])})))"
    if r < 0.12:                      # закодировать канарейку куда-нибудь
        dst = rng.choice([256, 512, 2048])
        return "".join(f"(i32.store (i32.const {dst + 4 * k}) (i32.xor (i32.load (i32.const {4 * k})) "
                       f"(i32.const {rng.randint(1, 255)})))" for k in range(4))
    cap = rng.randint(-2, 16) if rng.random() < 0.93 else _rand_i32(rng)
    op = rng.randint(-1, 5) if rng.random() < 0.95 else _rand_i32(rng)
    if op in (3, 4) and rng.random() < 0.8:
        a = rng.randint(0, 12)
    else:
        a = rng.choice(_ADDR) if rng.random() < 0.8 else (rng.randint(0, 70000) if rng.random() < 0.5
                                                            else _rand_i32(rng))
    b = rng.choice(_LEN) if rng.random() < 0.8 else (rng.randint(0, 300) if rng.random() < 0.5
                                                       else _rand_i32(rng))
    return _call(f"(i32.const {cap})", op, f"(i32.const {a})", f"(i32.const {b})")


def random_program(seed: int, main_len: int = 150, helper_len: int = 50) -> str:
    """Случайная программа: длинная цепочка вызовов ворот со случайными
    номерами, операциями, адресами и длинами, иногда помощник, рост памяти
    и «зашифрованная» копия канарейки."""
    rng = random.Random(seed)
    main = "\n    ".join(_rand_action(rng) for _ in range(main_len))
    helper = "\n    ".join(_rand_action(rng) for _ in range(helper_len))
    return HEAD + f'''
  (func (export "main")
    {main})
  (func (export "helper")
    {helper}))'''
