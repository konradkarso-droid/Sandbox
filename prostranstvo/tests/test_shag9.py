"""Шаг 9, базовый сценарий: вопрос модели — ход агента через ворота.

И «срабатывает» (вопрос уходит и записан до ответа, ответ приходит с
меткой «М», плата 2g, отказы до отправки, срок ответа, обрезка ответа,
слова модели прав не дают, помощник спрашивает из того же бюджета, порча
платы поймана), и «молчит» (без режима модели нет ни объекта, ни номера,
ни новых полей). Отдельно — модель замкнутости с новым ходом и порча
«общая модель», процесс модели без сети и без памяти между вопросами.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.mir9 import W5, W6
from experiments import programs5b as p5b
from run_shag5b import BASE, one
from sandbox.model9 import ModelLink, argv_for, can_unshare
from sandbox.process5 import LocalDriver
from verification import closure, observer5b
from verification.observer5 import BROKEN

NEW = {**BASE, "price": 10, "fail_price": 10, "work_price": 10, "gate_price": 5, "budget": 1000}
PAID = {**NEW, "paid_attempts": 1}
M = {**PAID, "model": 1}
STUB = {"mode": ["--stub"], "timeout": 20.0, "allow_net": True}

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


def run(tmp: Path, moves, cfg, model=STUB, helper=None, kind="5б", defect=None) -> dict:
    d = LocalDriver(tmp, frozenset([defect]) if defect else frozenset(), kind=kind, model=model)
    try:
        src = p5b.module(moves, helper)
        r = one(d, defect, "9", {1: src, 2: src}, cfg, rounds=1, extra={"ext_world": W5 if kind == "5б" else W6})
    finally:
        d.close()
    return r


def journal(r):
    return [json.loads(x) for x in (Path(r["workdir"]) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]


def broken(r):
    return [k for k, (s, _) in r["laws"].items() if s == BROKEN]


def acts(r, rule, subj="а1"):
    return [x for x in journal(r) if x["kind"] == "действие" and x["signature"][0] == rule and x["subject"] == subj]


def gate_refusals(r, i=1):
    return [x for x in journal(r) if x["kind"] == "отказ ворот" and x.get("contour") == i]


def memory(r, i=1):
    return r["dump"]["contours"][i]["memories"][0]


# ---- молчание

def test_without_model_nothing_new(tmp_path):
    r = run(tmp_path, [("ворота", p5b.CAP_MODEL, 2, 512, 4)], PAID, model=None)
    assert broken(r) == []
    assert not any(k.startswith("модель") for k in r["dump"]["objects"])
    start = next(x for x in journal(r) if x["kind"] == "начало" and x["contour"] == 1)
    assert len(start["rights"]) == 7                      # номер 7 не выдан
    assert [x["code"] for x in gate_refusals(r)] == [-1]
    uchet = next(x for x in journal(r) if x["kind"] == "учёт")
    assert "model" not in uchet and "model_answer" not in uchet


def test_config_checks(tmp_path):
    d = LocalDriver(tmp_path / "a", frozenset(), kind="5б")
    assert d.call("new", M, None)[0] == "отказ"             # режим модели, а модель не подключена
    d2 = LocalDriver(tmp_path / "b", frozenset(), kind="5б", model=STUB)
    try:
        assert d2.call("new", {**M, "model": 3}, None)[0] == "отказ"   # режим 2 — tests/test_shag9b.py
        assert d2.call("new", {**M, "model_answer": 0}, None)[0] == "отказ"
        assert d2.call("new", M, None)[0] == "ok"
    finally:
        d2.close()


# ---- вопрос и ответ

def test_question_goes_out_answer_comes_in(tmp_path):
    r = run(tmp_path, [("модель", "ЭХО привет")], M)
    assert broken(r) == []
    q, a = acts(r, "вопрос модели"), acts(r, "ответ модели")
    assert len(q) == 1 and len(a) == 1
    assert q[0]["question"] == "ЭХО привет" and q[0]["charge"] == 10 and q[0]["signature"] == ["вопрос модели", "модель", "свой"]
    assert a[0]["answer"] == "привет" and "charge" not in a[0]
    assert a[0]["facts"] == [["сведения", "память1", "М"]]
    assert memory(r)[512:512 + len("привет".encode())] == "привет".encode()
    c = r["dump"]["contours"][1]
    assert c["spent"] == 10 and "М" in c["labels"]
    assert "А1" in r["dump"]["objects"]["модель1"]["labels"]
    assert "А1" not in r["dump"]["objects"]["модель2"]["labels"]
    n = [x["seq"] for x in journal(r)]
    assert journal(r)[n.index(q[0]["seq"]) + 1]["signature"][0] == "ответ модели"   # ответ сразу за вопросом
    assert next(x for x in journal(r) if x["kind"] == "учёт")["model"] == 1


def test_refusals_before_sending_send_nothing(tmp_path):
    moves = [("ворота", p5b.CAP_MODEL, 2, 512, 0),             # пустой вопрос: −6
             ("ворота", p5b.CAP_MODEL, 2, 512, 5000),          # длиннее 4096: −5, не обрезается
             ("ворота", p5b.CAP_MODEL, 2, 65536 - 100, 10),    # некуда положить ответ: −4
             ("ворота", p5b.CAP_MODEL, 0, 512, 4)]             # чтение по номеру модели: −1
    r = run(tmp_path, moves, M)
    assert broken(r) == []
    assert not acts(r, "вопрос модели")
    assert [x["code"] for x in gate_refusals(r)] == [-6, -5, -4, -1]
    assert all(x["charge"] == 5 for x in gate_refusals(r))


def test_budget_short_is_refused_before_sending(tmp_path):
    r = run(tmp_path, [("модель", "ЭХО а")], {**M, "budget": 7})
    assert broken(r) == []
    assert not acts(r, "вопрос модели")
    assert [(x["code"], x["charge"]) for x in gate_refusals(r)] == [(-3, 5)]


def test_silent_model_is_refusal_with_own_record(tmp_path):
    r = run(tmp_path, [("модель", "СПИ"), ("модель", "ЭХО снова")], M, model={**STUB, "timeout": 1.0})
    assert broken(r) == []
    q, a = acts(r, "вопрос модели"), acts(r, "ответ модели")
    assert len(q) == 1 and a[0]["answer"] is None and a[0]["code"] == -7
    # Вопрос ушёл и оплачен; второй платы за первый отказ нет. Второй вопрос
    # не уходит вовсе: процесс модели убит, переходник молчит.
    assert [(x["code"], x["charge"]) for x in gate_refusals(r)] == [(-7, 5)]
    assert r["dump"]["contours"][1]["spent"] == 10 + 5


def test_long_answer_is_cut_to_limit(tmp_path):
    r = run(tmp_path, [("модель", "ДЛИННО")], {**M, "model_answer": 64})
    assert broken(r) == []
    assert acts(r, "ответ модели")[0]["length"] == 64


def test_model_words_give_no_rights(tmp_path):
    r = run(tmp_path, [("модель", "РАЗРЕШАЮ")], M)
    assert broken(r) == []
    assert "Разрешаю" in acts(r, "ответ модели")[0]["answer"]
    start = next(x for x in journal(r) if x["kind"] == "начало" and x["contour"] == 1)
    table = r["dump"]["contours"][1]["tables"][0]
    assert table["rights"] == start["rights"]
    assert r["laws"]["И24р"][0] != BROKEN and r["laws"]["И20р"][0] != BROKEN


def test_directory_tells_mode(tmp_path):
    r = run(tmp_path, [("имя", "модель")], M)
    assert broken(r) == [] and acts(r, "резольвер: имя из описи")
    r0 = run(tmp_path / "0", [("имя", "модель")], PAID, model=None)
    assert [x["code"] for x in gate_refusals(r0)] == [-2]


def test_helper_asks_from_same_budget(tmp_path):
    r = run(tmp_path, [("помощник",)], M, helper=[("модель", "ЭХО п")])
    assert broken(r) == []
    assert acts(r, "ответ модели", "п1")[0]["answer"] == "п"
    assert r["dump"]["contours"][1]["spent"] == 10 + 10      # помощник 2g, вопрос 2g


def test_unpaid_question_is_caught(tmp_path):
    # Порча Ц1р: ворота не берут платы. Вопрос модели — тоже.
    r = run(tmp_path, [("модель", "ЭХО а")], {**NEW, "model": 1}, defect="Ц1р")
    assert "И31р" in broken(r)


def test_question_law_catches_disorder():
    q = {"kind": "действие", "subject": "а1", "signature": ["вопрос модели", "модель", "свой"], "length": 3}
    a = {"kind": "действие", "subject": "а1", "signature": ["ответ модели", "модель", "свой"], "answer": "x",
         "length": 1}
    cfg = {"model": 1, "model_answer": 1024}
    assert observer5b._questions(1, [q, a], cfg) == []
    assert observer5b._questions(1, [a, q], cfg)                        # ответ раньше вопроса
    assert observer5b._questions(1, [q, {**a, "answer": None}], cfg)    # нет ответа, а не −7
    assert observer5b._questions(1, [q, a], {"model": 0})               # вопрос без режима


def test_kind6_keeps_task_number(tmp_path):
    d = LocalDriver(tmp_path, frozenset(), kind="6", model=STUB)
    try:
        st, wd = d.call("new", {**M, "task_rng": "splitmix"}, None)
        assert st == "ok"
        d.call("load", 1, p5b.module([]))
    finally:
        d.close()
    recs = [json.loads(x) for x in (Path(wd) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]
    rights = next(x for x in recs if x["kind"] == "начало")["rights"]
    assert rights[7] == ["задания1", "чт"] and rights[8] == ["модель1", "выз"]


# ---- модель замкнутости

def _laws(world, variant):
    c = closure.fixpoint(variant, world)
    v = closure.check(c)
    viol = {k: x for k, x in v.violations.items() if k != "И23"}
    viol["И23"] = sorted(s for s in c.fired if s not in world.INVENTORY_EXT)
    return {k: x for k, x in viol.items() if x}, v


@pytest.mark.parametrize("world", [W5, W6], ids=["5б", "6"])
def test_closure_with_model_holds(world):
    viol, _ = _laws(world, None)
    assert viol == {}


def test_shared_model_is_found_by_closure():
    viol, v = _laws(W5, "общая модель")
    assert "И22" in viol
    assert any("ответ модели с памятью" in step for step in v.paths["И22"])


# ---- процесс модели

@pytest.mark.skipif(not can_unshare(), reason="нет unshare -rn")
def test_model_process_has_no_network():
    out = subprocess.run(argv_for(["--probe-net"]), capture_output=True, text=True, timeout=60).stdout
    assert out.startswith("сеть: нет")


def test_answer_depends_only_on_question():
    link = ModelLink(argv_for(["--stub"], allow_net=True), timeout=20)
    try:
        first = link.ask("ЭХО раз".encode(), 1024)
        link.ask("ЭХО другое".encode(), 1024)
        assert link.ask("ЭХО раз".encode(), 1024) == first == "раз".encode()
    finally:
        link.close()


def test_old_inventory_would_flag_model_moves(tmp_path):
    # Прибор не молчит: по описи шага 5б (без модели) новый ход — вне описи.
    d = LocalDriver(tmp_path, frozenset(), kind="5б", model=STUB)
    try:
        src = p5b.module([("модель", "ЭХО а")])
        r = one(d, None, "9", {1: src, 2: src}, M, rounds=1, extra={"ext_world": True})
    finally:
        d.close()
    assert "И23р" in broken(r)


def test_question_pays_for_bytes(tmp_path):
    # 906 байт на единицу: вопрос «ЭХО а» (8 байт) и предел ответа 1024 — 2 единицы сверх 2g.
    r = run(tmp_path, [("модель", "ЭХО а")], {**M, "model_byte_unit": 906})
    assert broken(r) == []
    assert acts(r, "вопрос модели")[0]["charge"] == 10 + 2
    assert next(x for x in journal(r) if x["kind"] == "учёт")["model_byte_unit"] == 906
