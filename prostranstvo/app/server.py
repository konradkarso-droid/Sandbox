"""Экран песочницы: местный HTTP-сервер и одна страница (app/ui/index.html).

Устройство — docs/pesochnitsa_specifikatsiya.md, §6. Сервер — недоверенный
ведущий: он передаёт исполнителю настройки и слова и показывает журнал;
законы проверяет наблюдатель по файлам.

Экран — для одного человека на своём компьютере:
- слушает только 127.0.0.1;
- каждый запрос к /api несёт метку, выданную при запуске (она в ссылке,
  которую печатает start.py), и имя хоста проверяется: чужая вкладка
  браузера не запустит прогон и не узнает, какие ключи заданы;
- ключи облака из переменных окружения забираются при запуске сервера и
  из окружения удаляются: процессы исполнителя и модели их не наследуют.
  Ключ с экрана живёт в памяти до конца прогона и никуда не пишется.

Чего сервер не умеет: работать для нескольких людей и по сети; один
прогон за раз.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import importlib.util
import json
import os
import platform
import secrets
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path

from app import seal as sealmod
from app.cloud import PRESETS, Cloud, CloudError, check, cloud_destination, list_models
from app import model_sandbox, texts, weights
from app.exchange import ExchangeJob
from app.gateway import PROVIDERS, select_files
from env.exchange import MAX_FILE_BYTES, snapshot_inbox
from app.runner import (AGENTS, ANSWER_MAX, EXCHANGES, MODELS, OWN_MAX, RUNS, WORDS_AGENT_READS, Refusal, Run, agent_module,
                        own_agent_check, own_agent_source, settings_from, zip_run)
from sandbox.model9 import can_unshare

UI = Path(__file__).resolve().parent / "ui" / "index.html"

KEY_ENVS = sorted({p["key_env"] for p in PRESETS if p["key_env"]} | {"PROSTRANSTVO_KEY"})


class App:
    """Состояние экрана: метка, ключи из окружения, прогон, загрузки."""

    def __init__(self, port: int):
        self.port = port
        self.token = secrets.token_urlsafe(18)
        self.env_keys = {k: os.environ.pop(k) for k in KEY_ENVS if os.environ.get(k)}
        self.generic_key_base = os.environ.pop("PROSTRANSTVO_KEY_BASE", "").strip()
        try:
            cloud_destination(self.generic_key_base)
        except ValueError:
            self.generic_key_base = ""
        self.catalog = {}
        catalog = os.environ.get("PROSTRANSTVO_GGUF_CATALOG")
        catalog_key = os.environ.get("PROSTRANSTVO_GGUF_CATALOG_KEY")
        if catalog or catalog_key:
            if not catalog or not catalog_key:
                raise ValueError("configure both PROSTRANSTVO_GGUF_CATALOG and PROSTRANSTVO_GGUF_CATALOG_KEY")
            self.catalog = weights.load_catalog(Path(catalog), Path(catalog_key))
        self.run: Run | None = None
        self.agent: tuple[bytes, str] | None = None     # встроенный агент, правила по-русски
        self.agent_en: bytes | None = None             # он же, правила по-английски
        self.agent_error: str | None = None
        self.downloads: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.seal = {"сомкнутый": sealmod.check(False), "разомкнутый": sealmod.check(True)}
        self.unshare = can_unshare()
        self.model_sandbox_error = model_sandbox.check()
        try:
            self.agent = agent_module("ru")
            self.agent_en = agent_module("en")[0]
        except RuntimeError as e:
            self.agent_error = str(e)

    # ---- сведения для экрана
    def info(self) -> dict:
        MODELS.mkdir(exist_ok=True)
        files = sorted(MODELS.glob("*.gguf"))
        return {
            "platform": f"{platform.system()} {platform.machine()}",
            "seal": self.seal, "unshare": self.unshare,
            "closed_ok": self.model_sandbox_error is None,
            "exchange_wasm_ok": self.seal["сомкнутый"] is None,
            "model_sandbox_error": self.model_sandbox_error,
            "llama": importlib.util.find_spec("llama_cpp") is not None,
            "models": [{"name": p.name, "path": str(p), "mb": round(p.stat().st_size / 2 ** 20),
                        "has_reference": self.downloads.get(p.name, {}).get("state") == "готово"} for p in files],
            "models_dir": str(MODELS),
            "gguf_catalog": sorted(self.catalog),
            "presets": [{**p, "has_env_key": bool(p["key_env"] and p["key_env"] in self.env_keys)} for p in PRESETS],
            "generic_key_base": self.generic_key_base if "PROSTRANSTVO_KEY" in self.env_keys else "",
            "scenarios": texts.SCENARIOS, "groups": [{k: g[k] for k in ("id", "ru", "en")} for g in texts.GROUPS],
            "laws": texts.LAWS, "words_bytes": WORDS_AGENT_READS, "answer_max": ANSWER_MAX,
            "agent": self.agent[1] if self.agent else None, "agent_error": self.agent_error,
            "own_agents": self.own_agents(), "own_max": OWN_MAX,
            "runs_dir": str(RUNS),
        }

    @staticmethod
    def own_agents() -> list[dict]:
        AGENTS.mkdir(exist_ok=True)
        return [{"name": p.name, "kb": max(1, round(p.stat().st_size / 1024))}
                for p in sorted(AGENTS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                if p.suffix in (".wasm", ".wat")]

    def upload_agent(self, body: dict) -> dict:
        """Свой агент с экрана: проверить и положить в папку agents. Имя —
        начало хеша и расширение: одинаковый модуль не множится, а чужое имя
        файла не попадает в путь."""
        import base64
        try:
            data = base64.b64decode(str(body.get("data") or ""), validate=True)
        except ValueError:
            raise Refusal("файл не прочитался", "the file could not be read")
        chk = own_agent_check(data)
        if not chk["ok"]:
            raise Refusal(chk["ru"], chk["en"])
        ext = ".wasm" if data[:4] == b"\0asm" else ".wat"
        stem = "".join(c for c in str(body.get("name") or "agent").rsplit(".", 1)[0] if c.isalnum() or c in "-_")[:40]
        name = f"{stem or 'agent'}_{hashlib.sha256(data).hexdigest()[:8]}{ext}"
        AGENTS.mkdir(exist_ok=True)
        (AGENTS / name).write_bytes(data)
        return {"name": name, **chk}

    def key_for(self, cloud: dict, *, allow_generic: bool = True) -> str:
        """Select saved credentials only for their configured API endpoint."""
        provider = cloud.get("preset")
        if allow_generic:
            preset = next((p for p in PRESETS if p["id"] == provider), None)
        else:
            preset = PROVIDERS.get(provider) if isinstance(provider, str) else None
        typed_key = cloud.get("key") or ""
        saved_key = self.env_keys.get(preset.get("key_env", ""), "") if preset else ""
        if preset and preset["base"] and (typed_key or saved_key):
            base = str(cloud.get("base", "")).strip()
            if (cloud_destination(base) != cloud_destination(preset["base"])
                    or cloud.get("kind") != preset["kind"]):
                raise Refusal("ключ провайдера предназначен для его стандартного адреса и протокола",
                              "the provider key is scoped to its preset address and protocol")
        if typed_key or saved_key:
            return typed_key or saved_key
        if (allow_generic and self.generic_key_base and
                cloud_destination(str(cloud.get("base", "")).strip()) == cloud_destination(self.generic_key_base)):
            return self.env_keys.get("PROSTRANSTVO_KEY", "")
        return ""

    def cloud_of(self, body: dict) -> Cloud:
        c = body.get("cloud") or {}
        if not isinstance(c, dict):
            raise Refusal("неверные настройки облака", "invalid cloud settings")
        try:
            return Cloud(str(c.get("kind")), str(c.get("base", "")), str(c.get("model", "")), self.key_for(c),
                         int(c.get("max_tokens") or 512))
        except Refusal:
            raise
        except (ValueError, TypeError) as e:
            raise Refusal(str(e.args[0]), str(e.args[-1])) from e

    # ---- прогон
    def start(self, body: dict) -> dict:
        with self.lock:
            if self.run and not self.run.finished.is_set():
                raise Refusal("прогон уже идёт", "a run is already in progress")
            if not self.agent:
                raise Refusal(self.agent_error or "агент не собран", self.agent_error or "the agent is not built")
            cloud = body.get("cloud")
            if isinstance(cloud, dict) and body.get("contour") == "разомкнутый":
                scoped = "exchange" in body
                if scoped and isinstance(export := body.get("cloud_export"), dict):
                    provider = export.get("provider")
                    if cloud.get("preset", provider) != provider:
                        raise Refusal("ключ и разрешение должны относиться к одному провайдеру",
                                      "the key preset must match the approved provider")
                    cloud = {**cloud, "preset": provider}
                body = {**body, "cloud": {**cloud, "key": self.key_for(cloud, allow_generic=not scoped)}}
            if body.get("contour") == "сомкнутый" and isinstance(body.get("model"), str):
                p = Path(body["model"].strip()).expanduser()
                name = p.name
                if name in self.catalog:
                    body = {**body, "model_integrity": asdict(self.catalog[name])}
                elif body.get("model_integrity") is None and p.parent in (Path("."), MODELS):
                    download = self.downloads.get(name, {})
                    if download.get("state") == "готово":
                        body = {**body, "model_integrity": download["integrity"]}
            s = settings_from(body)
            files = consent = None
            if s.exchange:
                job = ExchangeJob(EXCHANGES, RUNS, s.exchange)
                if job.run.exists():
                    raise Refusal("сеанс обмена уже запускался", "exchange session has already been run")
                try:
                    files = snapshot_inbox(job.root / "inbox")
                    if s.contour == "разомкнутый":
                        consent, files = select_files(body.get("cloud_export"), files, s.cloud)
                except (ValueError, OSError) as exc:
                    raise Refusal(f"вход обмена: {exc}", f"exchange inbox: {exc}") from exc
            if s.agent != "встроенный":
                module = own_agent_source(s.agent)
            elif files is not None:
                module = agent_module(s.agent_lang, files=True)[0]
            else:
                module = self.agent_en if s.agent_lang == "en" else self.agent[0]
            self.run = Run(s, module, files=files, consent=consent)
            self.run.start()
            return {"id": self.run.stamp}

    # ---- загрузка модели по ссылке
    @staticmethod
    def download_target(url: str) -> tuple[str, str]:
        """Ссылка на файл GGUF → (ссылка для скачивания, имя файла). Ссылку
        Hugging Face на страницу файла («/blob/») превращает в ссылку на сам
        файл («/resolve/»)."""
        url = str(url or "").strip()
        if not url.startswith("https://"):
            raise Refusal("ссылка должна начинаться с https://", "the link must start with https://")
        url = url.replace("/blob/", "/resolve/", 1)
        name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
        if (not name.lower().endswith(".gguf") or "/" in name or "\\" in name or name.startswith(".")
                or any(ord(c) < 32 for c in name)):
            raise Refusal("ссылка должна вести на файл .gguf", "the link must point to a .gguf file")
        return url, name

    def download(self, url: str, metadata: object = None) -> str:
        url, name = self.download_target(url)
        reference = self.catalog.get(name) or weights.Reference.parse(metadata)
        integrity = asdict(reference)
        with self.lock:
            cur = self.downloads.get(name)
            if cur and cur["state"] == "идёт":
                if cur["integrity"] != integrity:
                    raise Refusal("файл уже скачивается с другим эталоном",
                                  "the file is already downloading with a different reference")
                return name
            if (MODELS / name).exists():
                raise Refusal(f"файл {name} уже есть в папке моделей", f"{name} is already in the models folder")
            self.downloads[name] = {"state": "идёт", "done": 0, "total": reference.size, "error": None,
                                    "error_en": None, "sha256": None, "integrity": integrity}
        threading.Thread(target=self._download, args=(url, name, reference), daemon=True).start()
        return name

    def _download(self, url: str, name: str, reference: weights.Reference) -> None:
        st = self.downloads[name]
        try:
            MODELS.mkdir(exist_ok=True)
            req = urllib.request.Request(url, headers={"user-agent": "prostranstvo-sandbox"})
            with tempfile.TemporaryDirectory(prefix=".gguf-", dir=MODELS) as staging:
                target = Path(staging) / "weights.gguf"
                with urllib.request.urlopen(req, timeout=30) as response:
                    length = response.headers.get("content-length")
                    if length is not None and int(length) != reference.size:
                        raise weights.IntegrityError("размер GGUF не совпадает с эталоном", "GGUF size does not match")
                    weights.copy_verified(response.read1, target, reference,
                                          progress=lambda done: st.update(done=done))
                os.link(target, MODELS / name)
            st.update(state="готово", sha256=reference.sha256)
        except weights.IntegrityError as exc:
            st.update(state="ошибка", error=exc.args[0], error_en=exc.args[-1])
        except (OSError, ValueError) as exc:
            st.update(state="ошибка", error=str(exc)[:300])


def make_handler(app: App):
    allowed_hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "prostranstvo"

        def log_message(self, fmt, *args):          # тишина в терминале
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("referrer-policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

        def _allowed(self, query: dict) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                return False
            token = self.headers.get("X-Token") or (query.get("t") or [""])[0]
            return hmac.compare_digest(token.encode(), app.token.encode())

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            limit = 64 * 1024
            if self.path == "/api/exchange/upload":
                limit = 2 * MAX_FILE_BYTES
            elif self.path.startswith("/api/agent"):
                limit = 2 * OWN_MAX
            if not 0 <= n <= limit:
                raise ValueError("слишком длинный запрос")
            raw = self.rfile.read(n) if n else b"{}"
            value = json.loads(raw or b"{}")
            if not isinstance(value, dict):
                raise ValueError("ожидается объект JSON")
            return value

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            if not self._allowed(q):
                self._send(403, "Открой ссылку, которую напечатал start.py в терминале.\n"
                                "Open the link that start.py printed in the terminal.".encode(),
                           "text/plain; charset=utf-8")
                return
            if u.path == "/":
                page = UI.read_text(encoding="utf-8").replace("__TOKEN__", app.token)
                self._send(200, page.encode(), "text/html; charset=utf-8",
                           {"content-security-policy": "default-src 'self'; script-src 'unsafe-inline'; "
                                                       "style-src 'unsafe-inline'; img-src 'self' data:"})
            elif u.path == "/api/info":
                self._json(app.info())
            elif u.path == "/api/run":
                since = int((q.get("since") or ["0"])[0] or 0)
                self._json(app.run.snapshot(since) if app.run else {"state": "нет"})
            elif u.path == "/api/downloads":
                self._json(app.downloads)
            elif u.path in ("/api/exchange", "/api/exchange/file"):
                try:
                    job = ExchangeJob(EXCHANGES, RUNS, (q.get("id") or [""])[0])
                    if u.path == "/api/exchange":
                        self._json(job.info())
                    else:
                        name, data = job.download(int((q.get("request") or [""])[0]))
                        filename = urllib.parse.quote(name, safe="")
                        self._send(200, data, "application/octet-stream",
                                   {"content-disposition": f"attachment; filename*=UTF-8''{filename}"})
                except (ValueError, OSError) as exc:
                    self._json({"error": str(exc)}, 400)
            elif u.path.startswith("/api/zip/"):
                try:
                    p = zip_run(u.path.rsplit("/", 1)[1])
                except ValueError as e:
                    self._json({"error": str(e)}, 404)
                    return
                self._send(200, p.read_bytes(), "application/zip",
                           {"content-disposition": f'attachment; filename="{p.name}"'})
            else:
                self._json({"error": "нет такого адреса"}, 404)

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            if not self._allowed({}):
                self._json({"error": "нет метки"}, 403)
                return
            try:
                body = self._body()
                if u.path == "/api/start":
                    self._json(app.start(body))
                elif u.path == "/api/exchange/new":
                    self._json(ExchangeJob.create(EXCHANGES, RUNS).info())
                elif u.path in ("/api/exchange/upload", "/api/exchange/approve"):
                    with app.lock:
                        job = ExchangeJob(EXCHANGES, RUNS, body.get("id"))
                        if u.path == "/api/exchange/upload":
                            data = base64.b64decode(body.get("data", ""), validate=True)
                            job.add_input(body.get("name"), data)
                            self._json({"ok": True})
                        else:
                            self._json(job.approve(body.get("request")))
                elif u.path == "/api/stop":
                    if app.run:
                        app.run.stop()
                    self._json({"ok": True})
                elif u.path == "/api/cloud/check":
                    text, used = check(app.cloud_of(body))
                    self._json({"ok": True, "answer": text[:200], "tokens": used})
                elif u.path == "/api/cloud/models":
                    self._json({"models": list_models(app.cloud_of(body))[:2000]})
                elif u.path == "/api/agent/upload":
                    self._json(app.upload_agent(body))
                elif u.path == "/api/download":
                    self._json({"ok": True, "name": app.download(str(body.get("url")), body.get("model_integrity"))})
                else:
                    self._json({"error": "нет такого адреса"}, 404)
            except CloudError as e:
                self._json({"error": str(e)}, 502)
            except weights.IntegrityError as e:
                self._json({"error": e.args[0], "error_en": e.args[-1]}, 400)
            except Refusal as e:
                self._json({"error": e.ru, "error_en": e.en}, 400)
            except (ValueError, TypeError, OSError, RuntimeError) as e:
                self._json({"error": str(e)}, 400)

    return Handler


def serve(port: int = 8737, open_browser: bool = True) -> None:
    app = App(port)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{port}/?t={app.token}"
    print("\nПространство — песочница для ИИ-агентов / Prostranstvo — a sandbox for AI agents")
    print(f"Экран / Screen: {url}")
    if app.agent_error:
        print(f"Внимание: {app.agent_error}")
    print("Остановить / Stop: Ctrl+C\n", flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.run:
            app.run.stop()
        srv.server_close()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8737)
