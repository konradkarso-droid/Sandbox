"""Искусственный получатель — внешний по отношению к агенту объект O.

Отдельный файл, а не часть журнала: если доставки считать по журналу
исполнителя, ошибка «доставлено раньше, чем записано списание» станет
невидимой — журнал соврёт заодно с исполнителем (спецификация шага 2, §2).

Одна доставка — одна строка {"fix": номер фиксации, "msgs": [сообщения]}.
Пакет пишется одной строкой, поэтому сбой не может оставить половину пакета.

Защита от повтора: пара (номер фиксации, сообщение) принимается не больше
одного раза. Это свойство получателя, а не исполнителя (ключ
идемпотентности). Выключается только порчей Р3.

Оборванная последняя строка (сбой посреди записи) при открытии отрезается:
такой доставки не было.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class Receiver:
    def __init__(self, path: Path, dedup: bool = True):
        self.path = path
        self.dedup = dedup
        self.lock = threading.Lock()
        if not path.exists():
            path.write_bytes(b"")
        self._repair()

    def _repair(self) -> None:
        data = self.path.read_bytes()
        end = data.rfind(b"\n") + 1
        if end != len(data):
            with self.path.open("r+b") as fh:
                fh.truncate(end)
                fh.flush()
                os.fsync(fh.fileno())

    def entries(self) -> list[dict]:
        data = self.path.read_bytes()
        end = data.rfind(b"\n") + 1
        return [json.loads(x) for x in data[:end].decode("utf-8").splitlines() if x]

    def has(self, fix: str) -> bool:
        return any(e["fix"] == fix for e in self.entries())

    def accept(self, fix: str, msgs: list[str]) -> list[str]:
        """Принять доставку. Возвращает реально принятые сообщения."""
        with self.lock:
            if self.dedup:
                seen = {(e["fix"], m) for e in self.entries() for m in e["msgs"]}
                msgs = [m for m in msgs if (fix, m) not in seen]
                if not msgs:
                    return []
            self._write((json.dumps({"fix": fix, "msgs": msgs}, ensure_ascii=False) + "\n").encode("utf-8"))
            return msgs

    def accept_torn(self, fix: str, msgs: list[str]) -> None:
        """Только для инъекции сбоя: на диск попадает половина строки."""
        line = (json.dumps({"fix": fix, "msgs": msgs}, ensure_ascii=False) + "\n").encode("utf-8")
        with self.lock:
            self._write(line[: len(line) // 2])

    def wipe(self) -> None:
        """Только для порчи Р13: получатель пересоздан."""
        with self.lock:
            self.path.write_bytes(b"")

    def _write(self, data: bytes) -> None:
        with self.path.open("ab") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
