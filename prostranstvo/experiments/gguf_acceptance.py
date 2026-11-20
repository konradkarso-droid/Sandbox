"""Положительная приёмка Bubblewrap и живой GGUF на отдельном Linux-хосте.

Запускать обычным пользователем из prostranstvo:
    python3 -m experiments.gguf_acceptance /путь/к/доверенной.gguf

Проба видит только заявленные границы текущей конфигурации Linux.
Она создаёт временные файлы пользователя и читает указанные веса.
"""

from __future__ import annotations

import argparse
from importlib.metadata import version
import os
from pathlib import Path
import subprocess
import tempfile

from app import model_sandbox
from sandbox.model9 import ModelLink

PROBE = """
import os, sys
from pathlib import Path

home, journal, parent, original_net = sys.argv[1:]
assert 'PROSTRANSTVO_ACCEPTANCE_SECRET' not in os.environ
assert not Path(home).exists()
assert not Path(journal).exists()
assert not Path('/proc', parent).exists()
assert os.readlink('/proc/self/ns/net') != original_net
with Path('/model/weights.gguf').open('rb') as weights:
    assert weights.read(4) == b'GGUF'
try:
    fd = os.open('/model/weights.gguf', os.O_WRONLY)
except OSError:
    pass
else:
    os.close(fd)
    raise AssertionError('веса доступны на запись')
Path('/tmp/acceptance').write_bytes(b'private scratch')
"""


def check(model: Path, timeout: float) -> int:
    if os.geteuid() == 0:
        raise RuntimeError("приёмка рассчитана на обычного пользователя Linux")
    if reason := model_sandbox.check():
        raise RuntimeError(reason)
    argv = model_sandbox.command(model)
    with tempfile.TemporaryDirectory(prefix="prostranstvo-", dir=Path.home()) as home:
        with tempfile.TemporaryDirectory(prefix="prostranstvo-") as work:
            home_probe, journal_probe = Path(home) / "private", Path(work) / "journal"
            home_probe.write_bytes(b"private home fixture")
            journal_probe.write_bytes(b"journal fixture")
            extra = [str(home_probe), str(journal_probe), str(os.getpid()), os.readlink("/proc/self/ns/net")]
            subprocess.run([*argv[:-2], PROBE, *extra], check=True, timeout=timeout,
                           env={**os.environ, "PROSTRANSTVO_ACCEPTANCE_SECRET": "fixture"})
    print("BWRAP_BOUNDARY_OK", flush=True)

    link = ModelLink(argv, timeout, isolated=True, sandboxed=True)
    try:
        answer = link.ask(b"Tell a short story about a dog.", 96)
        if not answer:
            raise RuntimeError("живой загрузчик GGUF не вернул ответ")
    finally:
        link.close()
    if link.proc.poll() is None:
        raise RuntimeError("процесс модели не завершился")
    return len(answer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    print("llama-cpp-python", version("llama-cpp-python"), flush=True)
    size = check(args.model, args.timeout)
    print("BWRAP_LIVE_GGUF_OK", size, "bytes", flush=True)


if __name__ == "__main__":
    main()
