"""Host-owned append-only copy of a desktop run's journal."""

import os
from pathlib import Path

from env.exchange import open_directory, read_file
from verification.observer5 import journal_record

NAME = "journal-checkpoint.jsonl"
MAX_RECORD = 128 << 10
MAX_JOURNAL = 16 << 20


class Checkpoint:
    def __init__(self, path: Path):
        self.path = path
        self.seq, self.prev, self.size = 0, "0" * 64, 0
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))

    def append(self, line: bytes) -> None:
        if (not isinstance(line, bytes) or not 0 < len(line) <= MAX_RECORD
                or self.size + len(line) > MAX_JOURNAL or not line.endswith(b"\n") or line.count(b"\n") != 1):
            raise ValueError("недопустимый размер или формат записи журнала")
        row = journal_record(line, self.seq, self.prev)
        if not isinstance(row.get("kind"), str):
            raise ValueError("не указан вид записи журнала")
        with self.path.open("ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        self.seq += 1
        self.prev, self.size = row["hash"], self.size + len(line)


def matches(checkpoint: Path, copy: Path) -> bool:
    try:
        fd = open_directory(copy.parent)
        try:
            return read_file(fd, copy.name, MAX_JOURNAL) == checkpoint.read_bytes()
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return False
