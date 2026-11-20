"""Filesystem, process and network boundary for desktop GGUF inference."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import site
import subprocess
import sys
import sysconfig

SERVE = Path(__file__).resolve().parents[1] / "model9" / "serve.py"
SYSTEM_LIBS = ("/usr/lib", "/usr/lib64", "/lib", "/lib64", "/lib32")
START = """
import json, runpy, sys
sys.path[:0] = json.loads(sys.argv[1])
sys.argv = ["/model/serve.py", "--gguf", "/model/weights.gguf"]
print("ready", flush=True)
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _prefix() -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("Bubblewrap (bwrap) is required for local GGUF models")
    return [
        str(Path(bwrap).resolve()), "--unshare-user", "--disable-userns",
        "--unshare-pid", "--unshare-ipc", "--unshare-net", "--new-session",
        "--die-with-parent", "--cap-drop", "ALL", "--clearenv",
        "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
        "--dev", "/dev", "--proc", "/proc", "--remount-ro", "/proc",
        "--tmpfs", "/tmp", "--dir", "/model", "--chdir", "/tmp",
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


def command(model: Path) -> list[str]:
    prefix = _prefix()
    mounts, python, packages = _runtime()
    return [
        *prefix, *mounts, "--ro-bind", str(SERVE), "/model/serve.py",
        "--ro-bind", str(model.resolve(strict=True)), "/model/weights.gguf",
        "--remount-ro", "/", "--", python, "-I", "-S", "-c", START, json.dumps(packages),
    ]


def check() -> str | None:
    """Probe namespace setup and the Python runtime without loading a model."""
    try:
        prefix = _prefix()
        mounts, python, _ = _runtime()
        result = subprocess.run(
            [*prefix, *mounts, "--remount-ro", "/", "--", python, "-I", "-S", "-c",
             "import ctypes, json, socket"], env={}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if result.returncode:
        return result.stderr.strip()[-500:] or f"bwrap exited with status {result.returncode}"
    return None
