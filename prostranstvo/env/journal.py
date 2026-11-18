"""Журнал исполнителя: файл, который только дописывается.

Формат — docs/shag2_specifikatsiya.md, §2. Одна запись — одна строка JSON.
Каждая запись несёт номер (seq), отпечаток предыдущей записи (prev) и
свой отпечаток (hash) = sha256 от JSON записи без поля hash, с
упорядоченными ключами. Первая запись ссылается на prev = «0» × 64.

Строка пишется целиком вместе с переводом строки и сразу сбрасывается на
диск. Поэтому оборванная запись (сбой посреди записи) — это всегда
последний кусок файла без перевода строки; при открытии он отрезается:
такой записи не было.

Разрыв цепочки внутри файла — не оборванная запись, а порча учёта. Тогда
JournalBroken: исполнитель обязан не запускаться, а не чинить молча.

Чего журнал не умеет: подписи внешним ключом нет — цепочка ловит правку
середины файла и обрезку не с конца только вместе с наблюдателем, который
помнит прежнюю версию (закон И7). Обрезку хвоста целыми строками цепочка
сама не видит.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

GENESIS = "0" * 64


class JournalBroken(Exception):
    """Цепочка отпечатков разорвана внутри журнала."""


def record_hash(body: dict) -> str:
    text = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_complete_lines(path: Path) -> tuple[list[bytes], int]:
    """Целые строки и длина файла до конца последней целой строки."""
    data = path.read_bytes() if path.exists() else b""
    end = data.rfind(b"\n") + 1          # 0, если перевода строки нет вовсе
    lines = data[:end].split(b"\n")[:-1] if end else []
    return lines, end


class Journal:
    def __init__(self, path: Path, records: list[dict]):
        self.path = path
        self.records = records

    @classmethod
    def create(cls, path: Path) -> "Journal":
        path.write_bytes(b"")
        return cls(path, [])

    @classmethod
    def open(cls, path: Path) -> tuple["Journal", bool]:
        """Прочитать журнал, отрезать оборванный хвост. Возвращает журнал и
        признак «хвост был отрезан»."""
        lines, end = _read_complete_lines(path)
        size = path.stat().st_size
        repaired = end != size
        if repaired:
            with path.open("r+b") as fh:
                fh.truncate(end)
                fh.flush()
                os.fsync(fh.fileno())
        records: list[dict] = []
        prev = GENESIS
        for i, raw in enumerate(lines):
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise JournalBroken(f"строка {i + 1} не читается: {e}") from e
            body = {k: v for k, v in rec.items() if k != "hash"}
            if rec.get("seq") != i or rec.get("prev") != prev or rec.get("hash") != record_hash(body):
                raise JournalBroken(f"цепочка разорвана на записи {i + 1}")
            records.append(rec)
            prev = rec["hash"]
        return cls(path, records), repaired

    def _line(self, fields: dict) -> tuple[dict, bytes]:
        prev = self.records[-1]["hash"] if self.records else GENESIS
        body = {**fields, "seq": len(self.records), "prev": prev}
        rec = {**body, "hash": record_hash(body)}
        return rec, (json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    def append(self, fields: dict) -> dict:
        rec, line = self._line(fields)
        with self.path.open("ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self.records.append(rec)
        return rec

    def append_torn(self, fields: dict) -> None:
        """Только для инъекции сбоя: на диск попадает половина строки."""
        _, line = self._line(fields)
        with self.path.open("ab") as fh:
            fh.write(line[: len(line) // 2])
            fh.flush()
            os.fsync(fh.fileno())
