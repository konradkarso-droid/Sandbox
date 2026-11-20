"""Папки обмена и получатель outbox, управляемые пользователем.

Подтверждение касается одной оплаченной заявки на неизменяемую версию
файла. Получатель сам проверяет состояние сеанса и данные на диске. После
сбоя повтор того же подтверждения восстанавливает квитанцию без перезаписи
файла. Пользовательский каталог не передаётся в исполнитель.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets

from env.exchange import (MAX_FILE_BYTES, MAX_FILES, MAX_FILES_BYTES, ExchangeStore, digest, open_directory,
                          input_manifest, is_digest, pack_json, put_new, read_file, snapshot_inbox, valid_name)
from verification.exchange_observer import check_files
from verification.gateway_observer import verify as verify_gateway
from verification.observer5 import read_journal


def valid_session(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


class ExchangeJob:
    def __init__(self, exchanges: Path, runs: Path, session: str):
        if not valid_session(session):
            raise ValueError("неверный номер сеанса обмена")
        self.id = session
        self.root = exchanges / session
        self.run = runs / session
        self.workdir = self.run / "ispolnitel" / "run00001"

    @classmethod
    def create(cls, exchanges: Path, runs: Path) -> ExchangeJob:
        job = cls(exchanges, runs, secrets.token_hex(16))
        job.root.mkdir(mode=0o700, parents=True)
        for area in ("inbox", "outbox", "receipts"):
            (job.root / area).mkdir(mode=0o700)
        return job

    def info(self) -> dict:
        if not self.root.is_dir():
            raise ValueError("сеанс обмена не найден")
        visible = {"id": self.id, "inbox": str(self.root / "inbox"), "outbox": str(self.root / "outbox"),
                   "receipts": str(self.root / "receipts"), "requests": [], "started": self.run.exists(), "ready": False}
        if not visible["started"]:
            try:
                visible["inputs"] = input_manifest(snapshot_inbox(self.root / "inbox"))
            except (ValueError, OSError) as exc:
                visible["issue"] = str(exc)
        if (self.run / "itog.json").exists():
            try:
                proposals = self._paid_requests()
            except (ValueError, OSError) as exc:
                return {**visible, "issue": str(exc)}
            visible["ready"] = True
            for request in proposals:
                receipt = self.root / "receipts" / f"{request['seq']}.json"
                visible["requests"].append({"id": request["seq"], "name": request["name"],
                                            "size": request["bytes"], "sha256": request["sha256"],
                                            "published": receipt.is_file()})
        return visible

    def add_input(self, name: object, data: bytes) -> None:
        if not valid_name(name) or len(data) > MAX_FILE_BYTES or self.run.exists():
            raise ValueError("недопустимый файл или сеанс уже запущен")
        inbox = self.root / "inbox"
        files = snapshot_inbox(inbox)
        if len(files) >= MAX_FILES or sum(map(len, files.values())) + len(data) > MAX_FILES_BYTES:
            raise ValueError("исчерпана квота входных файлов")
        fd = open_directory(inbox)
        try:
            put_new(fd, name, data)
        finally:
            os.close(fd)

    def _paid_requests(self) -> list[dict]:
        report = self.run / "itog.json"
        if not report.is_file():
            raise ValueError("итог прогона ещё не сохранён")
        result = json.loads(report.read_bytes())
        verdict = result.get("result") or {}
        if (result.get("state") != "готово" or result.get("settings", {}).get("exchange") != self.id
                or verdict.get("broken") != []):
            raise ValueError("выдача доступна после прогона без нарушенных законов")
        rows, broken = read_journal(self.workdir / "journal5.jsonl")
        if broken or not rows or rows[-1]["hash"] != verdict.get("journal_tip"):
            raise ValueError(broken or "журнал изменился после проверки прогона")
        config = next((row for row in rows if row.get("kind") == "учёт"), None)
        if (not config or config.get("paid_attempts") != 1
                or (config.get("model") == 1 and not is_digest(config.get("cloud_manifest")))):
            raise ValueError("неверный профиль прогона обмена")
        check_files(self.workdir, config)
        if config.get("model") == 1:
            consent = verdict.get("gateway") or {}
            if (consent.get("manifest") != config["cloud_manifest"]
                    or consent.get("provider") != config.get("cloud_provider")
                    or consent.get("model") != config.get("cloud_model")):
                raise ValueError("итог прогона не совпал с разрешением на облако")
            verify_gateway(self.workdir, config["cloud_provider"])
        return [row for row in rows if row.get("kind") == "инструмент" and row.get("tool") == "files.publish"]

    def approve(self, request_id: int) -> dict:
        if type(request_id) is not int or request_id < 0:
            raise ValueError("нужен номер заявки")
        request = next((r for r in self._paid_requests() if r["seq"] == request_id), None)
        if request is None:
            raise ValueError("оплаченная заявка не найдена")
        data = ExchangeStore(self.workdir / "exchange-state", create=False).read_blob(request["sha256"])
        name = f"{request_id}-{request['name']}"
        receipt = {"request": request_id, "name": name, "sha256": request["sha256"],
                   "bytes": request["bytes"], "charge": request["charge"], "journal": request["hash"]}
        self._deliver("outbox", name, data, MAX_FILE_BYTES)
        self._deliver("receipts", f"{request_id}.json", pack_json(receipt), 4096)
        return receipt

    def _deliver(self, area: str, name: str, data: bytes, limit: int) -> None:
        fd = open_directory(self.root / area)
        try:
            try:
                put_new(fd, name, data)
            except FileExistsError:
                existing = read_file(fd, name, limit, single_link=False)
                if digest(existing) != digest(data):
                    raise ValueError(f"получатель уже содержит другой файл «{name}»")
        finally:
            os.close(fd)

    def download(self, request_id: int) -> tuple[str, bytes]:
        fd = open_directory(self.root / "receipts")
        try:
            receipt = json.loads(read_file(fd, f"{request_id}.json", 4096, single_link=False))
        finally:
            os.close(fd)
        request = next((r for r in self._paid_requests() if r["seq"] == request_id), None)
        if request is None or receipt.get("journal") != request["hash"]:
            raise ValueError("квитанция не соответствует заявке")
        name = f"{request_id}-{request['name']}"
        fd = open_directory(self.root / "outbox")
        try:
            data = read_file(fd, name, MAX_FILE_BYTES, single_link=False)
        finally:
            os.close(fd)
        if digest(data) != request["sha256"]:
            raise ValueError("выданный файл изменился")
        return request["name"], data
