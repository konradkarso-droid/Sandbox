"""Проверка «К14» обвязки судьи (harness.check14): два поля журнала из §14
контракта, на которые наблюдатели не смотрят.

Проверяется сама проверка: на журнале первой реализации она держится, а
журнал, переписанный во второе прочтение контракта (субъект записей
помощника — породивший экземпляр; права в «начало» — таблица до стартовой
функции), она ловит. Без этого «К14 чисто» у второй реализации ничего бы
не значило: законы среды такой журнал проходят.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

_JUDGE = Path(os.environ.get("JUDGE", Path(__file__).resolve().parents[2] / "rust7" / "judge"))
if not (_JUDGE / "harness.py").exists():
    pytest.skip("нет обвязки чистой комнаты (папка rust7 рядом с репозиторием)", allow_module_level=True)

import experiments.diff7 as D  # noqa: E402
from experiments import programs5 as p5  # noqa: E402
from verification.observer5 import BROKEN, HOLDS, read_journal  # noqa: E402

H = D.H
HELPER_RULES = ("создание помощника", "копия прав помощнику")


def run_program(root: Path, name: str) -> dict:
    src = p5.named()[name]
    d = H.ExeDriver(D.PY_EXE, root, "5б")
    r = H.one(d, name, {1: src, 2: src}, H.BASE)
    d.close()
    assert not d.failures, d.failures
    return r


def write_chained(path: Path, records: list[dict]) -> None:
    """Записать журнал с пересчитанной цепочкой отпечатков (§4), чтобы он
    оставался целым и отличался от исходного только полями."""
    prev, lines = "0" * 64, []
    for n, r in enumerate(records):
        body = {k: v for k, v in r.items() if k != "hash"}
        body["seq"], body["prev"] = n, prev
        h = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        lines.append(json.dumps({**body, "hash": h}, ensure_ascii=False, sort_keys=True))
        prev = h
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert read_journal(path)[1] is None


def rewrite(src: Path, dst: Path, change) -> int:
    """Копия рабочей папки, где каждая запись журнала прошла через
    change(запись) → изменена ли она. Возвращает число изменённых записей."""
    shutil.copytree(src, dst)
    recs, broken = read_journal(src / "journal5.jsonl")
    assert broken is None
    changed = sum(bool(change(r)) for r in recs)
    write_chained(dst / "journal5.jsonl", recs)
    return changed


def test_helper_subject(tmp_path):
    r = run_program(tmp_path / "run", "помощники")
    wd = Path(r["workdir"])
    assert r["laws"]["К14"][0] == HOLDS

    def creator_as_subject(rec):
        if rec.get("kind") == "действие" and rec["signature"][0] in HELPER_RULES:
            rec["subject"] = "а" + rec["subject"][1:]
            return True
        return False

    n = rewrite(wd, tmp_path / "b", creator_as_subject)
    assert n >= 2
    status, detail = H.check14(tmp_path / "b", "5б", H.BASE)
    assert status == BROKEN and detail.startswith(f"{n} записей"), detail


def test_start_rights(tmp_path):
    r = run_program(tmp_path / "run", "старт")
    wd = Path(r["workdir"])
    assert r["laws"]["К14"][0] == HOLDS
    start = [x for x in read_journal(wd / "journal5.jsonl")[0] if x.get("kind") == "начало"]
    # Программа «старт» берёт права в стартовой функции, иначе случай пустой.
    assert all(len(x["rights"]) > len(H.initial_table(x["contour"], "5б", 0)) for x in start)

    def table_before_start(rec):
        if rec.get("kind") == "начало":
            rec["rights"] = H.initial_table(rec["contour"], "5б", 0)
            return True
        return False

    assert rewrite(wd, tmp_path / "b", table_before_start) == 2
    status, detail = H.check14(tmp_path / "b", "5б", H.BASE)
    assert status == BROKEN and "права в «начало»" in detail, detail


def test_rights_taken_before_recovery_do_not_count(tmp_path):
    """Права, взятые до «восстановлено», в «начало» после него не входят:
    окно записей «чтение ссылки» открывается заново."""
    r = run_program(tmp_path / "run", "старт")
    wd = Path(r["workdir"])
    recs = read_journal(wd / "journal5.jsonl")[0]
    k = next(n for n, x in enumerate(recs) if x.get("kind") == "начало")
    # «восстановлено» сразу перед первым «начало»: взятое до него больше не
    # в окне, и права из стартовой функции в «начало» становятся лишними.
    shutil.copytree(wd, tmp_path / "b")
    write_chained(tmp_path / "b" / "journal5.jsonl",
                  recs[:k] + [{"kind": "восстановлено", "reason": "перезапуск", "contours": {}}] + recs[k:])
    status, detail = H.check14(tmp_path / "b", "5б", H.BASE)
    assert status == BROKEN and "права в «начало» контура 1" in detail, detail
