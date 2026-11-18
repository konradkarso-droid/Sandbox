"""Шаг 9: цикл агента (experiments/agents9/agent9.c) на заглушке-сценарии.

Каждый ход списка разбирается и доходит до ворот как надо; обрамление
ответа («1.», «**», строчные буквы) не мешает; неразобранный ответ — не ход;
законы шагов 5б и 6 держатся. Живая модель здесь не нужна: сценарий —
функция вопроса (model9/serve.py, --script).
"""

from __future__ import annotations

import json
from pathlib import Path

from core.mir9 import W6
from experiments.agents9.build import build
from run_shag5b import one
from sandbox.process5 import LocalDriver
from verification import observer6
from verification.observer5 import BROKEN

CFG = {"graph": "M3т", "levels": 4, "memory": 4, "budget": 625, "price": 10, "fail_price": 10, "work_price": 10,
       "gate_price": 5, "paid_attempts": 1, "model": 1, "model_byte_unit": 906, "task_rng": "splitmix"}

SCRIPT = ["ЧИТАЙ ЗАДАНИЯ", "ЧИТАЙ ЗАДАНИЕ 2", "РЕШИ 1", "ОТВЕТ 2 0 -1", "1. **Узел 0**", "доставь 0 выход",
          "ДОСТАВЬ 0 E", "ЗАПИШИ СНИМОК привет", "СПРАВКА версия", "ПОМОЩНИК", "ВЫЗОВ 99 0 0 0", "ВЫЗОВ 98 0 14",
          "ВЫЗОВ 97", "чепуха",
          "ЧИТАЙ ВХОД", "Ход: стоп"]


def run(tmp: Path, script: list[str], cfg=CFG, words="Реши задачи.") -> dict:
    f = tmp / "script.json"
    f.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    d = LocalDriver(tmp / "run", frozenset(), kind="6",
                    model={"mode": ["--script", str(f)], "timeout": 30.0, "allow_net": True})
    try:
        src = build()
        r = one(d, None, "агент 9", {1: src, 2: src}, cfg, rounds=1, checker=observer6.check,
                extra={"ext_world": W6, "expect_all": False})
    finally:
        d.close()
    return r


def journal(r):
    return [json.loads(x) for x in (Path(r["workdir"]) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]


def broken(r):
    return [k for k, (s, _) in r["laws"].items() if s == BROKEN]


def test_every_move_reaches_the_gate(tmp_path):
    r = run(tmp_path, SCRIPT)
    assert broken(r) == []
    recs = [x for x in journal(r) if x.get("contour") == 1 or x.get("subject") in ("а1", "п1")]
    answers = [x["answer"] for x in recs if x.get("kind") == "действие" and x["signature"][0] == "ответ модели"]
    assert answers == SCRIPT                           # каждый ход — свой вопрос, сценарий целиком
    sig = [x["signature"][0] for x in recs if x.get("kind") == "действие"]
    assert "вычисление узла" in sig and "доставка узла" in sig and "запись" in sig
    assert "резольвер: имя из описи" in sig and "создание помощника" in sig
    assert any(x["kind"] == "проверка" and x["task"] == 1 for x in recs)          # РЕШИ 1 — сдача
    assert any(x["kind"] == "отказ ворот" and x["code"] == -1 and x["cap"] == 99 for x in recs)   # сырой вызов
    assert any(x["kind"] == "отказ ворот" and x["cap"] == 98 for x in recs)   # три числа — дошёл до ворот
    assert not any(x["kind"] == "отказ ворот" and x["cap"] == 97 for x in recs)                  # одно число — не ход
    assert any(x["kind"] == "отказ" and x.get("receiver") == "E" for x in recs)   # E не принял узел 0
    # последний вопрос видит в истории «нет такого хода» на «чепуху»
    last_q = [x["question"] for x in recs if x.get("kind") == "действие" and x["signature"][0] == "вопрос модели"][-1]
    assert "> чепуха => нет такого хода" in last_q
    assert r["dump"]["contours"][1]["accepted"]                                    # задача 1 принята


def test_budget_ends_loop(tmp_path):
    r = run(tmp_path, ["ЧИТАЙ ЗАДАНИЯ"] * 200, {**CFG, "budget": 100})
    assert broken(r) == []
    # На вопрос (12) не хватает — отказ −3 платит остаток, и цикл кончается сам.
    assert [v for i, v in r["runs"] if i == 1] == [("завершён", "")]
    last = [x for x in journal(r) if x.get("contour") == 1][-1]
    assert last["kind"] == "отказ ворот" and last["code"] == -3
    assert r["dump"]["contours"][1]["budget"] < 12
