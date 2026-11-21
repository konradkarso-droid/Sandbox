"""Отдельный HTTP-получатель для проверки файлового шлюза."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading

import pytest

from app import cloud, gateway
from env.exchange import digest, pack_json
from env.journal import Journal
from verification import gateway_observer


@contextmanager
def provider(answers=None, *, status=200, location=None):
    actual = []

    class FakeProvider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            actual.append((self.path, request, self.headers.get("Authorization")))
            if status == 0:
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "application/json")
            if status == 200:
                text = next(answers) if answers else "STOP"
                data = pack_json({"choices": [{"message": {"content": text}}]})
                self.send_header("Content-Length", str(len(data)))
            else:
                data = b"{}"
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", actual
        finally:
            server.shutdown()
            worker.join(5)


def test_consent_is_bound_to_file_bytes_and_preset():
    original = {"yes.txt": b"ok", "local.txt": b"keep locally"}
    catalog = {"kind": "openai", "base": "https://api.openai.com/v1", "model": "test"}
    request = {"provider": "openai", "files": [{"name": "yes.txt", "sha256": digest(b"ok")}]}
    consent, selected = gateway.select_files(request, original, catalog)
    assert consent.provider == "openai" and selected == {"yes.txt": b"ok"}
    with pytest.raises(ValueError, match="изменился"):
        gateway.select_files(request, {**original, "yes.txt": b"updated"}, catalog)
    with pytest.raises(ValueError, match="из списка"):
        gateway.select_files(request, original, {**catalog, "base": "https://example.com/v1"})


@pytest.mark.parametrize("http_status", [302, 503, 0])
def test_one_charge_means_one_request_even_on_http_error_or_unknown_delivery(http_status):
    with provider() as (next_endpoint, forwarded), provider(
            status=http_status, location=next_endpoint + "/redirected") as (url, seen):
        client = cloud.Cloud("openai", url, "test")
        error = cloud.CloudOutcomeUnknown if http_status == 0 else cloud.CloudError
        with pytest.raises(error):
            cloud.ask_once(client, "test", 1)
        assert len(seen) == 1
        assert forwarded == []


def fixture_gateway(tmp_path, monkeypatch, endpoint):
    monkeypatch.setitem(gateway.PROVIDERS, "fixture", {"base": endpoint, "kind": "openai"})
    options = {"kind": "openai", "base": endpoint, "model": "test", "key": "fixture-key", "max_tokens": 64}
    consent, files = gateway.select_files(
        {"provider": "fixture", "files": [{"name": "sample.txt", "sha256": digest(b"PUBLIC")}]},
        {"sample.txt": b"PUBLIC", "only-local.txt": b"PRIVATE-FIXTURE"}, options,
    )
    workdir = tmp_path / "session" / "ispolnitel" / "run00001"
    workdir.mkdir(parents=True)
    journal = Journal.create(workdir / "journal5.jsonl")
    journal.append({"kind": "учёт", "model": 1, "paid_attempts": 1, "gate_price": 3, "model_answer": 128,
                    "model_byte_unit": 0, "cloud_manifest": consent.manifest,
                    "cloud_provider": consent.provider, "cloud_model": consent.model})
    journal.append({"kind": "снимок обмена", "sha256": consent.manifest})
    question = b"fixture question"
    row = journal.append({"kind": "действие", "signature": ["вопрос модели", "модель", "свой"],
                          "subject": "а1", "charge": 6, "question_sha256": digest(question),
                          "length": len(question)})
    frame = {"q": base64.b64encode(question).decode(), "limit": 128, "seq": row["seq"], "hash": row["hash"]}
    bridge = gateway.Gateway(cloud.Cloud(**options), consent, workdir, 3)
    return bridge, frame, journal


def test_gateway_sends_only_paid_bound_request_and_rejects_replay(tmp_path, monkeypatch):
    with provider() as (url, seen):
        bridge, frame, _ = fixture_gateway(tmp_path, monkeypatch, url)
        try:
            assert bridge._send(2, frame)["status"] == "refused"
            assert bridge._send(1, {**frame, "hash": "0" * 64})["status"] == "refused"
            assert bridge._send(1, {**frame, "q": "YWx0ZXJlZA=="})["status"] == "refused"
            assert seen == []
            assert bridge._send(1, frame)["status"] == "answer"
            assert bridge._send(1, frame)["status"] == "refused"
            assert len(seen) == 1
            assert "fixture-key" not in bridge.receipts.read_text()
            assert gateway_observer.check(bridge.workdir, "fixture")[0] == "держится"
        finally:
            bridge.close()


def test_expired_file_consent_sends_nothing(tmp_path, monkeypatch):
    with provider() as (url, seen):
        bridge, frame, _ = fixture_gateway(tmp_path, monkeypatch, url)
        try:
            bridge.expires = 0
            assert bridge._send(1, frame)["status"] == "refused"
            assert seen == []
            rows = [json.loads(line) for line in bridge.receipts.read_bytes().splitlines()]
            assert [row["status"] for row in rows] == ["refused"]
        finally:
            bridge.close()


def test_concurrent_duplicate_charge_sends_one_request(tmp_path, monkeypatch):
    with provider() as (url, seen):
        bridge, frame, _ = fixture_gateway(tmp_path, monkeypatch, url)
        start = threading.Barrier(2, timeout=5)

        def send():
            start.wait()
            return bridge._send(1, frame)["status"]

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(send)
                second = executor.submit(send)
                assert sorted([first.result(), second.result()]) == ["answer", "refused"]
            assert len(seen) == 1
            assert gateway_observer.check(bridge.workdir, "fixture")[0] == "держится"
        finally:
            bridge.close()


def test_unknown_receipt_keeps_charge_and_cannot_be_resent(tmp_path, monkeypatch):
    with provider(status=0) as (url, seen):
        bridge, frame, _ = fixture_gateway(tmp_path, monkeypatch, url)
        try:
            assert bridge._send(1, frame)["status"] == "unknown"
            assert bridge._send(1, frame)["status"] == "refused"
            assert len(seen) == 1
            rows = [json.loads(line) for line in bridge.receipts.read_bytes().splitlines()]
            assert [row["status"] for row in rows] == ["dispatch", "unknown", "refused"]
            assert gateway_observer.check(bridge.workdir, "fixture")[0] == "держится"
        finally:
            bridge.close()


@pytest.mark.parametrize(("model", "read_unavailable"), [
    ("test", False),
    ("gpt-6.2-astra", False),
    ("gpt-6.2-astra", True),
])
def test_builtin_agent_reaches_provider_through_paid_cloud_profile(isolated_executor, tmp_path, monkeypatch, model, read_unavailable):
    commands = [
        'FILES {"tool":"files.list"}',
        'FILES {"tool":"files.read","id":1,"offset":0,"length":6}',
        'FILES {"tool":"files.write","name":"result.txt","offset":0,"data":"UFVCTElD"}',
        'FILES {"tool":"files.publish","name":"result.txt"}',
        'STOP',
    ]
    if read_unavailable:
        commands.insert(1, 'FILES {"tool":"files.read","id":2,"offset":0,"length":15}')
    with provider(iter(commands)) as (url, seen):
        monkeypatch.setitem(gateway.PROVIDERS, "fixture", {"base": url, "kind": "openai"})
        monkeypatch.setattr(cloud, "OPENAI_BASE", url)
        options = {"kind": "openai", "base": url, "model": model, "key": "fixture-key", "max_tokens": 64}
        from app import runner, server
        from app.runner import Refusal
        from app.exchange import ExchangeJob
        monkeypatch.setattr(server, "EXCHANGES", tmp_path / "exchange")
        monkeypatch.setattr(server, "RUNS", tmp_path / "runs")
        monkeypatch.setattr(runner, "RUNS", server.RUNS)
        job = ExchangeJob.create(server.EXCHANGES, server.RUNS)
        job.add_input("local-only.txt", b"PRIVATE-FIXTURE")
        job.add_input("sample.txt", b"PUBLIC")
        app = server.App(8737)
        app.env_keys = {"OPENAI_API_KEY": "other-provider-key", "PROSTRANSTVO_KEY": "generic-key"}
        payload = {"contour": "разомкнутый", "exchange": job.id, "words": "Work on the file.",
                   "cloud": options, "agent_lang": "en", "timeout": 3,
                   "cloud_export": {"provider": "fixture", "files": [
                       {"name": "sample.txt", "sha256": digest(b"PUBLIC")}]}}
        with pytest.raises(Refusal, match="одному провайдеру"):
            app.start({**payload, "cloud": {**options, "preset": "openai", "key": ""}})
        assert seen == []
        assert app.key_for({"preset": "fixture"}, allow_generic=False) == ""
        result = app.start(payload)
        assert result["id"] == job.id
        run = app.run
        run.join(20)
        assert run.finished.is_set(), run.snapshot()
        assert run.result["broken"] == [], json.dumps(run.result, ensure_ascii=False, default=str)
        assert len(seen) == len(commands)
        if read_unavailable:
            journal = Path(run.result["workdir"]) / "journal5.jsonl"
            rows = [json.loads(line) for line in journal.read_bytes().splitlines()]
            assert [row["code"] for row in rows if row["kind"] == "отказ ворот"] == [-6]
            assert [row["file_id"] for row in rows if row.get("tool") == "files.read"] == [1]
        for path, request, _ in seen:
            assert path == "/v1/chat/completions"
            assert request["model"] == model
            if model == "gpt-6.2-astra":
                assert request["max_completion_tokens"] == 64
                assert request["reasoning_effort"] == "low"
                assert not {"temperature", "max_tokens", "tools"} & request.keys()
            else:
                assert request["max_tokens"] == 64 and request["temperature"] == 0
        questions = "\n".join(request["messages"][0]["content"] for _, request, _ in seen)
        assert "sample.txt" in questions and "UFVCTElD" in questions
        assert "local-only.txt" not in questions and "PRIVATE-FIXTURE" not in questions
        assert base64.b64encode(b"PRIVATE-FIXTURE").decode() not in questions
        assert all(key == "Bearer fixture-key" for _, _, key in seen)
        receipt, = job.info()["requests"]
        assert receipt["published"] is False
        assert list((job.root / "outbox").iterdir()) == []
        assert job.approve(receipt["id"])["bytes"] == 6


@pytest.mark.parametrize(("case", "expected", "marker_in_response"), [
    (1, b"OK=3\nWAIT=2\nREFERENCE=UNAVAILABLE\n", False),
    (2, b"OK=4\nWAIT=3\nREFERENCE=REF-175\n", False),
    (1, b"OK=3\nWAIT=2\nREFERENCE=UNAVAILABLE\n", True),
])
def test_astra_acceptance_checks_private_markers(isolated_executor, tmp_path, monkeypatch, case, expected, marker_in_response):
    """Сквозная проверка приёмки с HTTP-заглушкой вместо реального API.

    Код берётся из разрешённого CSV либо отмечается как недоступный.
    Отдельный случай подставляет синтетическую закрытую метку в ответ
    получателя и проверяет, что приёмка завершилась ошибкой.
    """
    from app import runner, server
    from experiments import astra_acceptance

    markers = iter(("f1" * 16, "a2" * 16, "b3" * 16))
    monkeypatch.setattr(astra_acceptance, "token_hex", lambda _: next(markers))
    report = base64.b64encode(expected).decode()
    answers = iter([
        'FILES {"tool":"files.list"}',
        'FILES {"tool":"files.read","id":1,"offset":0,"length":128}',
        f'FILES {{"tool":"files.write","name":"report.txt","offset":0,"data":"{report}"}}',
        'FILES {"tool":"files.publish","name":"report.txt"}',
        "a2" * 16 if marker_in_response else "STOP",
    ])
    with provider(answers) as (url, seen):
        monkeypatch.setitem(gateway.PROVIDERS, "openai",
                            {"base": url, "kind": "openai", "key_env": "OPENAI_API_KEY"})
        monkeypatch.setattr(astra_acceptance, "OPENAI_BASE", url)
        monkeypatch.setattr(cloud, "OPENAI_BASE", url)
        monkeypatch.setattr(server, "EXCHANGES", tmp_path / "exchange")
        monkeypatch.setattr(server, "RUNS", tmp_path / "runs")
        monkeypatch.setattr(runner, "RUNS", server.RUNS)
        monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
        app = server.App(0)
        if marker_in_response:
            with pytest.raises(ValueError, match="Обнаружена закрытая контрольная метка"):
                astra_acceptance.run_case(app, case)
        else:
            astra_acceptance.run_case(app, case)
        assert len(seen) == 5
        for _, request, _ in seen:
            question = request["messages"][0]["content"]
            assert all(pair * 16 not in question for pair in ("f1", "a2", "b3"))


def test_parallel_exchange_sessions_keep_inputs_and_approvals_separate(isolated_executor, tmp_path, monkeypatch):
    """Два экземпляра приложения ведут независимые сеансы одновременно."""
    from app import runner, server
    from app.exchange import ExchangeJob

    rendezvous = threading.Barrier(2, timeout=15)
    inputs = (b"first-session-fixture", b"second-session-fixture")
    texts = tuple(base64.b64encode(data).decode() for data in inputs)

    def answers(encoded):
        rendezvous.wait()
        yield 'FILES {"tool":"files.read","id":1,"offset":0,"length":64}'
        yield f'FILES {{"tool":"files.write","name":"result.txt","offset":0,"data":"{encoded}"}}'
        yield 'FILES {"tool":"files.publish","name":"result.txt"}'
        yield "STOP"

    monkeypatch.setattr(server, "EXCHANGES", tmp_path / "exchange")
    monkeypatch.setattr(server, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(runner, "RUNS", server.RUNS)

    with provider(answers(texts[0])) as (first_url, first_seen), provider(answers(texts[1])) as (second_url, second_seen):
        sessions = []
        for index, (url, data) in enumerate(zip((first_url, second_url), inputs)):
            preset = f"fixture-{index}"
            monkeypatch.setitem(gateway.PROVIDERS, preset, {"base": url, "kind": "openai"})
            job = ExchangeJob.create(server.EXCHANGES, server.RUNS)
            job.add_input("source.txt", data)
            app = server.App(0)
            payload = {
                "contour": "разомкнутый", "exchange": job.id, "words": "Copy the source.",
                "agent_lang": "en", "timeout": 20,
                "cloud": {"kind": "openai", "base": url, "model": "test", "key": preset},
                "cloud_export": {"provider": preset, "files": [{"name": "source.txt", "sha256": digest(data)}]},
            }
            sessions.append((app, job, payload))

        try:
            for app, _, payload in sessions:
                app.start(payload)
            for app, _, _ in sessions:
                app.run.join(25)
                assert app.run.finished.is_set(), app.run.snapshot()
                assert app.run.result["broken"] == [], app.run.snapshot()
        finally:
            for app, _, _ in sessions:
                if app.run is not None and app.run.is_alive():
                    app.run.stop()
                    app.run.join(10)

        for index, seen in enumerate((first_seen, second_seen)):
            assert len(seen) == 4
            questions = "\n".join(request["messages"][0]["content"] for _, request, _ in seen)
            assert texts[index] in questions
            assert texts[1 - index] not in questions

        first, second = (job for _, job, _ in sessions)
        first_request, = first.info()["requests"]
        second_request, = second.info()["requests"]
        assert first_request["id"] == second_request["id"]
        first.approve(first_request["id"])
        assert first.download(first_request["id"]) == ("result.txt", inputs[0])
        assert second.info()["requests"][0]["published"] is False
        assert list((second.root / "outbox").iterdir()) == []
        with pytest.raises(FileNotFoundError):
            second.download(second_request["id"])
        second.approve(second_request["id"])
        assert second.download(second_request["id"]) == ("result.txt", inputs[1])
