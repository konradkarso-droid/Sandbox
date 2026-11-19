"""Связь разомкнутого контура: местный канал ↔ облако.

Устройство — docs/pesochnitsa_specifikatsiya.md, §2, §5. Связь живёт в
процессе ведущего. Передатчик (`model9/serve.py --relay ПУТЬ`), запущенный
исполнителем вместо процесса модели, присылает сюда строку вопроса
({"q": base64, "max": предел}) и ждёт строку ответа ({"a": base64}). Связь
спрашивает облако (app/cloud.py) и отвечает.

Связь — основание доверия разомкнутого контура: через неё вопрос уходит
наружу. Поэтому она ничего не добавляет к вопросу и ничего не дописывает в
ответ. Облако не ответило в срок — связь закрывает канал, передатчик
выходит, исполнитель записывает молчание (−7). Текст ошибки агенту не
подаётся: это были бы слова, которых модель не говорила.

Канал — файл сокета в закрытой папке (права 0700): чужие процессы этого
пользователя до него не дотянутся через права папки, процессы других
пользователей — тем более.

Чего связь не умеет: она не знает, какой контур спрашивает (соединений по
одному на контур, но порядок их установки не задан); кто спрашивал, видно
по журналу среды, а не здесь.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import tempfile
import threading
import time

from app.cloud import Cloud, CloudError, ask

# Сколько секунд срока ответа связь оставляет себе на передачу назад: срок
# считает исполнитель (ModelLink.timeout), и ответ, пришедший к нему позже,
# всё равно станет молчанием.
MARGIN_S = 2.0


class Bridge:
    def __init__(self, cloud: Cloud, timeout_s: float, on_event=None):
        self.cloud, self.timeout_s = cloud, float(timeout_s)
        self.on_event = on_event or (lambda kind, **kw: None)
        self.dir = tempfile.mkdtemp(prefix="prostranstvo_svyaz_")
        os.chmod(self.dir, 0o700)
        self.path = os.path.join(self.dir, "svyaz.sock")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        self.tokens = {"in": 0, "out": 0}
        self.questions = 0
        self.errors: list[str] = []
        self.closed = False
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True, name="связь").start()

    def _accept(self) -> None:
        while not self.closed:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True, name="связь-канал").start()

    def _serve(self, conn: socket.socket) -> None:
        f = conn.makefile("rwb")
        try:
            for line in f:
                try:
                    req = json.loads(line)
                    question = base64.b64decode(req["q"]).decode("utf-8", "replace")
                except (ValueError, KeyError) as e:
                    self._fail(f"передатчик прислал непонятное / unreadable relay line: {e}")
                    return
                t0 = time.monotonic()
                self.on_event("облако спрошено", length=len(question.encode()))
                try:
                    text, used = ask(self.cloud, question, t0 + self.timeout_s - MARGIN_S)
                except CloudError as e:
                    self._fail(str(e))
                    return
                with self._lock:
                    self.questions += 1
                    self.tokens["in"] += int(used.get("in") or 0)
                    self.tokens["out"] += int(used.get("out") or 0)
                self.on_event("облако ответило", seconds=round(time.monotonic() - t0, 1),
                              tokens=dict(self.tokens))
                f.write(json.dumps({"a": base64.b64encode(text.encode("utf-8")).decode()}).encode() + b"\n")
                f.flush()
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _fail(self, why: str) -> None:
        with self._lock:
            self.errors.append(why)
        self.on_event("облако не ответило", reason=why)

    def close(self) -> None:
        self.closed = True
        try:
            self.srv.close()
        except OSError:
            pass
        with self._lock:
            for c in self._conns:
                try:
                    c.close()
                except OSError:
                    pass
        shutil.rmtree(self.dir, ignore_errors=True)
