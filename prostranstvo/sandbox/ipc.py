"""Length-delimited JSON on preconnected sockets. No executable object codec."""

from __future__ import annotations

import base64
import json
import socket
import struct
import time

MAX_MESSAGE = 16 * 1024 * 1024


def _encode(value):
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {"tuple": [_encode(item) for item in value]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {"map": [[key, _encode(item)] for key, item in value.items()]}
    if value is None or type(value) in (str, int, bool, float):
        return value
    raise ValueError("unsupported IPC value")


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"bytes"} and isinstance(value["bytes"], str):
            return base64.b64decode(value["bytes"], validate=True)
        if set(value) == {"tuple"} and isinstance(value["tuple"], list):
            return tuple(_decode(item) for item in value["tuple"])
        if set(value) == {"map"} and isinstance(value["map"], list):
            result = {}
            for pair in value["map"]:
                if not isinstance(pair, list) or len(pair) != 2 or type(pair[0]) not in (str, int):
                    raise ValueError("invalid IPC map entry")
                if pair[0] in result:
                    raise ValueError("duplicate IPC map key")
                result[pair[0]] = _decode(pair[1])
            return result
        raise ValueError("invalid IPC value")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate IPC member")
        result[key] = value
    return result


def _invalid_constant(name):
    raise ValueError(f"invalid IPC number {name}")

class Channel:
    def __init__(self, stream: socket.socket, limit: int = MAX_MESSAGE):
        self.stream, self.limit = stream, limit

    def _set_timeout(self, deadline):
        if deadline is None:
            self.stream.settimeout(None)
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("IPC deadline expired")
            self.stream.settimeout(remaining)

    def _read(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            self._set_timeout(deadline)
            chunk = self.stream.recv(min(65536, size - len(data)))
            if not chunk:
                raise EOFError("IPC peer closed")
            data.extend(chunk)
        return data

    def receive(self, deadline=None):
        size, = struct.unpack("!I", self._read(4, deadline))
        if not 0 < size <= self.limit:
            raise ValueError("IPC message exceeds its bounds")
        return _decode(json.loads(self._read(size, deadline), object_pairs_hook=_unique, parse_constant=_invalid_constant))

    def send(self, value, deadline=None):
        body = json.dumps(_encode(value), ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
        if len(body) > self.limit:
            raise ValueError("IPC message exceeds its bounds")
        self._set_timeout(deadline)
        self.stream.sendall(struct.pack("!I", len(body)) + body)

    def close(self):
        try:
            self.stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.stream.close()
