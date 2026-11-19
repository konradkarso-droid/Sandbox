"""Сборка агента шага 9 (agent9.c) в WebAssembly: clang, цель wasm32, без
стандартной библиотеки, те же флаги, что у агентов шага 6
(experiments/agents6/build.py). Неописанная функция — ошибка сборки, а не
новый импорт: агенту доступны только ворота среды.

Собранное кешируется во временной папке по отпечатку исходников; в архив
проекта собранные модули не кладутся."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
SOURCES = (HERE / "agent9.c", HERE.parent / "agents6" / "tasks6.h")
CACHE = Path(tempfile.gettempdir()) / "prostranstvo_agents9"

# Чем собирать. clang — основной (им собраны все прогоны шагов 9–9б); нужен
# с целью wasm32 и компоновщиком wasm-ld. zig — запасной для песочницы «из
# коробки»: ставится одной командой pip (пакет ziglang) на любом Linux, там,
# где clang нет или он без wasm-ld. Байты модулей двух сборщиков разные,
# поведение одно: те же исходники, те же пределы памяти и стека.
COMPILERS = {
    "clang": ["clang", "--target=wasm32"],
    "zig": [sys.executable, "-m", "ziglang", "cc", "--target=wasm32-freestanding", "-g0", "-Wl,--strip-all"],
}


def build(compiler: str = "clang", lang: str = "ru") -> bytes:
    """Модуль agent9. lang — язык правил и итогов для модели: «ru» или «en»
    (ходы агент понимает на обоих; agent9.c, начало файла)."""
    if lang not in ("ru", "en"):
        raise ValueError("язык агента — ru или en")
    key = hashlib.sha256(compiler.encode() + lang.encode()
                         + b"".join(p.read_bytes() for p in SOURCES)).hexdigest()[:16]
    out = CACHE / f"{key}.wasm"
    if out.exists():
        return out.read_bytes()
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    cmd = [*COMPILERS[compiler], *(["-DLANG_EN"] if lang == "en" else []), "-nostdlib", "-O2", "-fno-builtin", "-Wl,--no-entry",
           "-Wl,-z,stack-size=16384", "-Wl,--initial-memory=131072", "-Wl,--max-memory=131072",
           "-o", str(tmp), str(HERE / "agent9.c")]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(f"сборка агента шага 9: нет сборщика {compiler} ({e})")
    if r.returncode != 0:
        raise RuntimeError("сборка агента шага 9 не удалась:\n" + r.stderr[-3000:])
    tmp.rename(out)
    return out.read_bytes()


if __name__ == "__main__":
    print(len(build()), "байт")
