"""Файлы сеанса. Пользовательская папка и защищённые копии разделены.

Импорт принимает плоский набор обычных файлов. Ссылки, папки и специальные
файлы отклоняются. Блокировки, изменения имени и метаданных при чтении
разрешаются повторным импортом. Журнал содержит идентификаторы и хеши,
а содержимое хранится в каталоге сеанса исполнителя.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

MAX_FILES = 16
MAX_FILE_BYTES = 64 * 1024
MAX_FILES_BYTES = 256 * 1024
MAX_STORAGE_BYTES = 4 * 1024 * 1024
MAX_NAME_BYTES = 120


def valid_name(name: object) -> bool:
    if not isinstance(name, str) or not name or name.startswith(".") or "/" in name or "\\" in name:
        return False
    return name.isprintable() and len(name.encode("utf-8")) <= MAX_NAME_BYTES


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def pack_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def input_manifest(inputs: dict[str, bytes]) -> list[dict]:
    return [{"id": i, "name": name, "size": len(data), "sha256": digest(data)}
            for i, (name, data) in enumerate(sorted(inputs.items()), 1)]


def open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def read_file(directory: int, name: str, limit: int, *, single_link: bool = True) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or (single_link and before.st_nlink != 1) or before.st_size > limit:
            raise ValueError(f"файл «{name}» должен быть обычным и не больше {limit} байт")
        data = stream.read(limit + 1)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (len(data) != before.st_size or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns or not os.path.samestat(current, before)):
            raise ValueError(f"файл «{name}» изменился при чтении")
        return data


def snapshot_inbox(path: Path) -> dict[str, bytes]:
    fd = open_directory(path)
    try:
        result = {}
        left = MAX_FILES_BYTES
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(result) == MAX_FILES:
                    raise ValueError(f"во входе допускается до {MAX_FILES} файлов")
                if not valid_name(entry.name):
                    raise ValueError(f"недопустимое имя во входе: {entry.name!r}")
                data = read_file(fd, entry.name, min(left, MAX_FILE_BYTES))
                result[entry.name] = data
                left -= len(data)
        return result
    finally:
        os.close(fd)


def put_new(directory: int, name: str, data: bytes) -> None:
    """Опубликовать целый новый файл и синхронизировать каталог."""
    tmp = ".pending-" + secrets.token_hex(16)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
    finally:
        os.unlink(tmp, dir_fd=directory)
        os.fsync(directory)


def put_private(path: Path, data: bytes) -> None:
    """Файлы исполнителя записывает только его процесс. Абсолютные пути видит аудит."""
    tmp = path.with_name(".pending-" + secrets.token_hex(16))
    try:
        with tmp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = open_directory(path.parent)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


class ExchangeStore:
    """Защищённые файлы по SHA-256. Вход задаётся один раз до загрузки агента."""

    def __init__(self, path: Path, *, create: bool = True):
        self.path = path
        self.blobs = path / "blobs"
        self.index = path / "inputs.json"
        if create:
            self.blobs.mkdir(mode=0o700, parents=True, exist_ok=True)
            for staged in self.blobs.glob(".pending-*"):
                staged.unlink()
        self.used = sum(p.stat().st_size for p in self.blobs.iterdir() if is_digest(p.name))
        self.inputs = json.loads(self.index.read_bytes()) if self.index.exists() else []

    def import_inputs(self, inputs: dict[str, bytes]) -> None:
        if (self.index.exists() or not isinstance(inputs, dict) or len(inputs) > MAX_FILES
                or any(not valid_name(k) or not isinstance(v, bytes) or len(v) > MAX_FILE_BYTES
                       for k, v in inputs.items()) or sum(map(len, inputs.values())) > MAX_FILES_BYTES):
            raise ValueError("недопустимый снимок входных файлов")
        self.inputs = input_manifest(inputs)
        for entry in self.inputs:
            self.put_blob(entry["sha256"], inputs[entry["name"]])
        put_private(self.index, pack_json(self.inputs))

    def can_store(self, sha: str, size: int) -> bool:
        return (self.blobs / sha).is_file() or self.used + size <= MAX_STORAGE_BYTES

    def put_blob(self, sha: str, data: bytes) -> None:
        if not is_digest(sha) or digest(data) != sha or not self.can_store(sha, len(data)):
            raise ValueError("неверный отпечаток или исчерпана квота файлов")
        if (self.blobs / sha).is_file():
            if self.read_blob(sha) != data:
                raise ValueError("данные файла повреждены")
            return
        put_private(self.blobs / sha, data)
        self.used += len(data)

    def read_blob(self, sha: str) -> bytes:
        if not is_digest(sha):
            raise ValueError("неверный отпечаток файла")
        with (self.blobs / sha).open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("файл превысил предел")
        if digest(data) != sha:
            raise ValueError("данные файла повреждены")
        return data
