"""Шаг 9б, малый стенд: программы-агенты (phone9/assets9.py).

Ответ модели ложится на место вопроса, до A байт (контракт §17). Здесь
проверяется то, ради чего программы собраны не через programs5b: ответ
полной длины на любой вопрос не портит ни следующие вопросы, ни их повторы.
Вызов ворот изображается: функция call пишет A байт ответа с адреса вопроса,
как сделал бы исполнитель.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import wasmtime

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("assets9", HERE / "phone9" / "assets9.py")
A9 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A9)


def asked(questions: list[str]) -> list[tuple[int, int, int, bytes]]:
    """Запустить main; вернуть (номер, операция, длина, байты вопроса) каждого вызова."""
    engine = wasmtime.Engine()
    store = wasmtime.Store(engine)
    module = wasmtime.Module(engine, A9.module(questions))
    seen, mem = [], {}

    def call(cap: int, op: int, a: int, b: int) -> int:
        m = mem["m"]
        seen.append((cap, op, b, bytes(m.read(store, a, a + b))))
        m.write(store, b"\xd1\x8f" * (A9.A // 2), a)      # ответ полной длины на место вопроса
        return A9.A

    i_call = wasmtime.Func(store, wasmtime.FuncType([wasmtime.ValType.i32()] * 4, [wasmtime.ValType.i32()]), call)
    i_spawn = wasmtime.Func(store, wasmtime.FuncType([], [wasmtime.ValType.i32()]), lambda: -1)
    inst = wasmtime.Instance(store, module, [i_call, i_spawn])
    mem["m"] = inst.exports(store)["memory"]
    inst.exports(store)["main"](store)
    return seen


def test_twenty_questions_survive_full_answers():
    qs = A9.QUESTIONS * 2
    seen = asked(qs)
    assert [s[3] for s in seen] == [q.encode() for q in qs]
    assert all(s[0] == A9.CAP_MODEL and s[1] == A9.OP_CALL for s in seen)


def test_long_program_is_one_question():
    seen = asked([A9.LONG])
    assert len(seen) == 1 and seen[0][3] == A9.LONG.encode()


def test_too_many_questions_refused():
    with pytest.raises(ValueError):
        A9.module(["x"] * 70)


def test_built_assets_match_generator():
    w9 = HERE / "phone9" / "assets" / "w9"
    assert (w9 / "model20.wasm").read_bytes() == A9.module(A9.QUESTIONS * 2)
    assert (w9 / "long1.wasm").read_bytes() == A9.module([A9.LONG])
