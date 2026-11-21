"""Filesystem, process and network boundary for desktop GGUF inference."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import shutil
import site
import subprocess
import sys
import sysconfig

SERVE = Path(__file__).resolve().parents[1] / "model9" / "serve.py"
SEAL = Path(__file__).resolve().with_name("seal.py")
ENGINE_VERSION = "0.3.37+prostranstvo.1"
ENGINE_SOURCE_REVISION = "a956a852c9c8bc1fb07206364838ddef299178c0"
ENGINE_SECURITY_COMMIT = "50518b64c6f2191a116d2760042f106c4b3387d7"
SYSTEM_LIBS = ("/usr/lib", "/usr/lib64", "/lib", "/lib64", "/lib32")
CONFINEMENT = """
import os, resource, runpy
os.umask(0o077)
for limit, ceiling in ((resource.RLIMIT_AS, {memory}), (resource.RLIMIT_CPU, 1800),
                       (resource.RLIMIT_NOFILE, 128), (resource.RLIMIT_FSIZE, 16 << 20),
                       (resource.RLIMIT_CORE, 0)):
    resource.setrlimit(limit, (ceiling, ceiling))
runpy.run_path('/model/seal.py')['seal'](allow_unix=False, worker=True)
"""
START = """
import json, sys
sys.path[:0] = json.loads(sys.argv[1])
sys.argv = {argv!r}
if sys.argv[1] == '--gguf':
    from importlib.metadata import version
    if version('llama-cpp-python') != {engine!r}:
        raise RuntimeError('unsupported llama-cpp-python version')
print("ready", flush=True)
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _prefix() -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("Bubblewrap (bwrap) is required for local GGUF models")
    return [
        str(Path(bwrap).resolve()), "--unshare-user", "--disable-userns",
        "--unshare-pid", "--unshare-ipc", "--unshare-net", "--unshare-uts", "--new-session",
        "--die-with-parent", "--cap-drop", "ALL", "--clearenv",
        "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
        "--dev", "/dev", "--proc", "/proc", "--remount-ro", "/proc",
        "--size", str(16 << 20), "--tmpfs", "/tmp", "--dir", "/model", "--chdir", "/tmp",
    ]


def _runtime() -> tuple[list[str], str, list[str]]:
    python = Path(sys.executable).resolve()
    stdlib = sysconfig.get_path("stdlib")
    paths = dict.fromkeys((*SYSTEM_LIBS, "/etc/ld.so.cache", str(python), stdlib))
    libdir = sysconfig.get_config_var("LIBDIR")
    if libdir:
        for name in ("INSTSONAME", "LDLIBRARY"):
            library = sysconfig.get_config_var(name)
            if library:
                paths[str(Path(libdir) / library)] = None
    mounts = [arg for path in paths if Path(path).exists() for arg in ("--ro-bind", path, path)]

    packages = []
    site_paths = dict.fromkeys((*site.getsitepackages([sys.prefix]),
                               sysconfig.get_path("purelib"), sysconfig.get_path("platlib")))
    for path in site_paths:
        if Path(path).is_dir():
            target = f"/model/site-{len(packages)}"
            mounts.extend(("--ro-bind", path, target))
            packages.append(target)
    return mounts, str(python), packages


def command(model: Path, *, script: bool = False) -> list[str]:
    prefix = _prefix()
    mounts, python, packages = _runtime()
    path = "/model/steps.json" if script else "/model/weights.gguf"
    mode = "--script" if script else "--gguf"
    # Keep the constants in the isolated bootstrap rather than adding them
    # after the interpreter arguments used by the boundary probe.
    model = model.resolve(strict=True)
    # The weights may be mapped and also copied by the backend. Leave an
    # additional allowance for inference state, libraries and thread stacks.
    memory = (3 << 30) + 2 * model.stat().st_size
    start = CONFINEMENT.format(memory=memory) + START.format(argv=["/model/serve.py", mode, path], engine=ENGINE_VERSION)
    return [
        *prefix, *mounts, "--ro-bind", str(SERVE), "/model/serve.py",
        "--ro-bind", str(SEAL), "/model/seal.py", "--ro-bind", str(model), path,
        "--remount-ro", "/", "--", python, "-I", "-S", "-c", start, json.dumps(packages),
    ]


def engine_available() -> bool:
    try:
        return version("llama-cpp-python") == ENGINE_VERSION
    except PackageNotFoundError:
        return False


def check() -> str | None:
    """Probe namespace setup and the Python runtime without loading a model."""
    try:
        prefix = _prefix()
        mounts, python, _ = _runtime()
        result = subprocess.run(
            [*prefix, *mounts, "--ro-bind", str(SEAL), "/model/seal.py",
             "--remount-ro", "/", "--", python, "-I", "-S", "-c",
             CONFINEMENT.format(memory=3 << 30)], env={}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if result.returncode:
        return result.stderr.strip()[-500:] or f"bwrap exited with status {result.returncode}"
    return None
