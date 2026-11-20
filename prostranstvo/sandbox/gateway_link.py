"""Канал исполнителя к облачному шлюзу. Конец канала выдаёт ведущий."""

from __future__ import annotations

import base64
import json

from env.exchange import pack_json

WIRE_LIMIT = 8192
QUESTION_LIMIT = 4096


class GatewayLink:
    isolated = False

    def __init__(self, conn, timeout: float, manifest: str):
        self.conn, self.timeout, self.manifest = conn, timeout, manifest
        self.dead = False

    def ask_paid(self, question: bytes, limit: int, receipt: dict) -> bytes | None:
        try:
            self.conn.send_bytes(pack_json({"q": base64.b64encode(question).decode(), "limit": limit,
                                            "seq": receipt["seq"], "hash": receipt["hash"]}))
            if self.conn.poll(self.timeout):
                reply = json.loads(self.conn.recv_bytes(WIRE_LIMIT))
                if reply.get("status") == "answer":
                    result = base64.b64decode(reply["a"], validate=True)
                    if len(result) <= limit:
                        return result
                if reply.get("status") == "refused":
                    return None
        except (ValueError, TypeError, OSError, EOFError, KeyError):
            pass
        self.close()
        return None

    def close(self) -> None:
        self.dead = True
        self.conn.close()
