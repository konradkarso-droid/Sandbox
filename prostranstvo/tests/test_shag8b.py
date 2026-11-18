"""Шаг 8б: плата за записи журнала, которые оставляют вызовы ворот.

И «срабатывает» (плата взята и записана, отказ −3 без записи, порча и
испорченный файл пойманы), и «молчит» (при прежних настройках записи
прежние, законы держатся).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.mir3 import World
from experiments import programs5b as p5b
from run_shag5b import BASE, one
from sandbox.process5 import LocalDriver
from verification import observer5b
from verification.observer5 import BROKEN
from verification.pebble_search import cheapest_e, cheapest_e_by_model, fast

NEW = {**BASE, "price": 10, "fail_price": 10, "work_price": 10, "gate_price": 5, "budget": 1000}
ALL_FEES = [("имя", "цена"), ("ворота", p5b.CAP_SNAP_W, 1, 0, 4), ("ворота", p5b.CAP_SNAP_R, 0, 64, 4),
            ("узел", 0), ("выброс", 0), ("помощник",)]


def run(tmp: Path, moves, cfg, defect=None, rounds=1) -> dict:
    d = LocalDriver(tmp, frozenset([defect]) if defect else frozenset(), kind="5б")
    src = p5b.module(moves)
    return one(d, defect, "8б", {1: src, 2: src}, cfg, rounds=rounds)


def journal(r):
    return [json.loads(x) for x in (Path(r["workdir"]) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]


def broken(r):
    return [k for k, (s, _) in r["laws"].items() if s == BROKEN]


def acts(r, rule):
    return [x for x in journal(r) if x["kind"] == "действие" and x["signature"][0] == rule and x["subject"] == "а1"]


# ---- поиск цены E

@pytest.mark.parametrize("g,k,m", [("M3т", 3, 3), ("M3т", 4, 4), ("M3ц", 4, 5)])
def test_cheapest_e_matches_old_search_and_model(g, k, m):
    w = World(g, k, m)
    assert cheapest_e(w, 1, 0) == fast(w).w_e
    for c, dcost in ((10, 10), (10, 3), (7, 1)):
        assert cheapest_e(w, c, dcost) == cheapest_e_by_model(w, c, dcost)


# ---- молчание: прежние настройки — прежние записи

def test_defaults_write_no_charges(tmp_path):
    r = run(tmp_path, ALL_FEES, BASE)
    j = journal(r)
    assert not [x for x in j if "charge" in x and x["kind"] == "действие"]
    assert all(x["price"] == 0 for x in j if x["kind"] == "выброс")
    uch = next(x for x in j if x["kind"] == "учёт")
    assert "gate_price" not in uch and "check_price" not in uch       # журнал побайтно прежний
    assert broken(r) == []


# ---- срабатывание

def test_every_fee_charged_and_recorded(tmp_path):
    r = run(tmp_path, ALL_FEES, NEW)
    assert broken(r) == []
    assert [x["charge"] for x in acts(r, "резольвер: имя из описи")] == [5]
    assert [x["charge"] for x in acts(r, "запись")] == [5]          # запись в снимок, не фиксация
    assert [x["charge"] for x in acts(r, "чтение")] == [5]
    helper = [x for x in journal(r) if x["kind"] == "действие" and x["signature"][0] == "создание помощника"
              and x["subject"] == "п1"]
    assert [x["charge"] for x in helper] == [10]
    assert [x["price"] for x in journal(r) if x["kind"] == "выброс" and x["contour"] == 1] == [10]
    c = r["dump"]["contours"][1]
    assert c["spent"] == 5 + 5 + 5 + 10 + 10 + 10 and c["budget"] == 1000 - c["spent"]


def test_short_budget_refuses_without_record(tmp_path):
    r = run(tmp_path, [("имя", "цена")] * 4, {**NEW, "budget": 12})
    assert broken(r) == []
    assert len(acts(r, "резольвер: имя из описи")) == 2
    c = r["dump"]["contours"][1]
    assert c["budget"] == 2 and c["refusals"].get(-3, c["refusals"].get("-3")) == 2


def test_spawn_fee_returned_when_helper_not_created(tmp_path):
    # Предел — 4 экземпляра на контур вместе с агентом: четвёртый помощник не создаётся.
    r = run(tmp_path, [("помощник",)] * 4, NEW)
    assert broken(r) == []
    c = r["dump"]["contours"][1]
    assert c["spent"] == 3 * 10 and c["refusals"].get(-5, c["refusals"].get("-5")) == 1



def test_instance_limit_checked_before_budget(tmp_path):
    # Бюджета хватает ровно на три помощника. Четвёртый упирается и в предел
    # экземпляров, и в бюджет; отказ — «нет места» (−5), а не «нет денег» (−3):
    # предел проверяется раньше платы (контракт шага 7, §15).
    r = run(tmp_path, [("помощник",)] * 4, {**NEW, "budget": 30})
    assert broken(r) == []
    c = r["dump"]["contours"][1]
    ref = {int(k): v for k, v in c["refusals"].items()}
    assert c["spent"] == 30 and ref.get(-5) == 1 and -3 not in ref

def test_free_gates_defect_caught_by_fee_law(tmp_path):
    r = run(tmp_path, ALL_FEES, NEW, defect="Ц1р")
    assert "И31р" in broken(r)
    assert "И1р" not in broken(r) and "И5р" not in broken(r)       # счёт сходится — дыру видит только И31р


def _rehash(recs: list[dict]) -> str:
    prev, out = "0" * 64, []
    for n, r in enumerate(recs):
        body = {k: v for k, v in r.items() if k not in ("seq", "prev", "hash")}
        body.update(seq=n, prev=prev)
        h = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        out.append(json.dumps({**body, "hash": h}, ensure_ascii=False, sort_keys=True))
        prev = h
    return "\n".join(out) + "\n"


def test_removed_charge_in_file_caught(tmp_path):
    r = run(tmp_path, ALL_FEES, NEW)
    recs = journal(r)
    k = next(i for i, x in enumerate(recs) if x.get("charge"))
    del recs[k]["charge"]
    (Path(r["workdir"]) / "journal5.jsonl").write_text(_rehash(recs), encoding="utf-8")
    laws = observer5b.check({**r, "snapshots": []})
    caught = [k for k, (s, _) in laws.items() if s == BROKEN]
    assert "И1р" in caught and "И31р" in caught


# ---- окно бюджета: расчёт на прежних ценах даёт прежние числа

def test_window_rule_reproduces_step6(tmp_path):
    import run_shag8b as R
    wnd = R.window(LocalDriver, tmp_path, R.OLD)
    assert (wnd["U"], wnd["L"], wnd["B"], wnd["B_M2"]) == (58, 27, 40, 17)


# ---- что нашла серия: восстановление после сбоя и цена проверки на новой шкале

def test_restore_after_crash_counts_fees(tmp_path):
    moves = [("имя", "цена"), ("ворота", p5b.CAP_SNAP_W, 1, 0, 4), ("сообщение", "А"), ("сообщение", "Б")]
    d = LocalDriver(tmp_path, frozenset(), kind="5б")
    src = p5b.module(moves)
    r = one(d, None, "8б сбой", {1: src, 2: src}, NEW, ("после списания", 1), None, rounds=2)
    assert r["crashes"] and broken(r) == []


def test_step6_check_price_scale(tmp_path):
    import run_shag6 as S6
    import run_shag8b as R
    d = LocalDriver(tmp_path, frozenset(), kind="6")
    cfg = {**S6.MAIN6, **R.PRICES, "levels": 5, "budget": 10 ** 4}
    for name, defs in (("честный", {"HONEST": None, "NO_E": None}), ("с ошибками", {"ERR_MASK": "0x1fULL", "NO_E": None})):
        r = S6.run6(d, None, name, defs, cfg, expect_all=True)
        assert S6.broken(S6.summary_row(r)) == [], name
