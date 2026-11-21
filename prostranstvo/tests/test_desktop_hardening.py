import os
import socket
import struct
import subprocess
import sys
import time

import pytest

from app import runner, worker
from sandbox.executor5 import Limits, Sandbox
from sandbox.ipc import Channel
from sandbox.model9 import MODEL_LINE_LIMIT, ModelLink


def test_ipc_preserves_types_and_checks_byte_limits():
    left, right = (Channel(sock, 128) for sock in socket.socketpair())
    try:
        value = ({2: b"\x00fixture", "bytes": "literal key"}, ["input"])
        left.send(value)
        assert right.receive() == value
        with pytest.raises(ValueError, match="bounds"):
            left.send("x" * 129)
        left.stream.sendall(struct.pack("!I", 129))
        with pytest.raises(ValueError, match="bounds"):
            right.receive(time.monotonic() + 1)
    finally:
        left.close()
        right.close()


def test_wasm_table_elements_have_a_separate_limit(tmp_path):
    sandbox = Sandbox(tmp_path, limits=Limits(wasm_table_elements=4))
    assert sandbox.load(1, '(module (table 5 funcref) (func (export "main")))')[0] == "отклонён"
    assert sandbox.load(2, '(module (table 4 funcref) (func (export "main")))')[0] == "загружен"


def test_local_model_stops_at_its_wire_limit():
    script = f"import sys; sys.stdin.buffer.readline(); print('x' * {MODEL_LINE_LIMIT}, flush=True)"
    link = ModelLink([sys.executable, "-c", script], timeout=2)
    try:
        assert link.ask(b"fixture", 64) is None
        assert link.dead and link.proc.poll() is not None
    finally:
        link.close()


def test_losing_run_cannot_replace_another_runs_report(tmp_path, monkeypatch):
    from app.exchange import ExchangeJob

    job = ExchangeJob.create(tmp_path / "exchange", tmp_path / "runs")
    job.run.mkdir(parents=True)
    report = job.run / "itog.json"
    report.write_bytes(b"existing owner's report")
    settings = runner.Settings(contour="сомкнутый", exchange=job.id, words="fixture")
    monkeypatch.setattr(worker, "check", lambda: None)
    run = runner.Run(settings, b"", job.run.parent, files={})
    run.run()
    assert run.snapshot()["notes"] == [{"code": "run_exists"}]
    assert report.read_bytes() == b"existing owner's report"


def test_worker_mounts_only_run_state_and_runtime(isolated_executor, tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "host-fixture"
    outside.write_text("local fixture")
    probe = """
from pathlib import Path
import os, sys
state, outside = map(Path, sys.argv[1:])
assert not outside.exists()
assert os.environ["HOME"] == "/tmp"
assert "SANDBOX_AUDIT_FIXTURE" not in os.environ
(state / "result").write_bytes(b"private result")
"""
    argv = worker.command(0, {"job": "probe"}, root)
    argv[argv.index("-c", argv.index("--")) + 1:] = [probe, str(root), str(outside)]
    subprocess.run(argv, env={**os.environ, "SANDBOX_AUDIT_FIXTURE": "local"}, check=True, timeout=15)
    assert (root / "result").read_bytes() == b"private result"


def test_http_body_is_bounded_before_json():
    from io import BytesIO

    from app.http_body import bounded

    assert bounded(BytesIO(b"abcd"), 4, time.monotonic() + 1) == b"abcd"
    with pytest.raises(ValueError, match="byte limit"):
        bounded(BytesIO(b"abcdef"), 4, time.monotonic() + 1)


def test_full_diagnostics_requires_explicit_post(tmp_path, monkeypatch):
    import http.server
    from io import BytesIO
    import threading
    import urllib.error
    import urllib.request
    from zipfile import ZipFile

    from app import server

    stamp = "20260101_120000_0123456789abcdef"
    run = tmp_path / stamp
    (run / "ispolnitel" / "run00001").mkdir(parents=True)
    (run / "itog.json").write_text('{"tally": {}, "result": {}}')
    (run / "ispolnitel" / "run00001" / "journal5.jsonl").write_text("transcript fixture")
    monkeypatch.setattr(runner, "RUNS", tmp_path)
    app = server.App.__new__(server.App)
    app.token = "fixture-session"
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *_: None) as httpd:
        app.port = httpd.server_port
        httpd.RequestHandlerClass = server.make_handler(app)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{app.port}/api/zip/{stamp}"
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(url, data=None):
            return client.open(urllib.request.Request(
                url, data, {"X-Token": app.token, "Content-Type": "application/json"}), timeout=5)

        try:
            with request(base + "?include_transcripts=true") as response:
                with ZipFile(BytesIO(response.read())) as archive:
                    assert archive.namelist() == [f"{stamp}/summary.json"]
            with pytest.raises(urllib.error.HTTPError) as declined:
                request(base, b'{"include_transcripts":false}')
            assert declined.value.code == 400
            declined.value.close()
            with request(base, b'{"include_transcripts":true}') as response:
                with ZipFile(BytesIO(response.read())) as archive:
                    assert archive.read(f"{stamp}/ispolnitel/run00001/journal5.jsonl") == b"transcript fixture"
        finally:
            httpd.shutdown()
            thread.join()
