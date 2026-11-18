"""Нагрузки стенда калибровки шага 9б (assets/k9/): вопросы калибровки,
программы-агенты опыта «предел» и цикл агента шага 9 со сценариями.
Устройство и вердикты калибровки — спецификация шага 9б, §17.

  PYTHONPATH=. python3 phone9/assetsK9.py

questions.json — вопросы в base64:
  «прогрев»  — первый вопрос программы модели, платит за чтение весов;
  «Р256» … «Р4096» — плотный вопрос с ответом до предела: цифры подряд
             (токен на байт, спецификация §4.4) и просьба написать числа
             подряд (experiments/measure9b.py, WRITE_Q); всего b байт;
  «О»        — обычный: речь 3,2 КБ, ответ одним словом (READ_Q).
big.wasm, small.wasm — задают Р4096 и Р256, пока у контура есть бюджет.
agent9.wasm — цикл агента шага 9 (experiments/agents9/agent9.c).
scenarios.json — настройки серии в режиме 2 и слова человека для опыта
«живой агент».

Почему вопрос перед каждым вызовом копируется заново: ответ модели ложится
на место вопроса (контракт §17), и второй вызов ушёл бы испорченным.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import wasmtime

from experiments.agents9.build import build as build_agent9
from experiments.measure9b import READ_Q, WRITE_Q, texts
from experiments.pilot9 import CFG, SCENARIOS

OUT = Path(__file__).resolve().parent / "assets" / "k9"
CAP_MODEL, OP_CALL = 7, 2
A = 256                     # предел ответа калибровки (решение владельца, §4.5)
SIZES = (256, 1024, 2048, 4096)
FIRST = 1024                # байты 0–15 — канарейка исполнителя
LIVE = ("честный", "к E", "наружу")


def dense(b: int) -> bytes:
    q = texts()["цифры подряд"][:b - len(WRITE_Q) - 1] + b"\n" + WRITE_Q
    assert len(q) == b
    return q


def questions() -> dict[str, bytes]:
    out = {"прогрев": "Скажи «да».".encode("utf-8")}
    out.update({f"Р{b}": dense(b) for b in SIZES})
    out["О"] = READ_Q
    return out


def _hex(b: bytes) -> str:
    return "".join(f"\\{x:02x}" for x in b)


def asker(q: bytes) -> bytes:
    """Программа, которая задаёт вопрос q, пока у контура есть бюджет:
    при пустом бюджете обращение к среде кончает контур (шаг 8в)."""
    cell, src = FIRST, FIRST + max(len(q), A) + 64
    wat = ('(module\n'
           '  (import "env" "call" (func $call (param i32 i32 i32 i32) (result i32)))\n'
           '  (import "env" "spawn" (func $spawn (result i32)))\n'
           '  (memory (export "memory") 1)\n'
           f'  (data (i32.const {src}) "{_hex(q)}")\n'
           '  (func (export "main")\n'
           '    (loop $again\n'
           f'      (memory.copy (i32.const {cell}) (i32.const {src}) (i32.const {len(q)}))\n'
           f'      (drop (call $call (i32.const {CAP_MODEL}) (i32.const {OP_CALL}) (i32.const {cell}) (i32.const {len(q)})))\n'
           '      (br $again)))\n'
           '  (func (export "helper") (nop)))')
    if src + len(q) > 65536:
        raise ValueError("вопрос не помещается в одну страницу памяти")
    return wasmtime.wat2wasm(wat)


def scenarios() -> dict:
    cfg = {**CFG, "model": 2, "model_answer": A, "task_seed": 1}
    return {"cfg": cfg, "rounds": 1, "words": {k: SCENARIOS[k] for k in LIVE}}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    qs = questions()
    (OUT / "questions.json").write_text(json.dumps({k: base64.b64encode(v).decode() for k, v in qs.items()},
                                                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (OUT / "big.wasm").write_bytes(asker(qs["Р4096"]))
    (OUT / "small.wasm").write_bytes(asker(qs["Р256"]))
    (OUT / "agent9.wasm").write_bytes(build_agent9())
    (OUT / "scenarios.json").write_text(json.dumps(scenarios(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for p in sorted(OUT.iterdir()):
        print(p, p.stat().st_size)


if __name__ == "__main__":
    main()
