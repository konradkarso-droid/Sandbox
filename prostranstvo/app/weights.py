"""Stream and authenticate GGUF files before handing a private copy to the loader."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20
CATALOG_LIMIT = 1 << 20
COPY_TIMEOUT = 2 * 3600


MISSING_REFERENCE = ("нужны доверенные SHA-256 и размер в байтах",
                     "a trusted SHA-256 and size in bytes are required")


class IntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class Reference:
    sha256: str
    size: int

    @classmethod
    def parse(cls, metadata: object) -> Reference:
        if not isinstance(metadata, dict):
            raise IntegrityError(*MISSING_REFERENCE)
        digest, size = metadata.get("sha256"), metadata.get("size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest.strip()):
            raise IntegrityError("неверный SHA-256 модели", "invalid model SHA-256")
        if type(size) is not int or not 4 <= size < 2**53:
            raise IntegrityError("неверный размер модели в байтах", "invalid model size in bytes")
        return cls(digest.strip().lower(), size)


def copy_verified(read, target: Path, reference: Reference, *, progress=None, cancelled=None) -> None:
    """The target must be inside a temporary directory with owner-only access."""
    digest = hashlib.sha256()
    total, header = 0, b""
    deadline = time.monotonic() + COPY_TIMEOUT
    with target.open("xb") as out:
        while True:
            if cancelled is not None and cancelled():
                raise IntegrityError("копирование остановлено", "copy cancelled")
            chunk = read(min(CHUNK, reference.size - total + 1))
            if time.monotonic() >= deadline:
                raise IntegrityError("срок копирования GGUF истёк", "GGUF copy timed out")
            if not chunk:
                break
            total += len(chunk)
            if total > reference.size:
                raise IntegrityError("модель больше ожидаемого размера", "model exceeds expected size")
            if len(header) < 4:
                header = (header + chunk[:4])[:4]
                if len(header) == 4 and header != b"GGUF":
                    raise IntegrityError("неверная сигнатура GGUF", "invalid GGUF signature")
            digest.update(chunk)
            out.write(chunk)
            if progress is not None:
                progress(total)
        if total != reference.size:
            raise IntegrityError("размер GGUF не совпадает с эталоном", "GGUF size does not match")
        if digest.hexdigest() != reference.sha256:
            raise IntegrityError("SHA-256 GGUF не совпадает с эталоном", "GGUF SHA-256 does not match")
        out.flush()
        os.fsync(out.fileno())
    target.chmod(0o400)


def stage_local(source: Path, staging: Path, reference: Reference, *, cancelled=None) -> Path:
    target = staging / "weights.gguf"
    with os.fdopen(os.open(source, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != reference.size:
            raise IntegrityError("нужен обычный файл GGUF ожидаемого размера",
                                 "a regular GGUF file of the expected size is required")
        copy_verified(stream.read, target, reference, cancelled=cancelled)
    return target


def load_catalog(path: Path, public_key: Path) -> dict[str, Reference]:
    """Load versioned references after verifying the exact bytes with a pinned key."""
    with path.open("rb") as source:
        payload = source.read(CATALOG_LIMIT + 1)
    with Path(str(path) + ".sig").open("rb") as source:
        signature = source.read(65)
    if not 0 < len(payload) <= CATALOG_LIMIT or len(signature) != 64:
        raise ValueError("invalid GGUF catalog or Ed25519 signature size")
    with tempfile.TemporaryDirectory(prefix="gguf-catalog-") as staging:
        catalog = Path(staging) / "catalog.json"
        sig = Path(staging) / "catalog.sig"
        catalog.write_bytes(payload)
        sig.write_bytes(signature)
        result = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public_key),
                                 "-rawin", "-in", str(catalog), "-sigfile", str(sig)],
                                capture_output=True, timeout=10)
    if result.returncode:
        raise ValueError("GGUF catalog signature verification failed")
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("models"), list):
        raise ValueError("invalid GGUF catalog schema")
    entries = {}
    for entry in data["models"]:
        if not isinstance(entry, dict):
            raise ValueError("invalid GGUF catalog entry")
        name, version = entry.get("name"), entry.get("version")
        if (not isinstance(name, str) or name != Path(name).name or "\\" in name
                or Path(name).suffix.lower() != ".gguf" or name in entries
                or not isinstance(version, str) or not version.strip()):
            raise ValueError("invalid GGUF catalog model name or version")
        entries[name] = Reference.parse(entry)
    return entries
