"""Независимый наблюдатель шага 2: проверяет законы И1–И10 по файлам.

Наблюдатель **не импортирует** код исполнителя (env/). Формат журнала и
получателя он читает сам, по описанию в docs/shag2_specifikatsiya.md, §2,
и правило цены вычисляет сам, по §5. Если бы он пользовался кодом
исполнителя, то повторил бы его ошибку и согласился бы с ней. Отсюда
сознательное второе прочтение того же формата — это не дублирование, а
независимость.

От исполнителя берётся только его отчёт о долях (report()). С файлами он
сверяется: доли — единственное, чего в файлах нет напрямую.

Законы И1–И10 — таблица в §6 спецификации шага 2. Законы по переходу (И2,
И8) проверяет compare_internal() — сравнение до и после внутреннего
действия; остальные — check().

Чего наблюдатель не умеет:
- в параллельных опытах он смотрит в паузах между волнами, поэтому
  промежуточные состояния внутри волны не видит;
- оборванный хвост файла (исполнитель упал посреди записи) он считает
  ненаписанным, как и восстановление; если хвост окажется не оборванной
  записью, а чем-то иным, наблюдатель этого не отличит;
- обрезку журнала целыми строками с конца цепочка не видит, её ловит
  только сравнение с прежней версией журнала (И7).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

GENESIS = "0" * 64

LAW_TITLES = {
    "И1": "сохранение: доли + расход по журналу = бюджет; доли ≥ 0 (кроме M0)",
    "И1-M0": "в M0 доля ушла в минус — так задумано: M0 не ограничивает",
    "И2": "внутреннее действие не меняет получателя",
    "И3": "у доставки есть списание с тем же номером и ценой по правилу варианта",
    "И4": "отказанная фиксация не попала к получателю",
    "И5": "журнал цел и сходится с отчётом исполнителя",
    "И6": "оплачено не меньше наименьшей цены доставленного по правилу варианта",
    "И7": "журнал только растёт",
    "И8": "внутреннее действие не увеличивает общий остаток",
    "И9": "пакет у получателя либо целиком, либо никак",
    "И10": "одна фиксация — не больше одной доставки каждого сообщения",
}


class Violation(NamedTuple):
    law: str
    detail: str


def _complete_lines(path: Path) -> list[str]:
    data = path.read_bytes() if path.exists() else b""
    end = data.rfind(b"\n") + 1
    return data[:end].decode("utf-8").splitlines()


def _hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def min_cost(variant: str, price: int, delivered: int) -> int:
    """Наименьшая цена D доставленных сообщений по правилу варианта (§5)."""
    if variant == "M2":
        return price * delivered * (delivered + 1) // 2
    return price * delivered


@dataclass
class Snapshot:
    journal_lines: list[str]
    receiver_bytes: bytes
    total_shares: int | None


class Observer:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.journal_path = workdir / "journal.jsonl"
        self.receiver_path = workdir / "receiver.jsonl"
        self.seen_journal: list[str] = []      # прежняя версия журнала для И7

    def snapshot(self, report: dict | None) -> Snapshot:
        return Snapshot(
            _complete_lines(self.journal_path),
            self.receiver_path.read_bytes() if self.receiver_path.exists() else b"",
            None if report is None else sum(report["shares"].values()),
        )

    def compare_internal(self, before: Snapshot, after: Snapshot) -> list[Violation]:
        """И2 и И8: внутреннее действие не меняет получателя и не
        увеличивает общий остаток."""
        out = []
        if before.receiver_bytes != after.receiver_bytes:
            out.append(Violation("И2", "файл получателя изменился от внутреннего действия"))
        if (before.total_shares is not None and after.total_shares is not None
                and after.total_shares > before.total_shares):
            out.append(Violation("И8", f"остаток вырос с {before.total_shares} до {after.total_shares}"))
        return out

    def check(self, report: dict | None) -> list[Violation]:
        """Все законы по файлам. report=None — исполнитель мёртв (после
        сбоя до восстановления): законы, требующие его отчёта, пропускаются."""
        out: list[Violation] = []
        lines = _complete_lines(self.journal_path)

        # И7: прежняя версия — начало новой
        if lines[: len(self.seen_journal)] != self.seen_journal:
            out.append(Violation("И7", f"было записей {len(self.seen_journal)}, начало журнала изменилось или он укоротился"))
        self.seen_journal = list(lines)

        # И5: цепочка
        recs: list[dict] = []
        prev = GENESIS
        for i, line in enumerate(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                out.append(Violation("И5", f"запись {i + 1} не читается"))
                break
            body = {k: v for k, v in rec.items() if k != "hash"}
            if rec.get("seq") != i or rec.get("prev") != prev or rec.get("hash") != _hash(body):
                out.append(Violation("И5", f"цепочка разорвана на записи {i + 1}"))
                break
            recs.append(rec)
            prev = rec["hash"]
        if not recs or recs[0].get("kind") != "открыт":
            out.append(Violation("И5", "нет записи об открытии сеанса"))
            return out
        opened = recs[0]
        budget, price, variant = opened["budget"], opened["price"], opened["variant"]

        debits = {r["fix"]: r for r in recs if r["kind"] == "списание"}
        refused = {r["fix"] for r in recs if r["kind"] == "отказ"}
        paid_total = sum(r["amount"] for r in recs if r["kind"] in ("списание", "отказ"))
        for r in recs:
            if r["kind"] in ("завершено", "потеряно") and r["fix"] not in debits:
                out.append(Violation("И5", f"исход «{r['kind']}» у фиксации {r['fix']} без списания"))

        # цены списаний по правилу варианта, в порядке журнала
        n = 0
        for r in recs:
            if r["kind"] != "списание":
                continue
            k = len(r["msgs"])
            due = (sum(price * (1 + n + j) for j in range(k)) if variant == "M2" else price * k)
            n += k
            r["_due"] = due

        # получатель
        entries = [json.loads(x) for x in _complete_lines(self.receiver_path) if x]
        pairs = Counter((e["fix"], m) for e in entries for m in e["msgs"])
        delivered_by_fix: dict[str, set[str]] = {}
        for (fix, m), cnt in pairs.items():
            delivered_by_fix.setdefault(fix, set()).add(m)
            if cnt > 1:
                out.append(Violation("И10", f"фиксация {fix}: сообщение «{m}» доставлено {cnt} раза"))
        for fix, got in delivered_by_fix.items():
            if fix in refused:
                out.append(Violation("И4", f"отказанная фиксация {fix} у получателя"))
            d = debits.get(fix)
            if d is None:
                out.append(Violation("И3", f"доставка {fix} без списания"))
                continue
            if d["amount"] < d["_due"]:
                out.append(Violation("И3", f"фиксация {fix}: списано {d['amount']}, по правилу {variant} положено {d['_due']}"))
            if got != set(d["msgs"]):
                out.append(Violation("И9", f"фиксация {fix}: доставлено {sorted(got)} из {sorted(d['msgs'])}"))

        delivered = sum(pairs.values())
        paid_debits = sum(d["amount"] for d in debits.values())
        need = min_cost(variant, price, delivered)
        if paid_debits < need:
            out.append(Violation("И6", f"доставлено {delivered}, по правилу {variant} нужно оплатить ≥ {need}, оплачено {paid_debits}"))

        if report is not None:
            shares = report["shares"]
            total = sum(shares.values())
            if total + paid_total != budget:
                out.append(Violation("И1", f"доли {total} + расход по журналу {paid_total} ≠ бюджет {budget}"))
            negative = {w: v for w, v in shares.items() if v < 0}
            if negative:
                law = "И1-M0" if variant == "M0" else "И1"
                out.append(Violation(law, f"отрицательные доли {negative}"))
            if report["spent"] != paid_total:
                out.append(Violation("И5", f"исполнитель говорит «расход {report['spent']}», журнал — {paid_total}"))
        return out
