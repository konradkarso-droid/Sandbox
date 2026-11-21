"""Streaming HTTP with an elapsed-time budget shared by all connection stages."""

from contextlib import contextmanager
import http.client
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


# A system DNS lookup cannot be cancelled. Bound the number still running
# after callers have stopped waiting for them.
_RESOLVERS = threading.BoundedSemaphore(16)


class _Deadline:
    def __init__(self, deadline):
        self.deadline = deadline
        self._socket = None
        self._lock = threading.Lock()
        self._timer = threading.Timer(self.remaining(), self._interrupt)
        self._timer.daemon = True

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP deadline expired")
        return remaining

    def watch(self, transport, idle_timeout=None):
        with self._lock:
            remaining = self.remaining()
            transport.settimeout(remaining if idle_timeout is None else min(remaining, idle_timeout))
            self._socket = transport

    def _interrupt(self):
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *_):
        self._timer.cancel()

    def _resolve(self, host, port):
        if not _RESOLVERS.acquire(timeout=self.remaining()):
            raise TimeoutError("HTTP deadline expired during address lookup")
        done = threading.Event()
        addresses = []
        errors = []

        def resolve():
            try:
                addresses.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except (OSError, ValueError) as exc:
                errors.append(exc)
            finally:
                _RESOLVERS.release()
                done.set()

        try:
            threading.Thread(target=resolve, daemon=True, name="HTTP address lookup").start()
        except BaseException:
            _RESOLVERS.release()
            raise
        if not done.wait(self.remaining()):
            raise TimeoutError("HTTP deadline expired during address lookup")
        self.remaining()
        if errors:
            raise errors[0]
        return addresses

    def connect(self, address, idle_timeout=None, source_address=None):
        last_error = None
        for family, kind, protocol, _, endpoint in self._resolve(*address):
            self.remaining()
            transport = None
            try:
                transport = socket.socket(family, kind, protocol)
                self.watch(transport, idle_timeout)
                if source_address:
                    transport.bind(source_address)
                transport.connect(endpoint)
                self.remaining()
                return transport
            except OSError as exc:
                if transport is not None:
                    transport.close()
                last_error = exc
        self.remaining()
        raise last_error or OSError("address lookup returned no stream endpoints")


class _HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Track the TLS socket before the handshake as well as the raw
        # connection used for TCP and any proxy CONNECT.
        http.client.HTTPConnection.connect(self)
        host = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=host, do_handshake_on_connect=False)
        self._deadline.watch(self.sock, self.timeout)
        self.sock.do_handshake()


def _connection(kind, deadline):
    def create(host, **kwargs):
        conn = kind(host, **kwargs)
        conn._create_connection = deadline.connect
        if isinstance(conn, _HTTPSConnection):
            conn._deadline = deadline
        return conn

    return create


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline):
        super().__init__()
        self._create = _connection(http.client.HTTPConnection, deadline)

    def http_open(self, request):
        return self.do_open(self._create, request)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline):
        super().__init__()
        self._create = _connection(_HTTPSConnection, deadline)

    def https_open(self, request):
        return self.do_open(self._create, request, context=self._context)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allowed or urllib.parse.urlsplit(newurl).scheme not in ("http", "https"):
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # urllib otherwise drains the entire redirect body before retrying.
            fp.close()
        return redirected


def open_response(request, deadline: float, *, idle_timeout=None, redirects=True, bypass_proxy=False):
    with _Deadline(deadline) as budget:
        handlers = [_HTTPHandler(budget), _HTTPSHandler(budget), _Redirect(redirects)]
        if bypass_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        remaining = budget.remaining()
        timeout = remaining if idle_timeout is None else min(remaining, idle_timeout)
        try:
            response = opener.open(request, timeout=timeout)
        except (OSError, http.client.HTTPException) as exc:
            if time.monotonic() >= deadline:
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
                raise TimeoutError("HTTP deadline expired") from exc
            raise
        if time.monotonic() >= deadline:
            response.close()
            raise TimeoutError("HTTP deadline expired")
        return response


@contextmanager
def reader(response, deadline: float):
    # stdlib urllib returns an HTTPResponse backed by a buffered SocketIO.
    # Shutting down that socket interrupts read1 even within a chunk header.
    raw = getattr(getattr(response, "fp", None), "raw", None)
    transport = getattr(raw, "_sock", None)
    with _Deadline(deadline) as budget:
        if transport is not None:
            budget.watch(transport, transport.gettimeout())

        def read(size):
            budget.remaining()
            chunk = response.read1(size)
            budget.remaining()
            return chunk

        yield read


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
