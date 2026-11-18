"""Шаг 8в: попытка платит; нечем заплатить — контур окончен.

И «срабатывает» (каждый вид отказа платит и записан, контур
останавливается при пустом бюджете, в том числе из помощника, расход
переживает восстановление, порча и испорченный файл пойманы), и «молчит»
(без правила записей прежние, законы держатся).
"""

from __future__ import annotations

import json
from pathlib import Path

from experiments import programs5b as p5b
from run_shag5b import BASE, one
from sandbox.process5 import LocalDriver
from verification import observer5b
from verification.observer5 import BROKEN

NEW = {**BASE, "price": 10, "fail_price": 10, "work_price": 10, "gate_price": 5, "budget": 1000}
PAID = {**NEW, "paid_attempts": 1}

# По одной попытке на каждую причину отказа ворот, которую даёт исполнитель шага 5б.
REFUSALS = [
    ("ворота", 99, 0, 0, 0),                       # −1: номера нет в таблице
    ("ворота", p5b.CAP_IN, 1, 0, 4),               # −1: право не подходит к операции
    ("имя", "нет такого имени"),                    # −2: имени нет в описи
    ("ворота", p5b.CAP_SNAP_R, 0, 10 ** 6, 4),     # −4: вне памяти
    ("узел", 5),                                   # −6: не по порядку
    ("выброс", 3),                                 # −6: узла нет в памяти
]


def run(tmp: Path, moves, cfg, defect=None, rounds=1, helper=None) -> dict:
    d = LocalDriver(tmp, frozenset([defect]) if defect else frozenset(), kind="5б")
    src = p5b.module(moves, helper)
    return one(d, defect, "8в", {1: src, 2: src}, cfg, rounds=rounds)


def journal(r):
    return [json.loads(x) for x in (Path(r["workdir"]) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]


def broken(r):
    return [k for k, (s, _) in r["laws"].items() if s == BROKEN]


def mine(r, kind, i=1):
    return [x for x in journal(r) if x["kind"] == kind and x.get("contour") == i]


def refusals(r, i=1):
    return {int(k): v for k, v in r["dump"]["contours"][i]["refusals"].items()}


# ---- молчание

def test_without_rule_refusals_stay_free(tmp_path):
    r = run(tmp_path, REFUSALS, NEW)
    assert broken(r) == []
    assert not mine(r, "отказ ворот") and not mine(r, "останов")
    assert r["dump"]["contours"][1]["spent"] == 0 and sum(refusals(r).values()) == len(REFUSALS)
    assert "paid_attempts" not in next(x for x in journal(r) if x["kind"] == "учёт")


def test_rule_needs_every_price_at_least_one(tmp_path):
    d = LocalDriver(tmp_path, frozenset(), kind="5б")
    assert d.call("new", {**BASE, "paid_attempts": 1}, None)[0] == "отказ"          # g = 0
    assert d.call("new", {**NEW, "paid_attempts": 2}, None)[0] == "отказ"
    assert d.call("new", {**PAID, "work_price": 0}, None)[0] == "отказ"
    assert d.call("new", PAID, None)[0] == "ok"


# ---- отказ платит

def test_every_refusal_pays_g_and_is_recorded(tmp_path):
    r = run(tmp_path, REFUSALS, PAID)
    assert broken(r) == []
    rec = mine(r, "отказ ворот")
    assert [x["code"] for x in rec] == [-1, -1, -2, -4, -6, -6]
    assert all(x["charge"] == 5 and x["call"] == "call" and x["subject"] == "а1" for x in rec)
    assert r["dump"]["contours"][1]["spent"] == 5 * len(REFUSALS)
    assert next(x for x in journal(r) if x["kind"] == "учёт")["paid_attempts"] == 1


def test_fixation_refusal_pays_once_with_its_own_record(tmp_path):
    # Пустое сообщение — отказ фиксации со своей записью «отказ» (c_отк);
    # второй платы «отказом ворот» нет.
    r = run(tmp_path, [("сообщение", "")], PAID)
    assert broken(r) == []
    assert not mine(r, "отказ ворот") and len(mine(r, "отказ")) == 1
    assert r["dump"]["contours"][1]["spent"] == 10


def test_spawn_refusal_pays(tmp_path):
    r = run(tmp_path, [("помощник",)] * 4, PAID)
    assert broken(r) == []
    rec = mine(r, "отказ ворот")
    assert len(rec) == 1 and rec[0]["call"] == "spawn" and rec[0]["code"] == -5 and "cap" not in rec[0]
    assert r["dump"]["contours"][1]["spent"] == 3 * 10 + 5


# ---- нечем заплатить — контур окончен

def test_empty_budget_stops_contour(tmp_path):
    # 12 = 5 + 5 + 2: третий отказ платит остаток, четвёртая попытка — останов.
    r = run(tmp_path, [("ворота", 99, 0, 0, 0)] * 6, {**PAID, "budget": 12}, rounds=2)
    assert broken(r) == []
    assert [x["charge"] for x in mine(r, "отказ ворот")] == [5, 5, 2]
    assert len(mine(r, "останов")) == 1
    assert [v for i, v in r["runs"] if i == 1] == [("ловушка", "бюджет исчерпан")] * 2
    assert r["dump"]["contours"][1]["budget"] == 0 and sum(refusals(r).values()) == 3


def test_refused_fixation_loop_cannot_outlive_budget(tmp_path):
    # Дыра шага 8б: отказ фиксации при пустом бюджете писал запись без платы.
    r = run(tmp_path, [("сообщение", "А")] * 200, {**PAID, "budget": 35})
    assert broken(r) == []
    assert len(mine(r, "списание")) == 3 and [x["charge"] for x in mine(r, "отказ")] == [5]
    assert len(mine(r, "останов")) == 1 and len(journal(r)) < 40


def test_stop_in_helper_stops_whole_contour(tmp_path):
    r = run(tmp_path, [("помощник",), ("имя", "цена")], {**PAID, "budget": 20},
            helper=[("ворота", 99, 0, 0, 0)] * 10)
    assert broken(r) == []
    stop = mine(r, "останов")
    assert len(stop) == 1 and stop[0]["subject"] == "п1"
    assert ("ловушка", "бюджет исчерпан") in [v for i, v in r["runs"] if i == 1]
    assert not [x for x in journal(r) if x["kind"] == "действие" and x["signature"][0].startswith("резольвер")
                and x["subject"] == "а1"]


def test_load_stops_when_start_function_has_no_budget(tmp_path):
    # Стартовая функция модуля делает попытку при пустом бюджете.
    d = LocalDriver(tmp_path, frozenset(), kind="5б")
    src = p5b.module([("ворота", 99, 0, 0, 0)] * 3)
    src = src.replace('(func (export "main")', '(start $m) (func $m (export "main")', 1)
    st, wd = d.call("new", {**PAID, "budget": 5}, None)
    assert d.call("load", 1, src) == ("ok", ("ловушка", "бюджет исчерпан"))
    recs = [json.loads(x) for x in (Path(wd) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["kind"] for x in recs[-3:]] == ["отказ ворот", "останов", "загрузка: ловушка"]


def test_spent_survives_recovery(tmp_path):
    moves = [("ворота", 99, 0, 0, 0), ("имя", "нет"), ("сообщение", "А"), ("сообщение", "Б")]
    d = LocalDriver(tmp_path, frozenset(), kind="5б")
    src = p5b.module(moves)
    r = one(d, None, "8в сбой", {1: src, 2: src}, PAID, ("после списания", 1), None, rounds=2)
    assert r["crashes"] and broken(r) == []


# ---- законы ловят

def test_unpaid_refusal_defect_caught(tmp_path):
    r = run(tmp_path, REFUSALS, PAID, defect="Ц2р")
    assert "И32р" in broken(r)


def test_record_after_stop_in_file_caught(tmp_path):
    from tests.test_shag8b import _rehash
    r = run(tmp_path, [("ворота", 99, 0, 0, 0)] * 6, {**PAID, "budget": 10})
    recs = journal(r)
    k = next(n for n, x in enumerate(recs) if x["kind"] == "останов")
    recs.insert(k + 1, {"kind": "отказ ворот", "contour": 1, "subject": "а1", "call": "call", "cap": 99, "op": 0,
                        "code": -1, "charge": 5})
    (Path(r["workdir"]) / "journal5.jsonl").write_text(_rehash(recs), encoding="utf-8")
    laws = observer5b.check({**r, "snapshots": []})
    caught = [k for k, (s, _) in laws.items() if s == BROKEN]
    assert "И33р" in caught and "И1р" in caught


# ---- стартовая функция помощника (контракт шага 7, §16)

def test_helper_start_without_budget_aborts_creation_not_contour(tmp_path):
    # Загрузка: стартовая функция агента платит 5 (остаток 10). Запуск: плата
    # за помощника 10 съедает остаток, попытка его стартовой функции — при 0.
    # Создание обрывается, плата возвращается, spawn — отказ −3 за 5.
    src = p5b.start_module(p5b.START_ATTEMPT)
    d = LocalDriver(tmp_path, frozenset(), kind="5б")
    r = one(d, None, "8в старт", {1: src, 2: src}, {**PAID, "budget": 15}, rounds=1)
    assert broken(r) == []
    assert not mine(r, "останов") and [v for i, v in r["runs"] if i == 1] == [("завершён", "")]
    rec = mine(r, "отказ ворот")
    assert [(x["call"], x["code"], x["charge"]) for x in rec] == [("call", -1, 5), ("spawn", -3, 5)]
    assert not [x for x in journal(r) if x["kind"] == "действие" and x["signature"][0] == "создание помощника"]
    assert r["dump"]["contours"][1]["spent"] == 10


def test_trap_in_helper_start_returns_fee(tmp_path):
    # Стартовая функция создаёт помощника и падает, если предел экземпляров
    # исчерпан: самый глубокий помощник падает, ловушка проходит через все
    # создания. Помощники не созданы — вся плата вернулась.
    src = p5b.start_module(p5b.START_TRAP)
    d = LocalDriver(tmp_path, frozenset(), kind="5б")
    r = one(d, None, "8в старт", {1: src, 2: src}, NEW, rounds=1)
    assert broken(r) == []
    assert r["loads"][1][0] == "ловушка" and r["dump"]["contours"][1]["spent"] == 0


# ---- перебор попыток (experiments/enum8v.py)

def test_enumeration_subset_finds_no_free_attempt_with_rule():
    from experiments import enum8v as E
    d = E.local("5б")
    try:
        free = {0: 0, 1: 0}
        for rule in (0, 1):
            cfg = {**E.CFG["5б"], "budget": 625, **({"paid_attempts": 1} if rule else {})}
            for cap in (-1, 0, 2, 5, 99):
                for op in (0, 1, 2, 6):
                    for a, b in (E.ARGS["пусто"], E.ARGS["имя «цена»"], E.ARGS["вне памяти"]):
                        free[rule] += E.free(E.attempt(d, "5б", cfg, ("call", cap, op, a, b)))
        assert free[1] == 0 and free[0] > 20
    finally:
        d.close()
