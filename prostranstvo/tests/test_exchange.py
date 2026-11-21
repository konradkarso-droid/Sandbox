"""Приёмка обмена на агенте WebAssembly и на отдельном получателе."""

import base64
import struct
from dataclasses import asdict

import pytest

from env.exchange import ExchangeStore, MAX_FILE_BYTES, MAX_STORAGE_BYTES, digest, pack_json, snapshot_inbox
from sandbox.exchange_executor import ExchangeConfig
from sandbox.process5 import Driver, LocalDriver
from sandbox.run_session import run_contours
from verification import exchange_observer
from verification.observer5 import BROKEN, read_journal

IDLE = "(module (func (export \"main\")))"


def agent(*calls, after_calls=""):
    module = ['(module (import "env" "call" (func $call (param i32 i32 i32 i32) (result i32)))',
              '(memory (export "memory") 2 2)']
    main = []
    for index, item in enumerate(calls):
        req, want, cap = item
        frame = struct.pack("<I", 4096) + pack_json(req)
        address = 16 + index * 4096
        escaped = "".join(f"\\{byte:02x}" for byte in frame)
        module.append(f'(data (i32.const {address}) "{escaped}")')
        main.extend((f"(call $call (i32.const {cap}) (i32.const 9) (i32.const {address}) (i32.const {len(frame)}))",
                     f"(i32.const {want})", "(i32.ne)" if want < 0 else "(i32.lt_s)",
                     "(if (then unreachable))"))
    module.extend(('(func (export "main")', *main, after_calls, "))"))
    return "\n".join(module)


def record_set(run):
    from pathlib import Path
    root = Path(run["workdir"])
    rows, broken = read_journal(root / "journal5.jsonl")
    assert not broken
    return rows


def exercise(root, source, *, second=IDLE, fault=None, budget=40, driver_cls=LocalDriver):
    cfg = asdict(ExchangeConfig(budget=budget, gate_price=2, price=7))
    driver = driver_cls(root, kind="exchange")
    try:
        return run_contours(driver, None, "файлы", {1: source, 2: second}, cfg, fault=fault, rounds=1,
                            files={"вход.txt": b"source"}, checker=exchange_observer.check)
    finally:
        driver.close()


def test_wasm_exchange_has_paid_operations_and_no_external_output(tmp_path):
    write = {"tool": "files.write", "name": "result.txt", "offset": 0, "data": base64.b64encode(b"hello").decode()}
    publish = {"tool": "files.publish", "name": "result.txt"}
    source = agent(({"tool": "files.list"}, 1, 8), ({"tool": "files.read", "id": 1, "offset": 0, "length": 6}, 1, 8),
                   (write, 1, 8), (publish, 1, 8), (publish, -6, 8))
    run = exercise(tmp_path, source, second=agent(({"tool": "files.list"}, -1, 8)), driver_cls=Driver)
    records = record_set(run)
    assert run["runs"] == [(1, ("завершён", "")), (2, ("завершён", ""))]
    assert [r["charge"] for r in records if r["kind"] == "инструмент"] == [2, 2, 4, 7]
    assert [r["contour"] for r in records if r["kind"] == "отказ ворот"] == [1, 2]
    assert run["dump"]["contours"][1]["budget"] == 23
    assert not list(tmp_path.rglob("result.txt"))
    assert {k: v for k, v in run["laws"].items() if v[0] == BROKEN} == {}


def test_bad_calls_pay_until_stop(tmp_path):
    request = {"tool": "files.read", "id": 3, "offset": 0, "length": 1}
    calls = ((request, -6, 8), (request, -3, 8), (request, -3, 8), (request, -3, 8))
    run = exercise(tmp_path, agent(*calls), budget=3)
    records = record_set(run)
    assert [r["charge"] for r in records if r["kind"] == "отказ ворот"] == [2, 1]
    assert sum(r["kind"] == "останов" for r in records) == 1
    assert run["runs"][0][1] == ("ловушка", "бюджет исчерпан")
    assert {k: v for k, v in run["laws"].items() if v[0] == BROKEN} == {}


def test_exchange_wasm_cannot_grow_beyond_two_pages(tmp_path):
    source = """
    (module
      (memory (export "memory") 1)
      (func (export "main")
        (if (i32.ne (memory.grow (i32.const 1)) (i32.const 1)) (then unreachable))
        (if (i32.ne (memory.grow (i32.const 1)) (i32.const -1)) (then unreachable))
        (if (i32.ne (memory.size) (i32.const 2)) (then unreachable))))
    """
    run = exercise(tmp_path, source, driver_cls=Driver)
    assert run["runs"][0] == (1, ("завершён", ""))
    assert run["laws"]["И27"][0] != BROKEN


def test_exchange_fuel_stop_leaves_draft_unpublished(isolated_executor, tmp_path):
    from app import runner
    from app.exchange import ExchangeJob

    job = ExchangeJob.create(tmp_path / "exchange", tmp_path / "runs")
    write = {"tool": "files.write", "name": "result.txt", "offset": 0, "data": "ZHJhZnQ="}
    source = agent((write, 1, 8), after_calls="(loop $spin (br $spin))")
    settings = runner.Settings(contour="сомкнутый", exchange=job.id, agent="test.wat", words="Сохрани черновик")
    run = runner.Run(settings, source, job.run.parent, files={})
    run.start()
    try:
        run.join(15)
        assert run.finished.is_set(), run.snapshot()
        assert run.result["contours"][1]["outcome"] == [("ловушка", "топливо кончилось")]
        assert run.result["broken"] == []
        assert run.driver is not None and not run.driver.proc.is_alive()
        store = ExchangeStore(job.workdir / "exchange-state", create=False)
        assert store.read_blob(digest(b"draft")) == b"draft"
        assert job.info()["requests"] == []
        assert list((job.root / "outbox").iterdir()) == []
    finally:
        if run.is_alive():
            run.stop()
            run.join(10)


def test_exchange_storage_refuses_new_bytes_at_quota(tmp_path):
    store = ExchangeStore(tmp_path / "exchange")
    for index in range(MAX_STORAGE_BYTES // MAX_FILE_BYTES):
        data = bytes([index]) * MAX_FILE_BYTES
        store.put_blob(digest(data), data)
    assert store.used == MAX_STORAGE_BYTES

    reopened = ExchangeStore(store.path, create=False)
    reopened.put_blob(digest(data), data)
    assert reopened.used == MAX_STORAGE_BYTES
    new = b"one more version"
    with pytest.raises(ValueError, match="квота"):
        reopened.put_blob(digest(new), new)
    assert not (reopened.blobs / digest(new)).exists()
    assert reopened.read_blob(digest(data)) == data


@pytest.mark.parametrize("stage", ["после списания", "после доставки"])
def test_interrupted_write_is_paid_and_does_not_create_a_publishable_draft(tmp_path, stage):
    write = {"tool": "files.write", "name": "draft.txt", "offset": 0, "data": "ZGF0YQ=="}
    run = exercise(tmp_path, agent((write, 1, 8)), fault=(stage, 1))
    records = record_set(run)
    assert run["dump"]["contours"][1]["budget"] == 36
    assert not [r for r in records if r["kind"] == "итог инструмента"]
    assert {k: v for k, v in run["laws"].items() if v[0] == BROKEN} == {}


def test_snapshot_only_admits_regular_files(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    data = inbox / "selected.txt"
    data.write_bytes(b"before")
    snapshot = snapshot_inbox(inbox)
    data.write_bytes(b"after")
    assert snapshot == {"selected.txt": b"before"}
    (inbox / "symbolic").symlink_to(data)
    with pytest.raises((ValueError, OSError)):
        snapshot_inbox(inbox)


def test_refuses_cloud_and_unpaid_exchange(tmp_path):
    driver = LocalDriver(tmp_path, kind="exchange")
    for cfg in ({"model": 1}, {"paid_attempts": 0}, {"gate_price": 0}, {"rule": "M0"}):
        assert driver.call("new", cfg, None, {})[0] == "отказ"


def file_agent():
    write = {"tool": "files.write", "name": "result.txt", "offset": 0, "data": "aGVsbG8="}
    return agent((write, 1, 8), ({"tool": "files.publish", "name": "result.txt"}, 1, 8))


def completed_job(tmp_path):
    from app import runner
    from app.exchange import ExchangeJob

    runs = tmp_path / "runs"
    job = ExchangeJob.create(tmp_path / "exchange", runs)
    job.add_input("source.txt", b"source")
    settings = runner.Settings(contour="сомкнутый", exchange=job.id, agent="test.wat", words="Сохрани результат")
    run = runner.Run(settings, file_agent(), runs, files=snapshot_inbox(job.root / "inbox"))
    run.start()
    run.join(15)
    assert run.finished.is_set(), run.snapshot()
    assert run.result["broken"] == [], run.snapshot()
    proposal, = job.info()["requests"]
    return job, proposal["id"]


def test_outbox_approval_is_per_result_and_repeat_finishes_receipt(isolated_executor, tmp_path):
    job, ident = completed_job(tmp_path)
    outbox = job.root / "outbox"
    assert not list(outbox.iterdir())
    with pytest.raises(OSError):
        job.download(ident)
    with pytest.raises(ValueError):
        job.approve(ident + 1)
    receipt = job.approve(ident)
    stable = outbox / receipt["name"]
    assert stable.read_bytes() == b"hello"
    inode = stable.stat().st_ino
    (job.root / "receipts" / f"{ident}.json").unlink()
    assert job.approve(ident) == receipt
    assert stable.stat().st_ino == inode
    assert list(outbox.iterdir()) == [stable]
    assert job.download(ident) == ("result.txt", b"hello")
    assert job.info()["requests"][0]["published"] is True


def test_request_is_bound_to_checked_journal(isolated_executor, tmp_path):
    from env.journal import Journal

    job, ident = completed_job(tmp_path)
    path = job.workdir / "journal5.jsonl"
    journal, _ = Journal.open(path)
    journal.append({"kind": "модельный сбой наблюдения"})
    with pytest.raises(ValueError, match="журнал изменился"):
        job.approve(ident)
    assert not list((job.root / "outbox").iterdir())


def test_approval_never_overwrites_different_recipient_file(isolated_executor, tmp_path):
    job, ident = completed_job(tmp_path)
    path = job.root / "outbox" / f"{ident}-result.txt"
    path.write_bytes(b"already there")
    with pytest.raises(ValueError, match="другой файл"):
        job.approve(ident)
    assert path.read_bytes() == b"already there"
    assert not list((job.root / "receipts").iterdir())


def test_diagnostics_zip_excludes_unapproved_blobs(isolated_executor, tmp_path, monkeypatch):
    from zipfile import ZipFile
    from app import runner

    job, _ = completed_job(tmp_path)
    monkeypatch.setattr(runner, "RUNS", job.run.parent)
    fixture = b"PRIVATE-TRANSCRIPT-FIXTURE"
    (job.workdir / "journal5.jsonl").write_bytes(fixture)
    with ZipFile(runner.zip_run(job.id)) as archive:
        assert archive.namelist() == [f"{job.id}/summary.json"]
        assert fixture not in archive.read(archive.namelist()[0])
    with ZipFile(runner.zip_run(job.id, detailed=True)) as archive:
        journal, = [name for name in archive.namelist() if name.endswith("journal5.jsonl")]
        assert archive.read(journal) == fixture
        assert all("exchange-state" not in name for name in archive.namelist())


def test_builtin_agent_can_use_file_tools_with_local_model_transport(isolated_executor, tmp_path, monkeypatch):
    from app import runner
    from app.exchange import ExchangeJob

    script = tmp_path / "model-script.json"
    requests = [
        'FILES {"tool":"files.list"}',
        'FILES {"tool":"files.read","id":1,"offset":0,"length":6}',
        'FILES {"tool":"files.write","name":"from-model.txt","offset":0,"data":"aGVsbG8="}',
        'FILES {"tool":"files.publish","name":"from-model.txt"}',
        'STOP',
    ]
    script.write_bytes(pack_json(requests))
    monkeypatch.setattr(runner, "INSTALL_SCRIPT", script)
    job = ExchangeJob.create(tmp_path / "exchange", tmp_path / "runs")
    settings = runner.Settings(contour="сомкнутый", model=runner.INSTALL_CHECK, exchange=job.id,
                               agent_lang="en", words="Use the files.")
    run = runner.Run(settings, runner.agent_module("en", files=True)[0], job.run.parent, files={"source.txt": b"source"})
    run.start()
    run.join(15)
    assert run.finished.is_set(), run.snapshot()
    assert run.result["broken"] == [], run.snapshot()
    rows = record_set(run.result)
    assert [r["tool"] for r in rows if r["kind"] == "инструмент"] == [
        "files.list", "files.read", "files.write", "files.publish"]
    assert any("c291cmNl" in row.get("question", "") for row in rows)
    proposal, = job.info()["requests"]
    assert proposal["name"] == "from-model.txt"
    assert list((job.root / "outbox").iterdir()) == []


def test_http_workflow_from_session_creation_to_download(isolated_executor, tmp_path, monkeypatch):
    import http.server
    import json
    import threading
    import urllib.error
    import urllib.parse
    import urllib.request

    from app import runner, server

    monkeypatch.setattr(server, "EXCHANGES", tmp_path / "exchange")
    monkeypatch.setattr(server, "RUNS", tmp_path / "runs")
    # Run() uses the runner's own default, while the HTTP receiver uses server.RUNS.
    monkeypatch.setattr(runner, "RUNS", server.RUNS)
    monkeypatch.setattr(runner, "AGENTS", tmp_path / "agents")
    monkeypatch.setattr(server, "AGENTS", runner.AGENTS)
    runner.AGENTS.mkdir()
    (runner.AGENTS / "test.wat").write_text(file_agent())
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *_: None)
    app = server.App(httpd.server_port)
    httpd.RequestHandlerClass = server.make_handler(app)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None, *, auth=True):
        headers = {"X-Token": app.token} if auth else {}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = pack_json(body)
        url = f"http://127.0.0.1:{httpd.server_port}{path}"
        return opener.open(urllib.request.Request(url, payload, headers), timeout=5)

    def get_json(path, body=None):
        with request(path, body) as response:
            return json.load(response)

    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            request("/api/exchange/new", {}, auth=False)
        assert denied.value.code == 403
        job = get_json("/api/exchange/new", {})
        ident = job["id"]
        assert get_json("/api/exchange/upload", {"id": ident, "name": "file.txt", "data": "aW5wdXQ="}) == {"ok": True}
        with pytest.raises(urllib.error.HTTPError) as cloud:
            request("/api/start", {"exchange": ident, "words": "Работай", "contour": "разомкнутый"})
        assert cloud.value.code == 400
        assert get_json("/api/start", {"exchange": ident, "words": "Работай", "contour": "сомкнутый",
                                      "agent": "test.wat", "model": ""}) == {"id": ident}
        app.run.join(15)
        assert app.run.finished.is_set(), app.run.snapshot()
        receipt, = get_json("/api/exchange?" + urllib.parse.urlencode({"id": ident}))["requests"]
        assert receipt["published"] is False
        approval = {"id": ident, "request": receipt["id"]}
        assert get_json("/api/exchange/approve", approval)["name"].endswith("result.txt")
        with request("/api/exchange/file?" + urllib.parse.urlencode(approval)) as response:
            assert response.read() == b"hello"
            assert response.headers["Content-Disposition"] == "attachment; filename*=UTF-8''result.txt"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def test_search_is_paid_and_uses_the_input_snapshot(tmp_path):
    request = {"tool": "files.search", "id": 1, "offset": 0, "needle": "dXJjZQ=="}
    source = agent((request, 1, 8))
    # Verify the actual JSON returned in guest memory, not just the paid log.
    expected = b'{"offset":2}'
    checks = " ".join(
        f"(i32.ne (i32.load8_u (i32.const {16 + index})) (i32.const {byte})) (if (then unreachable))"
        for index, byte in enumerate(expected)
    )
    source = source[:-2] + checks + source[-2:]
    run = exercise(tmp_path, source)
    assert run["runs"][0][1] == ("завершён", "")
    search, = [row for row in record_set(run) if row.get("tool") == "files.search"]
    assert search["charge"] == 2
    assert run["laws"]["И35р"][0] != BROKEN
