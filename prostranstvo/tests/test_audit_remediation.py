"""Regression tests for cloud credential binding and verified model admission."""

import hashlib
import http.server
import io
import json
import shutil
import subprocess
import threading
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app import cloud, model_sandbox, runner, server, weights


def test_saved_keys_are_scoped_on_check_and_start(monkeypatch):
    monkeypatch.setattr(server, "agent_module", lambda lang: (b"agent", "fixture"))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-provider")
    monkeypatch.setenv("PROSTRANSTVO_KEY", "fixture-generic")
    monkeypatch.setenv("PROSTRANSTVO_KEY_BASE", "https://custom.example/v2")
    app = server.App(8737)
    options = {"preset": "openai", "kind": "openai", "base": cloud.OPENAI_BASE, "model": "test"}
    payload = {"cloud": options, "contour": "разомкнутый", "words": "fixture"}
    assert app.cloud_of(payload).key == "fixture-provider"
    assert app.cloud_of({"cloud": {**options, "base": "https://API.OPENAI.COM:443/v1/"}}).key == "fixture-provider"
    for change in ({"base": "https://other.example/v1"}, {"base": "https://api.openai.com:444/v1"},
                   {"base": "https://api.openai.com/v2"}, {"kind": "anthropic"}):
        wrong = {**payload, "cloud": {**options, **change}}
        with pytest.raises(runner.Refusal, match="стандартного"):
            app.cloud_of(wrong)
        with pytest.raises(runner.Refusal, match="стандартного"):
            app.start(wrong)
    custom = {**options, "preset": "other", "base": "https://custom.example/v2"}
    assert app.key_for(custom) == "fixture-generic"
    assert not app.key_for(custom, allow_generic=False)
    assert not app.key_for({**custom, "base": "https://other.example/v2"})
    assert app.key_for({**custom, "key": "manually-entered"}) == "manually-entered"


@pytest.mark.parametrize("base", ["http://localhost.example/v1", "http://127.0.0.1.example/v1",
                                 "https://provider.example:bad/v1", "https://user@provider.example/v1",
                                 "https://provider.example/v1?x=y", "https://provider.example/v1#section"])
def test_cloud_requires_a_valid_address(base):
    with pytest.raises(ValueError, match="адрес"):
        cloud.Cloud("openai", base, "test")


@pytest.mark.parametrize("kind, header", [("openai", "Authorization"), ("anthropic", "X-Api-Key")])
def test_keyed_operations_do_not_follow_redirects(kind, header):
    requests = []

    class Provider(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get(header)))
            self.send_response(302)
            self.send_header("Location", "/redirected")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_):
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            client = cloud.Cloud(kind, f"http://127.0.0.1:{httpd.server_port}/v1", "test", "synthetic-key")
            for operation in (cloud.list_models, cloud.check):
                with pytest.raises(cloud.CloudError, match="302"):
                    operation(client, timeout=2)
        finally:
            httpd.shutdown()
            thread.join()
    endpoint = "messages" if kind == "anthropic" else "chat/completions"
    value = "synthetic-key" if kind == "anthropic" else "Bearer synthetic-key"
    assert requests == [("/v1/models", value), (f"/v1/{endpoint}", value)]


def reference(data):
    return weights.Reference(hashlib.sha256(data).hexdigest(), len(data))


def test_gguf_download_publishes_only_matching_content(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MODELS", tmp_path)
    app = server.App.__new__(server.App)
    app.downloads = {"fixture.gguf": {}}
    expected = b"GGUF synthetic data"

    class Response(io.BytesIO):
        headers = {}

    def deliver(data):
        monkeypatch.setattr(server.urllib.request, "urlopen", lambda *args, **kwargs: Response(data))
        app._download("https://example.test/fixture.gguf", "fixture.gguf", reference(expected))

    for wrong in (b"GGUF" + b"x" * (len(expected) - 4), expected + b"x", expected[:-1]):
        deliver(wrong)
        assert app.downloads["fixture.gguf"]["state"] == "ошибка"
        assert not list(tmp_path.iterdir())
    deliver(expected)
    assert app.downloads["fixture.gguf"]["state"] == "готово"
    assert (tmp_path / "fixture.gguf").read_bytes() == expected


def test_run_mounts_verified_copy_and_cleans_up(tmp_path, monkeypatch):
    original = tmp_path / "original.gguf"
    original.write_bytes(b"GGUF synthetic data")
    ref = reference(original.read_bytes())
    monkeypatch.setattr(model_sandbox, "check", lambda: None)
    monkeypatch.setattr(runner.sealmod, "check", lambda _: None)
    staged = []
    launch = []

    def command(path):
        staged.append(path)
        assert path != original
        assert path.parent.stat().st_mode & 0o077 == 0
        assert path.stat().st_mode & 0o777 == 0o400
        original.write_bytes(b"GGUF source changed after copying")
        assert reference(path.read_bytes()) == ref
        return ["fake-bubblewrap", str(path)]

    def driver(*args, model, **kwargs):
        launch.append(model["sandbox_argv"])
        return SimpleNamespace(proc=SimpleNamespace(is_alive=lambda: False), close=lambda: None)

    monkeypatch.setattr(model_sandbox, "command", command)
    monkeypatch.setattr(runner, "SealedDriver", driver)
    monkeypatch.setattr(runner, "run_contours", lambda *args, **kwargs: {})
    monkeypatch.setattr(runner.Run, "_finish", lambda *args: None)
    settings = runner.Settings(contour="сомкнутый", model=str(original), model_integrity=ref)
    run = runner.Run(settings, b"", tmp_path)
    run.run()
    assert launch == [["fake-bubblewrap", str(staged[0])]]
    assert not staged[0].exists()

    refused = runner.Run(settings, b"", tmp_path)
    refused.run()
    assert len(launch) == 1
    assert refused.snapshot()["state"] == "ошибка"
    assert refused.snapshot()["notes"][0]["code"] == "model_integrity"


@pytest.mark.skipif(not shutil.which("openssl"), reason="OpenSSL is required to verify a catalog")
def test_catalog_authenticates_exact_bytes(tmp_path):
    key, pub = tmp_path / "test-key.pem", tmp_path / "test-public.pem"
    catalog, signature = tmp_path / "catalog.json", tmp_path / "catalog.json.sig"
    ref = reference(b"GGUF fixture")
    payload = {"version": 1, "models": [{"name": "fixture.gguf", "version": "1", **asdict(ref)}]}
    catalog.write_text(json.dumps(payload))
    openssl = ["openssl"]
    subprocess.run([*openssl, "genpkey", "-algorithm", "ED25519", "-out", key], check=True)
    subprocess.run([*openssl, "pkey", "-in", key, "-pubout", "-out", pub], check=True)
    subprocess.run([*openssl, "pkeyutl", "-sign", "-inkey", key, "-rawin", "-in", catalog, "-out", signature], check=True)
    assert weights.load_catalog(catalog, pub) == {"fixture.gguf": ref}
    catalog.write_text(json.dumps(payload) + "\n")
    with pytest.raises(ValueError, match="signature verification failed"):
        weights.load_catalog(catalog, pub)
