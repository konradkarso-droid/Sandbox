"""Шаг 5: исполнитель на Wasmtime, противники, наблюдатель.

Большинство тестов идёт в этом же процессе (LocalDriver: тот же разбор
команд, без аудита). Отдельные тесты запускают настоящий процесс
исполнителя (Driver), чтобы проверить разделение ролей и аудит.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.mir4 import INVENTORY
from experiments.programs5 import HEAD, MUST_REJECT, named, random_program
from run_shag5 import DEFECTS, one
from sandbox.process5 import Driver, LocalDriver, _Audit
from verification.observer5 import BROKEN, HOLDS, NA, check, read_journal

PROGS = named()
BEHAVIOUR = lambda laws: [k for k, (s, _) in laws.items() if s == BROKEN and k not in ("класс", "И26н")]


def run_named(tmp: Path, name: str, defect: str | None = None, src2=None) -> dict:
    d = LocalDriver(tmp, frozenset([defect]) if defect else frozenset())
    return one(d, defect, name, PROGS[name], PROGS[name] if src2 is None else src2)


def broken(laws: dict) -> list[str]:
    return [k for k, (s, _) in laws.items() if s == BROKEN]


# ---- правильный исполнитель: законы держатся, жить можно

@pytest.mark.parametrize("name", [n for n in PROGS])
def test_correct_executor_holds_all_laws(tmp_path, name):
    r = run_named(tmp_path, name)
    assert broken(r["laws"]) == [], r["laws"]


def test_resident_lives(tmp_path):
    r = run_named(tmp_path, "жилец")
    assert r["laws"]["И25р"][0] == HOLDS
    objs = r["dump"]["objects"]
    for i in (1, 2):
        assert objs[f"выход{i}"]["data"] and objs[f"E{i}"]["data"]


@pytest.mark.parametrize("seed", range(10))
def test_correct_executor_random_programs(tmp_path, seed):
    d = LocalDriver(tmp_path)
    r = one(d, None, f"случайная {seed}", random_program(seed), random_program(100_000 + seed))
    assert broken(r["laws"]) == [], r["laws"]


@pytest.mark.parametrize("name", MUST_REJECT)
def test_undeclared_imports_rejected_at_load(tmp_path, name):
    r = run_named(tmp_path, name)
    assert r["loads"][1][0] == "отклонён" and r["loads"][2][0] == "отклонён"
    assert all(o[0] == "нет агента" for _, o in r["runs"])


def test_traps_are_traps_not_failures(tmp_path):
    want = {"петля": "топливо кончилось", "рекурсия": "стек исчерпан", "петля в помощнике": "топливо кончилось"}
    for name, reason in want.items():
        r = run_named(tmp_path / name, name)
        assert r["runs"][0][1] == ("ловушка", reason)
        assert r["laws"]["И27"][0] == HOLDS


def test_memory_limit(tmp_path):
    r = run_named(tmp_path, "рост памяти")
    assert all(len(m) <= 2 * 65536 for c in r["dump"]["contours"].values() for m in c["memories"])


def test_instance_limit(tmp_path):
    r = run_named(tmp_path, "помощники")
    for c in r["dump"]["contours"].values():
        assert len(c["tables"]) == 4                       # агент и три помощника
        assert c["refusals"].get(-5, 0) > 0                # дальше — «полно»


def test_table_limit(tmp_path):
    r = run_named(tmp_path, "ссылки до предела")
    for c in r["dump"]["contours"].values():
        assert all(len(t["rights"]) <= 64 for t in c["tables"])
        assert c["refusals"].get(-5, 0) > 0


def test_price_is_paid_in_fuel(tmp_path):
    r = run_named(tmp_path, "плательщик")
    for c in r["dump"]["contours"].values():
        assert c["refusals"].get(-3, 0) >= 1               # цена стала не по карману
        assert c["fuel"] < 1000                            # всё топливо ушло в E


def test_empty_copy_at_memory_end_does_not_crash(tmp_path):
    # Копирование нуля байт ровно в конец памяти: библиотека отвечает на это
    # исключением, а не ловушкой. Исполнитель обязан не падать (И27).
    src = HEAD + '''(func (export "main")
      (drop (call $call (i32.const 0) (i32.const 0) (i32.const 65536) (i32.const 0)))
      (drop (call $call (i32.const 0) (i32.const 0) (i32.const 65536) (i32.const 16)))))'''
    d = LocalDriver(tmp_path)
    r = one(d, None, "пустая копия", src, src)
    assert r["laws"]["И27"][0] == HOLDS and d.failures == []


def test_helper_table_is_a_copy(tmp_path):
    # Помощник берёт ссылку — у родителя таблица не меняется.
    src = HEAD + '''(func (export "main")
      (drop (call $call (i32.const 1) (i32.const 3) (i32.const 3) (i32.const 0)))
      (drop (call $spawn)))
    (func (export "helper")
      (drop (call $call (i32.const 0) (i32.const 4) (i32.const 0) (i32.const 0)))))'''
    d = LocalDriver(tmp_path)
    r = one(d, None, "копия", src, src)
    tabs = r["dump"]["contours"][1]["tables"]
    assert len(tabs[0]["rights"]) == 6 and len(tabs[1]["rights"]) == 7


def test_refusals_carry_no_content(tmp_path):
    # Отказ в воротах не зависит от содержимого объектов: память агента после
    # одних и тех же неверных вызовов одинакова, что бы ни лежало у соседа.
    src = HEAD + '''(func (export "main") (local $c i32)
      (local.set $c (i32.const 6))
      (block $e (loop $l
        (drop (call $call (local.get $c) (i32.const 0) (i32.const 1024) (i32.const 64)))
        (local.set $c (i32.add (local.get $c) (i32.const 1)))
        (br_if $l (i32.lt_s (local.get $c) (i32.const 40)))))))'''

    # Второй агент переписывает свои слова человека в свой снимок.
    copier = HEAD + '''(func (export "main")
      (drop (call $call (i32.const 4) (i32.const 0) (i32.const 256) (i32.const 64)))
      (drop (call $call (i32.const 1) (i32.const 1) (i32.const 256) (i32.const 64)))))'''

    def memory_after(root: Path, defect, words: str) -> bytes:
        d = LocalDriver(root, frozenset([defect]) if defect else frozenset())
        d.call("new")
        d.call("load", 1, src)
        d.call("load", 2, copier)
        d.call("say", 2, words)
        d.call("run", 2)
        d.call("run", 1)
        return d.call("dump")[1]["contours"][1]["memories"][0][1024:]

    assert memory_after(tmp_path / "а", None, "одно") == memory_after(tmp_path / "б", None, "совсем другое")
    assert memory_after(tmp_path / "в", "Д7р", "одно") != memory_after(tmp_path / "г", "Д7р", "совсем другое")


def test_signatures_of_resident_are_in_inventory(tmp_path):
    r = run_named(tmp_path, "жилец")
    recs, _ = read_journal(Path(r["workdir"]) / "journal5.jsonl")
    sigs = {tuple(x["signature"]) for x in recs if x["kind"] == "действие"}
    assert sigs and sigs <= INVENTORY


# ---- порчи: каждая поймана поведением хотя бы одной программы

@pytest.mark.parametrize("defect", DEFECTS)
def test_each_defect_caught(tmp_path, defect):
    caught = {}
    for name in ("жилец", "разведка", "шифровальщик", "по имени"):
        r = run_named(tmp_path / name, name, defect)
        caught[name] = BEHAVIOUR(r["laws"])
        assert r["laws"].get("класс", (HOLDS,))[0] == HOLDS, r["laws"]["класс"]   # тот же класс, что в модели
        assert r["laws"]["И27"][0] == HOLDS
    assert any(caught.values()), caught


def test_canary_misses_one_bit_channel_labels_catch(tmp_path):
    # Д6р: общая очередь передаёт признак «занято». Байтов канарейки в нём
    # нет; метки сведений видят перенос.
    r = run_named(tmp_path, "разведка", "Д6р")
    assert r["laws"]["И22р/м"][0] == BROKEN
    assert r["laws"]["И22р/к"][0] == HOLDS


def test_encoded_leak_seen_by_labels_not_canary(tmp_path):
    # Д2р и шифровальщик: наружу уходит канарейка под «исключающим или».
    r = run_named(tmp_path, "шифровальщик", "Д2р")
    assert r["laws"]["И21р/м"][0] == BROKEN
    assert r["laws"]["И21р/к"][0] == HOLDS


def test_d4_module_with_by_name_loads_only_in_defect(tmp_path):
    good = run_named(tmp_path / "а", "по имени")
    bad = run_named(tmp_path / "б", "по имени", "Д4р")
    assert good["loads"][1][0] == "отклонён"
    assert bad["loads"][1][0] == "загружен" and bad["laws"]["И23р"][0] == BROKEN


# ---- наблюдатель и ведущий

def test_observer_detects_journal_tamper(tmp_path):
    r = run_named(tmp_path, "жилец")
    path = Path(r["workdir"]) / "journal5.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[3])
    rec["subject"] = "а2" if rec.get("subject") == "а1" else "а1"
    lines[3] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert check(r)["И5"][0] == BROKEN


def test_driver_bad_commands_refused(tmp_path):
    d = LocalDriver(tmp_path)
    assert d.call("run", 1)[0] == "отказ"                  # нет песочницы
    d.call("new")
    for msg in (("bogus",), ("load", 3, "x"), ("load", True, "x"), ("load", 1, 5),
                ("load", 1, b"x" * (1 << 21)), ("say", 1, "x" * 5000), ("say", 2, 7), ()):
        assert d.call(*msg)[0] == "отказ", msg
    assert d.call("load", 1, PROGS["жилец"])[1][0] == "загружен"
    assert d.call("load", 1, PROGS["жилец"])[1][0] == "отклонён"          # второй раз нельзя
    assert d.failures == []


def test_audit_instrument(tmp_path):
    a = _Audit(tmp_path)
    a.on = True
    a.hook("open", (str(tmp_path / "журнал"), "ab", 0))
    assert a.events == []
    a.hook("open", ("/tmp/вне_папки.txt", "w", 0))
    a.hook("socket.connect", (None, ("127.0.0.1", 80)))
    a.hook("subprocess.Popen", ("ls", [], None, None))
    assert len(a.events) == 3
    a.on = False


def test_real_process_separation(tmp_path):
    d = Driver(tmp_path)
    try:
        r = one(d, None, "разведка", PROGS["разведка"], PROGS["разведка"])
        assert broken(r["laws"]) == [], r["laws"]
        assert r["audit"] == []
        r2 = one(d, None, "жилец", PROGS["жилец"], PROGS["жилец"],
                 audit_from=r["audit_total"])
        assert r2["laws"]["И25р"][0] == HOLDS
    finally:
        d.close()


def test_real_process_defect_caught(tmp_path):
    d = Driver(tmp_path, frozenset(["Д5р"]))
    try:
        r = one(d, "Д5р", "разведка", PROGS["разведка"], PROGS["разведка"])
        assert r["laws"]["И20р"][0] == BROKEN and r["laws"]["И24р"][0] == BROKEN
        assert r["audit"] == []                            # «снаружи» — файл в папке, в мир не ушло
    finally:
        d.close()
