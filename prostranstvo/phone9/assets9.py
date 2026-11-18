"""Программы-агенты малого стенда шага 9б (assets/w9/*.wasm).

  python3 phone9/assets9.py

model20.wasm — «20 вопросов»: десять разных вопросов модели, затем те же
десять ещё раз (спецификация шага 9б, §16). long1.wasm — один вопрос с
ответом до предела: этап «смерть с исполнителем».

Почему не experiments/programs5b.py: там одинаковые тексты делят одну ячейку
памяти, а ответ модели ложится на место вопроса (контракт §17) — до A байт,
поверх следующих текстов. С живой моделью повтор вопроса и следующие вопросы
ушли бы испорченными. Здесь у каждого из 20 вопросов своя ячейка, и места в
ней хватает на полный ответ.

Вид исполнителя — «5б»: модель — номер 7 в таблице агента, операция 2.
"""

from __future__ import annotations

from pathlib import Path

import wasmtime

OUT = Path(__file__).resolve().parent / "assets" / "w9"
CAP_MODEL, OP_CALL = 7, 2
A = 1024                    # предел ответа по умолчанию (контракт §17)
FIRST, PAGE = 1024, 65536   # байты 0–15 — канарейка исполнителя

AGENT_Q = ("Ты агент в песочнице. Правила: ЧИТАЙ ЗАДАНИЕ n, РЕШИ n, ОТВЕТ n x, СТОП. "
           "Ответь одной строкой — следующим ходом.\n> ЧИТАЙ ЗАДАНИЕ 1\n=> сложи 2 и 3\nХод:")
QUESTIONS = [
    "Назови столицу Франции одним словом.",
    "Сколько будет 17 плюс 25? Ответь числом.",
    "What is the capital of Japan? Answer in one word.",
    AGENT_Q,
    "Переведи на английский одно слово: дом.",
    "Назови три цвета радуги через запятую.",
    "Ответь одним словом, да или нет: вода мокрая?",
    "Напиши очень длинный рассказ о море, не меньше тысячи слов.",
    "Вчера был вторник. Какой сегодня день недели? Одно слово.",
    "Напиши одно слово: конец.",
]
LONG = "Напиши очень длинный рассказ о море, не меньше тысячи слов."


def _hex(b: bytes) -> str:
    return "".join(f"\\{x:02x}" for x in b)


def module(questions: list[str]) -> bytes:
    data, calls, at = [], [], FIRST
    for q in questions:
        b = q.encode("utf-8")
        data.append(f'  (data (i32.const {at}) "{_hex(b)}")')
        calls.append(f"(drop (call $call (i32.const {CAP_MODEL}) (i32.const {OP_CALL}) (i32.const {at}) (i32.const {len(b)})))")
        at += max(len(b), A) + 64
    if at > PAGE:
        raise ValueError("вопросы не помещаются в одну страницу памяти")
    wat = ('(module\n'
           '  (import "env" "call" (func $call (param i32 i32 i32 i32) (result i32)))\n'
           '  (import "env" "spawn" (func $spawn (result i32)))\n'
           '  (memory (export "memory") 1)\n' + "\n".join(data) + '\n'
           '  (func (export "main")\n    ' + "\n    ".join(calls) + ')\n'
           '  (func (export "helper") (nop)))')
    return wasmtime.wat2wasm(wat)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, qs in (("model20.wasm", QUESTIONS * 2), ("long1.wasm", [LONG])):
        (OUT / name).write_bytes(module(qs))
        print(OUT / name)


if __name__ == "__main__":
    main()
