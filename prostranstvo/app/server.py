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

import hashlib
import hmac
import http.server
import importlib.util
import json
import os
import platform
import secrets
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from app import seal as sealmod
from app.cloud import PRESETS, Cloud, CloudError, check, list_models
from app import texts
from app.runner import (ANSWER_MAX, MODELS, RUNS, WORDS_AGENT_READS, Refusal, Run, agent_module, settings_from,
                        zip_run)
from sandbox.model9 import can_unshare

UI = Path(__file__).resolve().parent / "ui" / "index.html"

KEY_ENVS = sorted({p["key_env"] for p in PRESETS if p["key_env"]} | {"PROSTRANSTVO_KEY"})


class App:
    """Состояние экрана: метка, ключи из окружения, прогон, загрузки."""

    def __init__(self, port: int):
        self.port = port
        self.token = secrets.token_urlsafe(18)
        self.env_keys = {k: os.environ.pop(k) for k in KEY_ENVS if os.environ.get(k)}
        self.run: Run | None = None
        self.agent: tuple[bytes, str] | None = None
        self.agent_error: str | None = None
        self.downloads: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.seal = {"сомкнутый": sealmod.check(False), "разомкнутый": sealmod.check(True)}
        self.unshare = can_unshare()
        try:
            self.agent = agent_module()
        except RuntimeError as e:
            self.agent_error = str(e)

    # ---- сведения для экрана
    def info(self) -> dict:
        MODELS.mkdir(exist_ok=True)
        files = sorted(MODELS.glob("*.gguf"))
        return {
            "platform": f"{platform.system()} {platform.machine()}",
            "seal": self.seal, "unshare": self.unshare,
            "closed_ok": self.seal["сомкнутый"] is None or self.unshare,
            "llama": importlib.util.find_spec("llama_cpp") is not None,
            "models": [{"name": p.name, "path": str(p), "mb": round(p.stat().st_size / 2 ** 20)} for p in files],
            "models_dir": str(MODELS),
            "presets": [{**p, "has_env_key": bool(p["key_env"] and p["key_env"] in self.env_keys)} for p in PRESETS],
            "generic_env_key": "PROSTRANSTVO_KEY" in self.env_keys,
            "scenarios": texts.SCENARIOS, "groups": [{k: g[k] for k in ("id", "ru", "en")} for g in texts.GROUPS],
            "laws": texts.LAWS, "words_bytes": WORDS_AGENT_READS, "answer_max": ANSWER_MAX,
            "agent": self.agent[1] if self.agent else None, "agent_error": self.agent_error,
            "runs_dir": str(RUNS),
        }

    def key_for(self, cloud: dict) -> str:
        """Ключ с экрана, иначе из окружения по готовому адресу, иначе общий."""
        if cloud.get("key"):
            return cloud["key"]
        preset = next((p for p in PRESETS if p["id"] == cloud.get("preset")), None)
        if preset and preset["key_env"] in self.env_keys:
            return self.env_keys[preset["key_env"]]
        return self.env_keys.get("PROSTRANSTVO_KEY", "")

    def cloud_of(self, body: dict) -> Cloud:
        c = body.get("cloud") or {}
        try:
            return Cloud(str(c.get("kind")), str(c.get("base", "")), str(c.get("model", "")), self.key_for(c),
                         int(c.get("max_tokens") or 512))
        except (ValueError, TypeError) as e:
            raise CloudError(str(e))

    # ---- прогон
    def start(self, body: dict) -> dict:
        with self.lock:
            if self.run and not self.run.finished.is_set():
                raise Refusal("прогон уже идёт", "a run is already in progress")
            if not self.agent:
                raise Refusal(self.agent_error or "агент не собран", self.agent_error or "the agent is not built")
            if isinstance(body.get("cloud"), dict):
                body = {**body, "cloud": {**body["cloud"], "key": self.key_for(body["cloud"])}}
            s = settings_from(body)
            self.run = Run(s, self.agent[0])
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
        if not name.lower().endswith(".gguf") or "/" in name or name.startswith("."):
            raise Refusal("ссылка должна вести на файл .gguf", "the link must point to a .gguf file")
        return url, name

    def download(self, url: str) -> str:
        url, name = self.download_target(url)
        with self.lock:
            cur = self.downloads.get(name)
            if cur and cur["state"] == "идёт":
                return name
            if (MODELS / name).exists():
                raise Refusal(f"файл {name} уже есть в папке моделей", f"{name} is already in the models folder")
            self.downloads[name] = {"state": "идёт", "done": 0, "total": 0, "error": None, "sha256": None}
        threading.Thread(target=self._download, args=(url, name), daemon=True).start()
        return name

    def _download(self, url: str, name: str) -> None:
        """Скачивает в папку моделей. Хеш файла считается и показывается, но
        не сверяется: для произвольной ссылки сверять не с чем."""
        st = self.downloads[name]
        MODELS.mkdir(exist_ok=True)
        part = MODELS / (name + ".part")
        h = hashlib.sha256()
        try:
            req = urllib.request.Request(url, headers={"user-agent": "prostranstvo-sandbox"})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                st["total"] = int(r.headers.get("content-length") or 0)
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    st["done"] += len(chunk)
            with open(part, "rb") as f:
                if f.read(4) != b"GGUF":
                    raise RuntimeError("это не файл GGUF / not a GGUF file")
            part.rename(MODELS / name)
            st.update(state="готово", sha256=h.hexdigest())
        except Exception as e:
            part.unlink(missing_ok=True)
            st.update(state="ошибка", error=str(e)[:300])


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
            if n > 64 * 1024:
                raise ValueError("слишком длинный запрос")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

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
                elif u.path == "/api/stop":
                    if app.run:
                        app.run.stop()
                    self._json({"ok": True})
                elif u.path == "/api/cloud/check":
                    text, used = check(app.cloud_of(body))
                    self._json({"ok": True, "answer": text[:200], "tokens": used})
                elif u.path == "/api/cloud/models":
                    self._json({"models": list_models(app.cloud_of(body))[:2000]})
                elif u.path == "/api/download":
                    self._json({"ok": True, "name": app.download(str(body.get("url")))})
                else:
                    self._json({"error": "нет такого адреса"}, 404)
            except CloudError as e:
                self._json({"error": str(e)}, 502)
            except Refusal as e:
                self._json({"error": e.ru, "error_en": e.en}, 400)
            except (ValueError, TypeError) as e:
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
