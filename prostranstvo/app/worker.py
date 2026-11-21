"""Bubblewrap boundary for the desktop executor and independent Wasm inspection."""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import sys
import time

from app import model_sandbox, seal
from app.checkpoint import Checkpoint, NAME
from sandbox.gateway_link import WIRE_LIMIT
from sandbox.ipc import Channel
from sandbox.process5 import Driver, MAX_MODULE, _main

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRS = ("app", "core", "env", "sandbox", "verification")
BOOTSTRAP = """
import json, sys
sys.path[:0] = ["/application", *json.loads(sys.argv[1])]
from app.worker import main
main(int(sys.argv[2]), json.loads(sys.argv[3]))
"""
START_TIMEOUT = 8
INSPECT_TIMEOUT = 12


def command(fd: int, settings: dict, root: Path | None = None) -> list[str]:
    prefix = model_sandbox._prefix()
    runtime, python, packages = model_sandbox._runtime()
    code = [arg for name in RUNTIME_DIRS
            for arg in ("--ro-bind", str(ROOT / name), f"/application/{name}")]
    work = ["--bind", str(root), str(root)] if root is not None else []
    return [*prefix, *runtime, *code, *work, "--remount-ro", "/", "--", python, "-I", "-S", "-B",
            "-c", BOOTSTRAP, json.dumps(packages), str(fd), json.dumps(settings)]


class WorkerProcess(subprocess.Popen):
    """Process operations shared with the standalone executor driver."""

    def is_alive(self):
        return self.poll() is None

    @property
    def exitcode(self):
        return self.poll()

    def join(self, timeout=None):
        try:
            self.wait(timeout)
        except subprocess.TimeoutExpired:
            pass


def launch(settings: dict, root: Path | None = None, channels=()):
    parent, child = socket.socketpair()
    with child:
        try:
            argv = command(child.fileno(), settings, root)
            proc = WorkerProcess(argv, env={}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, pass_fds=(child.fileno(), *channels), umask=0o077)
        except BaseException:
            parent.close()
            raise
    channel = Channel(parent)
    try:
        if channel.receive(time.monotonic() + START_TIMEOUT) != ("ready",):
            raise ValueError("invalid executor readiness message")
    except (EOFError, OSError, ValueError, RecursionError) as exc:
        channel.close()
        if proc.is_alive():
            proc.kill()
        proc.join(2)
        with proc.stderr:
            os.set_blocking(proc.stderr.fileno(), False)
            details = (proc.stderr.read(1024) or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(details or f"executor sandbox did not start: {exc}") from exc
    return proc, channel


def check() -> str | None:
    try:
        proc, channel = launch({"job": "probe"})
    except (OSError, RuntimeError) as exc:
        return str(exc)
    channel.close()
    proc.join(2)
    if proc.is_alive():
        proc.kill()
        proc.join(2)
    proc.stderr.close()
    return None


class IsolatedDriver(Driver):
    def _start(self):
        self.checkpoint = Checkpoint(self.root.parent / NAME)
        spec = dict(self.model or {})
        channels = {i: conn.stream.fileno() for i, conn in spec.pop("channels", {}).items()}
        if channels:
            spec["channels"] = channels
        self.proc, self.conn = launch({"job": "run", "root": str(self.root), "kind": self.kind,
                                       "defects": sorted(self.defects), "limits": self.limits,
                                       "model": spec}, self.root, tuple(channels.values()))

    def close(self):
        super().close()
        self.proc.stderr.close()

    def _receive(self, deadline):
        while True:
            reply = self.conn.receive(deadline)
            if not isinstance(reply, tuple) or len(reply) != 2 or reply[0] != "journal":
                return reply
            self.checkpoint.append(reply[1])
            self.conn.send(("saved",), deadline)


@lru_cache(maxsize=8)
def inspect(source: bytes | str) -> dict:
    proc, channel = launch({"job": "inspect"})
    try:
        deadline = time.monotonic() + INSPECT_TIMEOUT
        channel.send(source, deadline)
        result = channel.receive(deadline)
        if not isinstance(result, dict):
            raise ValueError("invalid module inspection reply")
        if set(result) == {"error"} and isinstance(result["error"], str):
            return result
        imports, exports = result.get("imports"), result.get("exports")
        if (set(result) != {"imports", "exports", "main"} or type(result["main"]) is not bool
                or not isinstance(imports, list)
                or any(not isinstance(pair, tuple) or len(pair) != 2 or not all(isinstance(x, str) for x in pair)
                       for pair in imports)
                or not isinstance(exports, list) or not all(isinstance(name, str) for name in exports)):
            raise ValueError("invalid module inspection reply")
        return result
    finally:
        channel.close()
        if proc.is_alive():
            proc.kill()
        proc.join(2)
        proc.stderr.close()


def _inspect(source) -> dict:
    import wasmtime

    if not isinstance(source, (bytes, str)) or len(source) > MAX_MODULE:
        raise ValueError("invalid module source")
    module = wasmtime.Module(wasmtime.Engine(), source)
    return {"imports": sorted({(item.module, item.name) for item in module.imports}),
            "exports": sorted(item.name for item in module.exports),
            "main": any(item.name == "main" and isinstance(item.type, wasmtime.FuncType) for item in module.exports)}


def main(fd: int, settings: dict) -> None:
    import wasmtime  # load the pinned runtime before accepting any guest data
    from sandbox.model9 import probe_net

    job = settings["job"]
    cpu = 120 if job == "run" else INSPECT_TIMEOUT
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_DATA, (512 << 20, 512 << 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    stream = socket.socket(fileno=fd)
    model = settings.get("model")
    if model and "channels" in model:
        model["channels"] = {int(i): Channel(socket.socket(fileno=number), WIRE_LIMIT)
                             for i, number in model["channels"].items()}
    seal.seal(allow_unix=False, worker=True)
    if reason := probe_net():
        raise RuntimeError(reason)
    channel = Channel(stream)
    channel.send(("ready",))
    if job == "probe":
        return
    if job == "inspect":
        try:
            reply = _inspect(channel.receive())
        except Exception as exc:
            reply = {"error": str(exc).splitlines()[0][:200]}
        channel.send(reply)
        return
    from env.journal import Journal

    def checkpoint(line):
        deadline = time.monotonic() + START_TIMEOUT
        channel.send(("journal", line), deadline)
        if channel.receive(deadline) != ("saved",):
            raise OSError("хост не сохранил запись журнала")

    Journal.checkpoint = staticmethod(checkpoint)
    _main(stream, settings["root"], settings["defects"], settings["limits"], settings["kind"], model)
