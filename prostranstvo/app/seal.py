"""Печать сети: фильтр системы (seccomp, Linux) на процесс исполнителя.

Устройство и причины — docs/pesochnitsa_specifikatsiya.md, §4. После
печати `socket()` для любого семейства, кроме местного (AF_UNIX, если он
разрешён), отвечает «нет права» (EPERM); `io_uring_setup` и вызовы чужой
разрядности (x32) — тоже. Фильтр наследуется всеми дочерними процессами
(в том числе через exec) и не снимается: снять его нечем ни исполнителю,
ни процессу модели.

Для строгой пробы контракта §18 (`sandbox.model9.probe_net`) печать — то
же, что на телефоне отсутствие права на сеть: система сама отказывает в
создании сетевого сокета.

Чего печать не умеет:
- только Linux на x86_64 и aarch64 (номера вызовов ниже — этих двух);
- местные каналы при allow_unix=True открыты, и проба их не видит;
- уже открытые соединения не закрываются — ставить печать надо в чистом
  процессе, до всякой работы;
- побочные каналы (время, общий процессор) не закрывает.
"""

from __future__ import annotations

import ctypes
import os
import platform
import socket
import struct
import sys

# архитектура (AUDIT_ARCH_*) и номера вызовов socket, io_uring_setup, seccomp
_ARCH = {
    "x86_64": (0xC000003E, 41, 425, 317),
    "aarch64": (0xC00000B7, 198, 425, 277),
}
_LD_ABS, _JEQ, _JGE, _JSET, _RET = 0x20, 0x15, 0x35, 0x45, 0x06
_ALLOW, _EPERM, _KILL = 0x7FFF0000, 0x00050000 | 1, 0x80000000
_X32 = 0x40000000              # вызовы x32 на x86_64: номер с этим битом
_PR_SET_NO_NEW_PRIVS = 38
_SET_MODE_FILTER, _FLAG_TSYNC = 1, 1


def available() -> str | None:
    """None — печать ставится на этой машине; иначе почему нет."""
    if sys.platform != "linux":
        return f"печать только для Linux, а здесь {sys.platform}"
    if platform.machine() not in _ARCH:
        return f"печать только для x86_64 и aarch64, а здесь {platform.machine()}"
    return None


# link/symlink/mknod and their *at variants. The worker's journal directory
# crosses the mount boundary, so only regular files and directories belong there.
_WORKER_DENIED = {
    "x86_64": (57, 58, 59, 86, 88, 133, 259, 265, 266, 322),
    "aarch64": (33, 36, 37, 221, 281),
}
_WORKER_CLONE = {"x86_64": 56, "aarch64": 220}
_CLONE3, _CLONE_THREAD = 435, 0x00010000
_ENOSYS = 0x00050000 | 38


def _program(arch: int, nr_socket: int, nr_uring: int, allow_unix: bool, denied=(), clone=None) -> bytes:
    def stmt(code: int, k: int) -> bytes:
        return struct.pack("=HBBI", code, 0, 0, k)

    def jump(k: int, jt: int, jf: int, code: int = _JEQ) -> bytes:
        return struct.pack("=HBBI", code, jt, jf, k)

    process_filter = [] if clone is None else [
        jump(_CLONE3, 0, 1), stmt(_RET, _ENOSYS),  # libc can fall back to clone for threads
        jump(clone, 0, 4),
        stmt(_LD_ABS, 16), jump(_CLONE_THREAD, 0, 1, _JSET),
        stmt(_RET, _ALLOW), stmt(_RET, _EPERM),
    ]
    return b"".join([
        stmt(_LD_ABS, 4), jump(arch, 1, 0), stmt(_RET, _KILL),          # чужая архитектура — смерть
        stmt(_LD_ABS, 0),
        jump(_X32, 0, 1, _JGE), stmt(_RET, _EPERM),                     # x32 — отказ
        jump(nr_uring, 0, 1), stmt(_RET, _EPERM),                       # io_uring — отказ
        *(insn for nr in denied for insn in (jump(nr, 0, 1), stmt(_RET, _EPERM))),
        *process_filter,
        jump(nr_socket, 1, 0), stmt(_RET, _ALLOW),                      # не socket — можно
        stmt(_LD_ABS, 16),                                              # семейство (младшие 32 бита)
        jump(socket.AF_UNIX, 0, 1), stmt(_RET, _ALLOW if allow_unix else _EPERM),
        stmt(_RET, _EPERM),
    ])


def seal(allow_unix: bool, *, worker: bool = False) -> None:
    """Поставить печать на этот процесс (все его нити — TSYNC) и всех его
    будущих потомков. Ошибка — OSError: процесс без печати дальше идти не
    должен, решает вызывающий."""
    why = available()
    if why:
        raise OSError(why)
    arch, nr_socket, nr_uring, nr_seccomp = _ARCH[platform.machine()]
    machine = platform.machine()
    denied = _WORKER_DENIED[machine] if worker else ()
    clone = _WORKER_CLONE[machine] if worker else None
    prog = _program(arch, nr_socket, nr_uring, allow_unix, denied, clone)
    buf = ctypes.create_string_buffer(prog, len(prog))

    class _Fprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fprog = _Fprog(len(prog) // 8, ctypes.addressof(buf))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) не прошёл")
    if libc.syscall(nr_seccomp, _SET_MODE_FILTER, _FLAG_TSYNC, ctypes.byref(fprog)) != 0:
        raise OSError(ctypes.get_errno(), "seccomp не принял фильтр")


_CHECK = r"""
import errno, socket, sys
from app.seal import seal
seal(allow_unix=sys.argv[1] == "1")
from sandbox.model9 import probe_net
why = probe_net()
if why:
    print("проба §18 не прошла: " + why); sys.exit(1)
try:
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
    unix = True
except PermissionError:
    unix = False
if unix != (sys.argv[1] == "1"):
    print("местный канал: " + ("открыт" if unix else "закрыт") + " вопреки режиму"); sys.exit(1)
print("ok")
"""


def check(allow_unix: bool) -> str | None:
    """Проверка печати в отдельном процессе: печать встаёт, строгая проба
    §18 проходит, местный канал открыт ровно тогда, когда велено. None —
    всё так; иначе — что не так. Ставить печать в процессе экрана нельзя:
    она не снимается."""
    import subprocess
    why = available()
    if why:
        return why
    root = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    r = subprocess.run([sys.executable, "-c", _CHECK, "1" if allow_unix else "0"], capture_output=True, text=True,
                       cwd=root, env={**os.environ, "PYTHONPATH": root}, timeout=60)
    out = (r.stdout.strip() or r.stderr.strip()[-300:])
    return None if r.returncode == 0 and out == "ok" else (out or f"код {r.returncode}")
