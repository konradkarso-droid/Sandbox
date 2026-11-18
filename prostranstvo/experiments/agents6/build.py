"""Сборка агентов шага 6 из agent.c в WebAssembly (clang, цель wasm32, без
стандартной библиотеки). Поведение — макросами; путь к E — заголовком
epath.h, который пишется на каждую сборку. Неописанная функция — ошибка
сборки, а не новый импорт: агенту доступны только ворота среды.

Собранное кешируется в папке опыта по отпечатку исходника и настроек;
в архив проекта собранные модули не кладутся."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
CACHE = Path(tempfile.gettempdir()) / "prostranstvo_agents6"


def build(defines: dict[str, str | int | None], e_moves: list[tuple[int, int]]) -> bytes:
    src = (HERE / "agent.c").read_text(encoding="utf-8")
    common = (HERE / "tasks6.h").read_text(encoding="utf-8")
    header = "static const int e_moves[][2] = {" + ", ".join(f"{{{a}, {b}}}" for a, b in e_moves or [(0, 0)]) \
             + "};\nstatic const int e_len = " + str(len(e_moves)) + ";\n"
    flags = [f"-D{k}" if v is None else f"-D{k}={v}" for k, v in sorted(defines.items())]
    key = hashlib.sha256((src + common + header + " ".join(flags)).encode()).hexdigest()[:16]
    out = CACHE / f"{key}.wasm"
    if out.exists():
        return out.read_bytes()
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "agent.c").write_text(src, encoding="utf-8")
        (t / "tasks6.h").write_text(common, encoding="utf-8")
        (t / "epath.h").write_text(header, encoding="utf-8")
        cmd = ["clang", "--target=wasm32", "-nostdlib", "-O2", "-fno-builtin", *flags,
               "-Wl,--no-entry", "-Wl,-z,stack-size=16384", "-Wl,--initial-memory=131072",
               "-Wl,--max-memory=131072", "-o", str(t / "a.wasm"), str(t / "agent.c")]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError("сборка агента не удалась:\n" + res.stderr[-2000:])
        data = (t / "a.wasm").read_bytes()
    out.write_bytes(data)
    return data
