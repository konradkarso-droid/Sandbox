"""Переходник к модели (шаг 9): часть исполнителя, а значит основание доверия.

Устройство и причины — docs/shag9_specifikatsiya.md, §2–§3. Здесь только
передача: байты вопроса — процессу модели, ответ — обратно. Плату, метки,
журнал и отказы ведут ворота (sandbox/executor5b.py, «_ask»).

Процесс модели — свой у каждого контура и запускается при старте
исполнителя, до включения прибора аудита: потом у исполнителя нет повода
запускать процессы. Чем обеспечено «без сети», задаёт параметр запуска
`--model-net` (контракт §18):
- netns — пустое сетевое пространство (`unshare -rn`), если оно доступно;
  иначе — только при явном allow_net=True (тесты на машине без `unshare`),
  и это пишется в отчёте как «сеть у модели не отнята»;
- probe — сеть отнята у всего процесса исполнителя системой (Android без
  разрешения на сеть). Исполнитель это проверяет строгой пробой (probe_net)
  и запускает программу модели напрямую: на телефоне нет `unshare`.

Чего переходник не умеет:
- он не проверяет, что модель инертна, — это обеспечивает запуск без сети
  и проверяет прибор снаружи (strace, `serve.py --probe-net`);
- проба видит только сеть: местные каналы между процессами устройства
  (сокеты внутри устройства, общие файлы) ей не видны;
- после срока ответа процесс модели убит, и переходник больше не отвечает:
  перезапуск процесса во время работы был бы новым событием, которое прибор
  аудита должен был бы различать;
- время ответа модели — побочный канал, и он здесь не закрыт.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import selectors
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

SERVE = Path(__file__).resolve().parents[1] / "model9" / "serve.py"

# Строгая проба (контракт §18, способ probe). Годится один исход: система
# отказала в самом создании сетевого сокета «нет права». EACCES даёт старый
# запрет ядра Android (группа inet), EPERM — нынешний (программа ядра,
# которая смотрит разрешение приложения). Сокет, который создался, не годится,
# даже если маршрута нет: Wi-Fi может вернуться в любую минуту. Любая другая
# ошибка тоже не годится — её смысл пробе неизвестен.
PROBE_SOCKETS = ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET, socket.SOCK_DGRAM),
                 (socket.AF_INET6, socket.SOCK_STREAM), (socket.AF_INET6, socket.SOCK_DGRAM))
NO_RIGHT = (errno.EACCES, errno.EPERM)


def probe_net() -> str | None:
    """None — сети нет: все четыре сокета отказаны «нет права». Иначе —
    почему проба не прошла. Сокет, если создался, сразу закрывается."""
    for fam, typ in PROBE_SOCKETS:
        what = f"{fam.name}/{typ.name}"
        try:
            s = socket.socket(fam, typ)
        except OSError as e:
            if e.errno in NO_RIGHT:
                continue
            return f"{what}: отказ не «нет права» ({errno.errorcode.get(e.errno, e.errno)})"
        s.close()
        return f"{what}: сокет создан — сеть не отнята"
    return None


def _die_with_parent(parent: int):
    """Для дочернего процесса до запуска программы модели: при смерти
    исполнителя — любой, в том числе при убийстве и сбое по инъекции, —
    система сама убивает процесс модели (контракт §18.1). Иначе процесс,
    занятый вопросом, досчитывал бы его после смерти исполнителя: на телефоне
    это минуты полной нагрузки вне предела и вне сторожа. Настройка
    переживает запуск программы (и `unshare`). Исполнитель, умерший раньше,
    чем она выставлена, виден по смене родителя — тогда процесс выходит сам."""
    import ctypes

    def setup() -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, 9, 0, 0, 0) != 0 or os.getppid() != parent:   # PR_SET_PDEATHSIG, SIGKILL
            os._exit(1)
    return setup


def can_unshare() -> bool:
    """Есть ли пустое сетевое пространство на этой машине."""
    if not shutil.which("unshare"):
        return False
    r = subprocess.run(["unshare", "-rn", "true"], capture_output=True)
    return r.returncode == 0


def program() -> list[str]:
    """Программа процесса модели. Если задана переменная MODEL_SERVE (так её
    задаёт обвязка судьи всем исполнителям), — этот файл, запущенный сам как
    исполняемый: так же внешний исполнитель запускает родную программу модели
    на телефоне. Иначе — serve.py рядом, через этот же Python."""
    path = os.environ.get("MODEL_SERVE")
    return [path] if path else [sys.executable, "-I", str(SERVE)]


def argv_for(mode: list[str], allow_net: bool = False) -> list[str]:
    """Команда процесса модели: программа (program) с режимом, обёрнутая в
    пустое сетевое пространство. mode — ["--stub"] или ["--gguf", файл]."""
    base = [*program(), *mode]
    if can_unshare():
        return ["unshare", "-rn", *base]
    if allow_net:
        return base
    raise RuntimeError("нет `unshare -rn`: модель нельзя запустить без сети")


MODEL_LINE_LIMIT = 16 * 1024


def _wait(sel, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not sel.select(remaining):
        raise TimeoutError("model deadline expired")


def _write(fd: int, data: bytes, deadline: float) -> None:
    pending = memoryview(data)
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_WRITE)
        while pending:
            _wait(sel, deadline)
            try:
                pending = pending[os.write(fd, pending):]
            except BlockingIOError:
                continue


def _line(fd: int, deadline: float, limit: int) -> bytes:
    line = bytearray()
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while len(line) < limit:
            _wait(sel, deadline)
            try:
                chunk = os.read(fd, min(4096, limit - len(line)))
            except BlockingIOError:
                continue
            if not chunk:
                raise EOFError("model stream closed")
            line.extend(chunk)
            end = line.find(b"\n")
            if end >= 0:
                return bytes(line[:end])
    raise ValueError("model reply exceeds its bounds")


class ModelLink:
    """Один процесс модели — для одного контура."""

    def __init__(self, argv: list[str], timeout: float = 120.0, isolated: bool | None = None,
                 sandboxed: bool = False):
        self.argv, self.timeout = list(argv), timeout
        # Запущен ли процесс без сети. Режим 2 («модель на устройстве»)
        # включается только при таком запуске (контракт §18): иначе утверждение
        # «вопрос не покидает устройство» ничем не обеспечено. Без явного
        # значения — по команде: обёрнута ли она в пустое сетевое пространство.
        self.isolated = self.argv[:2] == ["unshare", "-rn"] if isolated is None else isolated
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, close_fds=True,
                                     env={} if sandboxed else None,
                                     preexec_fn=None if sandboxed else _die_with_parent(os.getpid()))
        os.set_blocking(self.proc.stdin.fileno(), False)
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.dead = False
        self._lock = threading.Lock()
        if sandboxed:
            try:
                if _line(self.proc.stdout.fileno(), time.monotonic() + 5, len(b"ready\n")) != b"ready":
                    raise ValueError("invalid readiness message")
            except (EOFError, OSError, ValueError) as exc:
                self._die()
                raise RuntimeError("Bubblewrap model sandbox did not start") from exc

    def ask(self, question: bytes, limit: int) -> bytes | None:
        """Ответ не длиннее limit; None — модель не ответила в срок или
        процесс умер. Тогда процесс убит, и дальше переходник молчит."""
        with self._lock:
            if self.dead:
                return None
            line = json.dumps({"q": base64.b64encode(question).decode(), "max": limit}).encode() + b"\n"
            deadline = time.monotonic() + self.timeout
            try:
                _write(self.proc.stdin.fileno(), line, deadline)
                reply = _line(self.proc.stdout.fileno(), deadline, MODEL_LINE_LIMIT)
                a = base64.b64decode(json.loads(reply)["a"], validate=True)
            except (EOFError, OSError, ValueError, RecursionError, KeyError, TypeError):
                return self._die()
            return a[:limit]

    def _die(self) -> None:
        self.dead = True
        try:
            self.proc.kill()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.proc.stdin.close()
        self.proc.stdout.close()
        return None

    def close(self, *, force=False) -> None:
        if force:
            # Wake an active pipe reader before waiting for its lock.
            self.proc.kill()
        with self._lock:
            if self.dead:
                return
            if force:
                self._die()
                return
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._die()
            self.proc.stdout.close()
            self.dead = True


def start_links(spec: dict | None) -> dict[int, ModelLink] | None:
    """spec — {"mode": ["--stub"], "timeout": с, "allow_net": False, "net":
    "netns" | "probe"}; интерфейс Linux также передаёт собственную команду
    Bubblewrap в sandbox_argv. По процессу на контур. RuntimeError — исполнитель не
    может стартовать: модель без сети не запустить."""
    if not spec:
        return None
    mode, timeout = list(spec.get("mode", ["--stub"])), float(spec.get("timeout", 120.0))
    net = spec.get("net", "netns")
    if net == "relay":
        from sandbox.gateway_link import RelayedModel
        return {i: RelayedModel(conn, timeout, spec["isolated"]) for i, conn in spec["channels"].items()}
    if net == "gateway":
        from sandbox.gateway_link import GatewayLink
        return {i: GatewayLink(conn, timeout, spec["manifest"]) for i, conn in spec["channels"].items()}
    if net == "bwrap":
        links = {}
        try:
            for i in (1, 2):
                links[i] = ModelLink(spec["sandbox_argv"], timeout, isolated=True, sandboxed=True)
        except (OSError, RuntimeError):
            for link in links.values():
                link._die()
            raise
        return links
    if net == "probe":
        why = probe_net()
        if why:
            raise RuntimeError(f"--model-net=probe: проба сети не прошла — {why}")
        argv = [*program(), *mode]
        return {i: ModelLink(argv, timeout, isolated=True) for i in (1, 2)}
    if net != "netns":
        raise RuntimeError(f"--model-net: netns или probe, а не {net!r}")
    argv = argv_for(mode, bool(spec.get("allow_net", False)))
    return {i: ModelLink(argv, timeout) for i in (1, 2)}
