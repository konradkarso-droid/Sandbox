"""Preconnected channels from the isolated executor to its model providers."""

from __future__ import annotations

import hashlib
import select
import socket
import threading
import time

from app.cloud import Cloud, CloudError, ask
from sandbox.gateway_link import QUESTION_LIMIT, WIRE_LIMIT
from sandbox.ipc import Channel
from sandbox.model9 import ModelLink


def question_mark(question: str) -> str:
    return hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]


MARGIN_S = 2.0


class ModelChannels:
    def __init__(self, handlers, timeout: float, *, isolated: bool):
        self.timeout, self.isolated = timeout, isolated
        self.clients, self.peers = {}, []
        self.closed = threading.Event()
        for contour, ask_model in enumerate(handlers, 1):
            client, peer = (Channel(sock, WIRE_LIMIT) for sock in socket.socketpair())
            self.clients[contour] = client
            self.peers.append(peer)
            threading.Thread(target=self._serve, args=(peer, ask_model),
                             daemon=True, name="канал модели").start()

    def specification(self):
        return {"net": "relay", "channels": self.clients, "timeout": self.timeout,
                "isolated": self.isolated}

    def _serve(self, peer, ask_model):
        try:
            while not self.closed.is_set():
                if not select.select([peer.stream], [], [], 0.1)[0]:
                    continue
                request = peer.receive(time.monotonic() + 5)
                if not isinstance(request, tuple) or len(request) != 2:
                    return
                question, limit = request
                if (not isinstance(question, bytes) or not 0 < len(question) <= QUESTION_LIMIT
                        or type(limit) is not int or not 0 < limit <= QUESTION_LIMIT):
                    return
                answer = ask_model(question, limit)
                peer.send(answer, time.monotonic() + 5)
                if answer is None:
                    return
        except (ValueError, OSError, EOFError, RecursionError):
            return
        finally:
            peer.close()

    def close(self):
        self.closed.set()
        for channel in (*self.clients.values(), *self.peers):
            channel.close()


class LocalModels(ModelChannels):
    def __init__(self, argv, timeout):
        self.models = []
        try:
            for _ in range(2):
                self.models.append(ModelLink(argv, timeout, isolated=True, sandboxed=True))
            super().__init__([model.ask for model in self.models], timeout, isolated=True)
        except BaseException:
            for model in self.models:
                model.close(force=True)
            raise

    def close(self):
        super().close()
        for model in self.models:
            model.close(force=True)


class Bridge(ModelChannels):
    def __init__(self, cloud: Cloud, timeout_s: float, on_event=None):
        self.cloud = cloud
        self.on_event = on_event or (lambda kind, **kw: None)
        self.tokens = {"in": 0, "out": 0}
        self.questions = 0
        self.errors: list[str] = []
        self._lock = threading.Lock()
        super().__init__([self._ask, self._ask], timeout_s, isolated=False)

    def _ask(self, raw: bytes, limit: int):
        question = raw.decode("utf-8", "replace")
        started = time.monotonic()
        self.on_event("облако спрошено", length=len(question.encode()))
        try:
            text, used = ask(self.cloud, question, started + self.timeout - MARGIN_S)
        except CloudError as exc:
            why = str(exc)
            with self._lock:
                self.errors.append(why)
            self.on_event("облако не ответило", reason=why)
            return None
        with self._lock:
            self.questions += 1
            for key in self.tokens:
                value = used.get(key)
                self.tokens[key] += value if type(value) is int and value >= 0 else 0
            tokens = dict(self.tokens)
        self.on_event("облако ответило", seconds=round(time.monotonic() - started, 1),
                      tokens=tokens, cut=bool(used.get("cut")), q=question_mark(question))
        return text.encode("utf-8")[:limit]
