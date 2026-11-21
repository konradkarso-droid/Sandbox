"""Сборка родной программы модели (serve9.cpp рядом) для сервера и телефона.

  python3 model9/native/build.py server  --llama ИСХОДНИКИ --out ПАПКА
  python3 model9/native/build.py android --llama ИСХОДНИКИ --ndk NDK --out ПАПКА

ИСХОДНИКИ — архив llama_cpp_python-0.3.37.tar.gz с PyPI (из него берётся
vendor/llama.cpp, хеш архива сверяется) или уже распакованная папка llama.cpp.
Версия закреплена: с этим пакетом сверяется serve.py, а числа модели зависят
от версии библиотеки (почему — serve9.cpp, «Живая модель»). Страница llama.cpp
на GitHub из среды программы недоступна, PyPI — доступен.

Сервер: те же умолчания сборки, что у pip install llama-cpp-python (под этот
процессор, с OpenMP), — иначе ответы с serve.py не совпали бы байт в байт.

Телефон (NDK r27, arm64-v8a, Android 10 и новее) — без OpenMP и без
llamafile. Две библиотеки JNI загружаются в изолированные службы:
  serve9-android-armv8a  — переносимая, на любом arm64;
  serve9-android-armv82  — с dotprod, i8mm и fp16 (их умеют ядра Cortex-X4 и
                           A720 телефона шага); на процессоре без них падает
                           на первой же неизвестной команде.
Какая пойдёт на телефоне, решает малый стенд (запустилась ли и с какой
скоростью). Ответы телефона с ответами сервера байт в байт не совпадут: другой
процессор — другие округления; на телефоне проверяется повторяемость (И35р).
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SDIST = "llama_cpp_python-0.3.37.tar.gz"
SDIST_SHA256 = "bf4108fc76e933ea4969b61b1e281a2d232dd3e43361d1fdca5e2e029d62f729"

ANDROID = {
    "serve9-android-armv8a": [],
    "serve9-android-armv82": ["-DGGML_CPU_ARM_ARCH=armv8.2-a+dotprod+i8mm+fp16"],
}


def engine_source(path: Path, tmp: Path, *, package_version: str | None = None) -> Path:
    """Verify the release archive and apply the upstream security backport."""
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != SDIST_SHA256:
        raise ValueError(f"архив не тот: sha256 {digest}, ждали {SDIST_SHA256} ({SDIST})")
    with tarfile.open(path) as t:
        t.extractall(tmp, filter="data")
    source = tmp / SDIST.removesuffix(".tar.gz")
    subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i",
                    str(HERE / "llama-cve-2026-133710.patch")], cwd=source / "vendor" / "llama.cpp", check=True)
    if package_version is not None:
        init = source / "llama_cpp" / "__init__.py"
        code = init.read_text(encoding="utf-8")
        old = '__version__ = "0.3.37"'
        if code.count(old) != 1:
            raise ValueError("не найдена версия исходного пакета llama-cpp-python")
        init.write_text(code.replace(old, f'__version__ = "{package_version}"'), encoding="utf-8")
    return source


def llama_src(path: Path, tmp: Path) -> Path:
    if path.is_dir():
        if not (path / "include" / "llama.h").exists():
            sys.exit(f"не исходники llama.cpp: {path}")
        return path
    return engine_source(path, tmp) / "vendor" / "llama.cpp"


def cmake(src: Path, build: Path, extra: list[str]) -> None:
    subprocess.run(["cmake", "-S", str(HERE), "-B", str(build), f"-DLLAMA_SRC={src}", "-DCMAKE_BUILD_TYPE=Release", *extra],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["cmake", "--build", str(build), "-j", "2", "--target", "serve9"], check=True, stdout=subprocess.DEVNULL)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", choices=["server", "android"])
    ap.add_argument("--llama", required=True, type=Path)
    ap.add_argument("--ndk", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="serve9_") as t:
        tmp = Path(t)
        src = llama_src(a.llama, tmp)
        if a.target == "server":
            cmake(src, tmp / "b", [])
            shutil.copy2(tmp / "b" / "serve9", a.out / "serve9")
            print(a.out / "serve9")
            return
        if not a.ndk or not (a.ndk / "build" / "cmake" / "android.toolchain.cmake").exists():
            sys.exit("нужен --ndk: папка Android NDK r27")
        base = [f"-DCMAKE_TOOLCHAIN_FILE={a.ndk}/build/cmake/android.toolchain.cmake", "-DANDROID_ABI=arm64-v8a",
                "-DANDROID_PLATFORM=android-29", "-DANDROID_STL=c++_static", "-DGGML_NATIVE=OFF",
                "-DGGML_OPENMP=OFF", "-DGGML_LLAMAFILE=OFF"]
        strip = next((a.ndk / "toolchains" / "llvm" / "prebuilt").glob("*/bin/llvm-strip"))
        for name, flags in ANDROID.items():
            cmake(src, tmp / name, base + flags)
            subprocess.run([str(strip), "-o", str(a.out / name), str(tmp / name / "serve9")], check=True)
            print(a.out / name)


if __name__ == "__main__":
    main()
