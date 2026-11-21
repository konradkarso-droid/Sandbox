"""Сверка оплаченных вопросов с квитанциями отдельного отправителя."""

import json
from pathlib import Path

from verification.observer5 import BROKEN, HOLDS, read_journal

LAW = "И36р"


def check(workdir: Path, provider: str, *, journal: Path | None = None) -> tuple[str, str]:
    try:
        verify(workdir, provider, journal=journal)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return BROKEN, str(exc)
    return HOLDS, ""


def verify(workdir: Path, provider: str, *, journal: Path | None = None) -> None:
    rows, broken = read_journal(journal or workdir / "journal5.jsonl")
    if broken:
        raise ValueError(broken)
    setup = next((r for r in rows if r["kind"] == "учёт"), {})
    if setup.get("cloud_provider") != provider or not setup.get("cloud_model"):
        raise ValueError("адресат шлюза отсутствует в учёте")
    requests = {r["hash"]: r for r in rows if r.get("signature", [None])[0] == "вопрос модели"}
    answered = set()
    for index, row in enumerate(rows):
        if row.get("signature", [None])[0] == "ответ модели" and row.get("code") is None:
            if index == 0 or rows[index - 1]["hash"] not in requests:
                raise ValueError("ответ шлюза без вопроса")
            answered.add(rows[index - 1]["hash"])
    path = workdir.parent.parent / "gateway-receipts.jsonl"
    sent, outcomes = set(), {}
    lines = path.read_bytes().splitlines() if path.exists() else []
    for line in lines:
        receipt = json.loads(line)
        status = receipt["status"]
        contour = receipt["contour"]
        if (receipt["session"] != workdir.parent.parent.name or receipt["provider"] != provider
                or receipt["model"] != setup["cloud_model"] or type(contour) is not int or contour not in (1, 2)):
            raise ValueError("квитанция не принадлежит провайдеру или контуру сеанса")
        if status == "refused":
            continue
        claimed = receipt["receipt"]
        row = requests.get(claimed)
        if not row or row.get("charge", 0) < 1 or row["subject"] not in (f"а{contour}", f"п{contour}"):
            raise ValueError("сетевая квитанция без списания своего контура")
        if status == "dispatch":
            if claimed in sent or row["seq"] != receipt["charge_seq"] or row["length"] != receipt["size"]:
                raise ValueError("повторная либо несогласованная отправка")
            sent.add(claimed)
        elif status in ("answer", "unknown", "provider_error") and claimed in sent and claimed not in outcomes:
            outcomes[claimed] = status
        else:
            raise ValueError("итог шлюза без одной предварительной отправки")
    if any(outcomes.get(key) != "answer" for key in answered):
        raise ValueError("успешный ответ исполнителя без сетевой квитанции")
