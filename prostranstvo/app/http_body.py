"""Deadline-aware streaming of standard library HTTP response bodies."""

from contextlib import contextmanager
import socket
import threading
import time


@contextmanager
def reader(response, deadline: float):
    # urllib returns http.client.HTTPResponse with a buffered SocketIO.
    # Interrupt the socket as well as checking elapsed time between chunks:
    # chunk framing can make a single read1() perform multiple socket reads.
    raw = getattr(getattr(response, "fp", None), "raw", None)
    transport = getattr(raw, "_sock", None)

    def interrupt():
        try:
            transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("HTTP body deadline expired")
    timer = None
    if transport is not None:
        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()

    def read(size):
        if time.monotonic() >= deadline:
            raise TimeoutError("HTTP body deadline expired")
        chunk = response.read1(size)
        if time.monotonic() >= deadline:
            raise TimeoutError("HTTP body deadline expired")
        return chunk

    try:
        yield read
    finally:
        if timer is not None:
            timer.cancel()


def bounded(response, limit: int, deadline: float, *, truncate=False) -> bytes:
    data = bytearray()
    with reader(response, deadline) as read:
        while len(data) <= limit:
            chunk = read(min(16384, limit - len(data) + 1))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
    if truncate:
        return bytes(data[:limit])
    raise ValueError("HTTP response exceeds its byte limit")
