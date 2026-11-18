"""Исполнитель в отдельном процессе и недоверенный ведущий.

Ведущий (класс Driver) передаёт исполнителю только данные: байты модулей,
текст слов человека, номера контуров. Каждая команда проверяется по типу и
размеру; неизвестная команда — отказ. Состояние исполнителя ведущему не
видно, кроме выгрузки по команде «dump» — она нужна наблюдателю.

Разделение процессов здесь — разделение ролей, а не рубеж безопасности:
оба процесса принадлежат одному пользователю ОС. Рубеж — оболочка
WebAssembly (спецификация шага 5, §6).

Прибор «аудит». В процессе исполнителя стоит перехватчик событий Python
(sys.addaudithook): он записывает открытие файлов вне рабочей папки,
сетевые вызовы и запуск процессов. Это прибор, а не рубеж: код Wasmtime
работает в обход перехватчика, и перехватчик сам живёт в том же процессе.
Он отвечает на вопрос «не трогал ли исполнитель мир на самом деле», в том
числе в порчах, где «снаружи» изображает файл в рабочей папке.

Закон И27 («исполнитель не падает») виден здесь: ответ «сбой» — это
исключение исполнителя, а не ловушка агента; смерть процесса — тоже сбой.

Сбой по плану опыта (шаг 5б) отличается от падения кодом завершения
процесса CRASH_EXIT: процесс умирает сразу, без уборки, и ведущий поднимает
новый командой recover — исполнитель восстанавливается по файлам.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import traceback
from pathlib import Path

MAX_MODULE = 1 << 20          # объявленные числа, не подобранные
MAX_TEXT = 4096
COMMANDS = ("new", "load", "say", "run", "dump", "audit", "recover", "restart")

_NET = ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg", "socket.getaddrinfo",
        "socket.gethostbyname", "http.client.connect", "urllib.Request", "ftplib.connect", "smtplib.connect")
_PROC = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
         "pty.spawn", "ctypes.dlopen")
_FS = ("os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.symlink", "os.link",
       "os.truncate", "shutil.rmtree", "shutil.copyfile", "shutil.move")


class _Audit:
    def __init__(self, root: Path):
        self.root = str(root.resolve())
        self.allowed_read = tuple({str(Path(p).resolve()) for p in sys.path if p} |
                                  {sys.prefix, sys.base_prefix, str(Path(__file__).resolve().parents[1])})
        self.events: list[str] = []
        self.on = False
        sys.addaudithook(self.hook)

    def _inside(self, path, prefixes) -> bool:
        try:
            p = os.path.realpath(os.fsdecode(path))
        except Exception:
            return False
        return any(p == x or p.startswith(x.rstrip("/") + "/") for x in prefixes)

    def hook(self, event: str, args) -> None:
        if not self.on:
            return
        if event == "open":
            path, mode, flags = (list(args) + [None, None, None])[:3]
            if isinstance(path, int) or path is None:
                return
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                      (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
            if writing and not self._inside(path, (self.root,)):
                self.events.append(f"запись вне папки: {os.fsdecode(path)}")
            elif not writing and not self._inside(path, (self.root,) + self.allowed_read):
                self.events.append(f"чтение вне папки: {os.fsdecode(path)}")
        elif event.startswith(_NET):
            self.events.append(f"сеть: {event}")
        elif event.startswith(_PROC):
            self.events.append(f"процесс: {event}")
        elif event.startswith(_FS):
            paths = [a for a in args if isinstance(a, (str, bytes, os.PathLike))]
            if any(not self._inside(p, (self.root,)) for p in paths):
                self.events.append(f"файлы вне папки: {event}")


class _Refused(Exception):
    """Команда ведущего не принята (неверный тип, размер, порядок)."""


CRASH_EXIT = 17          # код завершения процесса при сбое по инъекции (шаг 5б)


def _handle_factory(kind: str = "5"):
    """Разбор команд ведущего. kind — «5» (исполнитель шага 5), «5б» (с
    учётом и провенансом: команды recover и restart, настройки учёта и точка
    сбоя в new) или «6» (то же плюс задачи и проверка ответа)."""
    from sandbox.executor5 import Sandbox
    from sandbox.executor5b import FAULT_POINTS, Sandbox5b, config_error
    from sandbox.executor6 import Sandbox6
    from env.journal import JournalBroken
    state: dict = {}
    accounting = {"5б": Sandbox5b, "6": Sandbox6}.get(kind)
    cfg_keys = set(accounting.config_class.__dataclass_fields__) if accounting else set()

    def fault_of(x):
        if x is None:
            return None
        if not (isinstance(x, (tuple, list)) and len(x) == 2 and x[0] in FAULT_POINTS
                and isinstance(x[1], int) and not isinstance(x[1], bool) and x[1] >= 1):
            raise _Refused("точка сбоя — (имя точки, номер прохода ≥ 1)")
        return (x[0], x[1])

    def workdir(n: int) -> Path:
        return Path(state["root"]) / f"run{n:05d}"

    def handle(msg):
        if not (isinstance(msg, tuple) and msg and msg[0] in COMMANDS):
            raise _Refused("неизвестная команда")
        cmd, args = msg[0], msg[1:]
        if cmd == "new":
            state["n"] = state.get("n", 0) + 1
            if accounting is not None:
                cfg = args[0] if args else None
                if cfg is not None and not (isinstance(cfg, dict) and set(cfg) <= cfg_keys and all(
                        isinstance(v, (int, str)) and not isinstance(v, bool) for v in cfg.values())):
                    raise _Refused("настройки учёта — словарь известных полей")
                conf = accounting.config_class(**(cfg or {}))
                err = config_error(conf)
                if err:
                    raise _Refused(err)
                links = state.get("model_links")
                if conf.model and not links:
                    raise _Refused("режим модели включён, а модель при запуске исполнителя не подключена")
                if conf.model == 2 and not all(link.isolated for link in links.values()):
                    raise _Refused("model = 2, а процесс модели запущен не без сети")
                state["sb"] = accounting(workdir(state["n"]), frozenset(state["defects"]), state["limits"], conf,
                                         fault_of(args[1] if len(args) > 1 else None))
                state["sb"].model_links = state.get("model_links")
            else:
                state["sb"] = Sandbox(workdir(state["n"]), frozenset(state["defects"]), state["limits"])
            return str(state["sb"].workdir)
        if cmd == "audit":
            return list(state["audit"].events)
        if cmd in ("recover", "restart"):
            if accounting is None:
                raise _Refused("нет такой команды у этого исполнителя")
            n = args[0] if args else None
            if not (isinstance(n, int) and not isinstance(n, bool) and n >= 1 and workdir(n).exists()):
                raise _Refused("номер запуска — существующий")
            state["n"] = max(state.get("n", 0), n)
            state["sb"] = None                       # прежнее состояние отброшено целиком
            try:
                state["sb"] = accounting.recover(workdir(n), frozenset(state["defects"]), state["limits"],
                                                fault_of(args[1] if len(args) > 1 else None),
                                                "сбой" if cmd == "recover" else "перезапуск")
            except JournalBroken as e:
                raise _Refused(f"журнал испорчен, исполнитель не запускается: {e}")
            links = state.get("model_links")
            # Без модели восстановление разрешено (контракт §17): вопросы получат
            # −7. Но режим 2 с моделью, у которой есть сеть, не продолжается.
            if state["sb"].cfg.model == 2 and links and not all(link.isolated for link in links.values()):
                state["sb"] = None
                raise _Refused("model = 2, а процесс модели запущен не без сети")
            state["sb"].model_links = links
            return str(state["sb"].workdir)
        sb = state.get("sb")
        if sb is None:
            raise _Refused("нет песочницы: сначала new")
        if cmd == "dump":
            return sb.dump()
        contour = args[0] if args else None
        if contour not in (1, 2) or isinstance(contour, bool):
            raise _Refused("контур — 1 или 2")
        if cmd == "load":
            src = args[1] if len(args) > 1 else None
            if not isinstance(src, (bytes, str)) or len(src) > MAX_MODULE:
                raise _Refused("модуль — байты или текст не длиннее 1 МиБ")
            return sb.load(contour, src)
        if cmd == "say":
            text = args[1] if len(args) > 1 else None
            if not isinstance(text, str) or len(text) > MAX_TEXT:
                raise _Refused("слова — строка не длиннее 4096 знаков")
            sb.say(contour, text)
            return None
        if cmd == "run":
            return sb.run(contour)
        raise _Refused("неизвестная команда")

    return handle, state


def _main(conn, root: str, defects: list[str], limits: dict | None, kind: str = "5",
          model: dict | None = None) -> None:
    import wasmtime  # noqa: F401  — библиотека грузится до включения аудита
    from sandbox.executor5 import Limits
    from sandbox.executor5b import Crash
    from sandbox.model9 import start_links
    rootp = Path(root)
    rootp.mkdir(parents=True, exist_ok=True)
    handle, state = _handle_factory(kind)
    # Процессы модели (шаг 9) — до включения аудита: потом исполнителю
    # запускать процессы незачем, и прибор считает любой запуск событием.
    state.update(root=root, defects=list(defects), limits=Limits(**limits) if limits else Limits(),
                 model_links=start_links(model))
    state["audit"] = _Audit(rootp)
    state["audit"].on = True
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        try:
            reply = ("ok", handle(msg))
        except _Refused as e:
            reply = ("отказ", str(e))
        except Crash:
            os._exit(CRASH_EXIT)               # сбой: процесс умирает без уборки
        except Exception:
            reply = ("сбой", traceback.format_exc(limit=8))
        conn.send(reply)


class Driver:
    """Недоверенный ведущий. Запускает исполнитель в отдельном процессе и
    говорит с ним только сообщениями."""

    def __init__(self, root: Path, defects: frozenset[str] = frozenset(), limits: dict | None = None,
                 kind: str = "5", model: dict | None = None, timeout: float = 120.0):
        self.root, self.defects, self.limits, self.kind, self.model = root, defects, limits, kind, model
        # Сколько ведущий ждёт ответа на команду. С живой моделью запуск контура
        # идёт минутами (шаг 9): ведущий, бросивший ждать, путает ответы команд.
        self.timeout = timeout
        self.failures: list[str] = []        # для закона И27
        self.crashes = 0                     # сбои по инъекции — разрешены
        self._start()

    def _start(self) -> None:
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_main, args=(child, str(self.root), sorted(self.defects), self.limits,
                                                    self.kind, self.model), daemon=True)
        self.proc.start()
        child.close()

    def respawn(self) -> None:
        """Новый процесс исполнителя после сбоя. Состояние — только файлы."""
        self.close()
        self._start()

    def call(self, *msg, timeout: float | None = None):
        timeout = self.timeout if timeout is None else timeout
        if not self.proc.is_alive():
            self.failures.append("процесс исполнителя мёртв")
            return ("сбой", "процесс исполнителя мёртв")
        self.conn.send(msg)
        if not self.conn.poll(timeout):
            self.failures.append(f"исполнитель не ответил за {timeout} с на {msg[0]}")
            return ("сбой", "нет ответа")
        try:
            status, value = self.conn.recv()
        except (EOFError, OSError):
            self.proc.join(timeout=10)
            if self.proc.exitcode == CRASH_EXIT:
                self.crashes += 1
                return ("сбой-инъекция", "процесс исполнителя завершён в точке сбоя")
            self.failures.append(f"процесс исполнителя умер на команде {msg[0]} (код {self.proc.exitcode})")
            return ("сбой", "процесс умер")
        if status == "сбой":
            self.failures.append(value)
        return status, value

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self.proc.join(timeout=10)
            if self.proc.is_alive():
                self.proc.terminate()


class LocalDriver:
    """Тот же разбор команд, что в процессе исполнителя, но в этом же
    процессе и без аудита — для тестов. Отвечает так же, как Driver;
    сбой по инъекции отбрасывает песочницу вместо смерти процесса."""

    def __init__(self, root: Path, defects: frozenset[str] = frozenset(), limits: dict | None = None,
                 kind: str = "5", model: dict | None = None):
        from sandbox.executor5 import Limits
        from sandbox.model9 import start_links

        class _NoAudit:
            events: list[str] = []

        self.handle, self.state = _handle_factory(kind)
        self.state.update(root=str(root), defects=sorted(defects),
                          limits=Limits(**limits) if limits else Limits(), audit=_NoAudit(),
                          model_links=start_links(model))
        self.failures: list[str] = []
        self.crashes = 0

    def respawn(self) -> None:
        self.state["sb"] = None

    def call(self, *msg):
        from sandbox.executor5b import Crash
        try:
            return ("ok", self.handle(msg))
        except _Refused as e:
            return ("отказ", str(e))
        except Crash:
            self.state["sb"] = None
            self.crashes += 1
            return ("сбой-инъекция", "исполнитель упал в точке сбоя")
        except Exception:
            self.failures.append(traceback.format_exc(limit=8))
            return ("сбой", self.failures[-1])

    def close(self) -> None:
        for link in (self.state.get("model_links") or {}).values():
            link.close()
