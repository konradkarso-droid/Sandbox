"""Управляемый вывод файлового сеанса к одному разрешённому провайдеру.

Канал привязан ведущим к сеансу и контуру до запуска агента. Агент не
задаёт адрес, ключ, полномочия или тариф. Одна проверенная запись
списания разрешает ровно одну HTTP-попытку. Тайм-аут без ответа
закрывает канал, квитанция отражает неопределённость приёма.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import select
import socket
import os
from pathlib import Path
import threading
import time

from app.cloud import Cloud, CloudError, CloudOutcomeUnknown, PRESETS, ask_once
from env.exchange import digest, input_manifest, pack_json, valid_name
from sandbox.gateway_link import QUESTION_LIMIT, WIRE_LIMIT
from sandbox.ipc import Channel
from verification.observer5 import read_journal

DEFAULT_CONSENT_SECONDS = 3600
PROVIDERS = {p["id"]: p for p in PRESETS if p["base"].startswith("https://")}


@dataclass(frozen=True)
class Consent:
    provider: str
    model: str
    manifest: str


def select_files(exposed: dict, files: dict[str, bytes], cloud: dict) -> tuple[Consent, dict[str, bytes]]:
    if not isinstance(exposed, dict) or set(exposed) != {"provider", "files"}:
        raise ValueError("нужно подтверждение адресата и версий файлов")
    provider, versions = exposed["provider"], exposed["files"]
    preset = PROVIDERS.get(provider) if isinstance(provider, str) else None
    if (not preset or cloud.get("base") != preset["base"] or cloud.get("kind") != preset["kind"]):
        raise ValueError("файловый шлюз принимает только точные адреса провайдеров из списка")
    if not isinstance(versions, list) or not 0 < len(versions) <= len(files):
        raise ValueError("выбери версии файлов для облака")
    selected = {}
    for version in versions:
        if not isinstance(version, dict) or set(version) != {"name", "sha256"}:
            raise ValueError("неверная версия файла")
        name = version["name"]
        if not valid_name(name) or name not in files or name in selected or digest(files[name]) != version["sha256"]:
            raise ValueError("файл изменился после выбора либо версия выбрана повторно")
        selected[name] = files[name]
    return Consent(provider, cloud["model"], digest(pack_json(input_manifest(selected)))), selected


class Gateway:
    def __init__(self, cloud: Cloud, consent: Consent, workdir: Path, timeout: float, on_event=None):
        allowed = PROVIDERS[consent.provider]
        if (cloud.base != allowed["base"] or cloud.kind != allowed["kind"] or cloud.model != consent.model
                or not 1 <= cloud.max_tokens <= 32768):
            raise ValueError("настройки запроса не совпадают с разрешением")
        self.cloud, self.consent = cloud, consent
        self.workdir, self.timeout = workdir, timeout
        self.receipts = workdir.parent.parent / "gateway-receipts.jsonl"
        self.expires = time.monotonic() + DEFAULT_CONSENT_SECONDS
        self.on_event = on_event or (lambda kind, **kw: None)
        self.tokens = {"in": 0, "out": 0}
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.sent = set()
        self.clients = {}
        self.peers = {}
        for contour in (1, 2):
            client, peer = (Channel(sock, WIRE_LIMIT) for sock in socket.socketpair())
            self.clients[contour], self.peers[contour] = client, peer
            threading.Thread(target=self._serve, args=(contour, peer), daemon=True).start()

    def specification(self):
        return {"net": "gateway", "channels": self.clients, "timeout": self.timeout,
                "manifest": self.consent.manifest}

    def _record(self, contour: int, status: str, **fields) -> None:
        data = pack_json({"session": self.workdir.parent.parent.name, "contour": contour,
                          "provider": self.consent.provider, "model": self.consent.model,
                          "status": status, **fields}) + b"\n"
        with self.receipts.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def _authorize(self, contour: int, frame: dict) -> tuple[bytes, int, str]:
        if self.closed.is_set() or time.monotonic() >= self.expires:
            raise ValueError("срок разрешения истёк")
        if not isinstance(frame, dict) or set(frame) != {"q", "limit", "seq", "hash"}:
            raise ValueError("неверный запрос шлюза")
        question = base64.b64decode(frame["q"], validate=True)
        question.decode("utf-8")
        sequence, limit, claimed = frame["seq"], frame["limit"], frame["hash"]
        if (not 0 < len(question) <= QUESTION_LIMIT or type(sequence) is not int or sequence < 0
                or type(limit) is not int or not 0 < limit <= QUESTION_LIMIT):
            raise ValueError("за пределами размера запроса или номера списания")
        rows, broken = read_journal(self.workdir / "journal5.jsonl")
        if broken or not sequence < len(rows):
            raise ValueError("списание не подтверждено журналом")
        setup = next((r for r in rows if r["kind"] == "учёт"), {})
        snapshot = next((r for r in rows if r["kind"] == "снимок обмена"), {})
        charge = 2 * setup.get("gate_price", 0)
        unit = setup.get("model_byte_unit", 0)
        if unit > 0:
            charge += -(-(len(question) + limit) // unit)
        row = rows[sequence]
        if (setup.get("model") != 1 or setup.get("paid_attempts") != 1 or charge <= 0
                or snapshot.get("sha256") != self.consent.manifest or setup.get("cloud_manifest") != self.consent.manifest
                or setup.get("cloud_provider") != self.consent.provider or setup.get("cloud_model") != self.consent.model
                or setup.get("model_answer", 1024) != limit
                or row.get("kind") != "действие" or row.get("signature") != ["вопрос модели", "модель", "свой"]
                or row.get("subject") not in (f"а{contour}", f"п{contour}")
                or row.get("charge") != charge or row.get("hash") != claimed or row.get("length") != len(question)
                or row.get("question_sha256") != digest(question)):
            raise ValueError("сеанс, адресат или списание не согласованы")
        return question, limit, claimed

    def _send(self, contour: int, frame: dict) -> dict:
        try:
            question, limit, claimed = self._authorize(contour, frame)
            with self.lock:
                if claimed in self.sent or self.closed.is_set() or time.monotonic() >= self.expires:
                    raise ValueError("списание уже использовано или сеанс закрыт")
                self._record(contour, "dispatch", receipt=claimed, charge_seq=frame["seq"], size=len(question))
                self.sent.add(claimed)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            with self.lock:
                self._record(contour, "refused", reason=str(exc))
            self.on_event("облако не ответило", reason=str(exc))
            return {"status": "refused"}

        text = question.decode("utf-8")
        started = time.monotonic()
        self.on_event("облако спрошено", length=len(question))
        status, answer, error = "answer", b"", None
        try:
            remaining = min(self.timeout * 0.9, self.expires - started)
            if remaining <= 0 or self.closed.is_set():
                raise CloudError("сеанс шлюза закрыт")
            reply, used = ask_once(self.cloud, text, remaining)
            answer = reply.encode()[:limit]
        except CloudOutcomeUnknown as exc:
            status, error = "unknown", str(exc)
        except (CloudError, ValueError, TypeError) as exc:
            status, error = "provider_error", str(exc)
        with self.lock:
            self._record(contour, status, receipt=claimed)
            if status == "answer":
                for key in self.tokens:
                    value = used.get(key)
                    self.tokens[key] += value if type(value) is int and value >= 0 else 0
                tokens = dict(self.tokens)
        if status == "answer":
            from app.bridge import question_mark
            self.on_event("облако ответило", seconds=round(time.monotonic() - started, 1), tokens=tokens,
                          cut=bool(used.get("cut")), q=question_mark(text))
            return {"status": status, "a": base64.b64encode(answer).decode()}
        self.on_event("облако не ответило", reason=error)
        return {"status": status}

    def _serve(self, contour: int, conn) -> None:
        try:
            while not self.closed.is_set():
                if not select.select([conn.stream], [], [], 0.1)[0]:
                    continue
                frame = conn.receive(time.monotonic() + 5)
                reply = self._send(contour, frame)
                conn.send(reply, time.monotonic() + 5)
                if reply["status"] in ("unknown", "provider_error"):
                    return
        except (OSError, EOFError, ValueError, TypeError, RecursionError):
            return
        finally:
            conn.close()

    def close(self) -> None:
        self.closed.set()
        for conn in (*self.clients.values(), *self.peers.values()):
            conn.close()
