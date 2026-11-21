"""Облачная модель: два протокола — OpenAI-совместимый и Anthropic.

Устройство — docs/pesochnitsa_specifikatsiya.md, §5. Этот файл живёт в
процессе ведущего (экрана), у которого есть сеть; в процессы исполнителя и
модели он не попадает, и ключ тоже.

Вопрос уходит одним сообщением пользователя, как есть, без системной
подсказки: что спросить, решает агент. Температура 0. Беседы на стороне
поставщика нет — каждый вопрос самостоятелен.

Чего клиент не умеет:
- он не гарантирует, что поставщик не хранит вопросы и не связывает их
  между собой: это обещание поставщика, а не свойство среды;
- ответ облака не обязан быть одинаковым на одинаковый вопрос даже при
  температуре 0;
- байты вопроса, которые не складываются в UTF-8 (агент может спросить
  модель сырым вызовом о любой своей памяти), уходят знаком «�»: облако
  принимает только текст. Уходит при этом не больше, чем записано в
  журнале;
- токены и деньги считает поставщик, клиент лишь показывает его числа.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# Готовые адреса. Имя модели человек пишет сам или берёт из списка
# поставщика (list_models): имена моделей меняются чаще, чем этот файл.
OPENAI_BASE = "https://api.openai.com/v1"
OPENAI_ASTRA = "gpt-6.2-astra"
PRESETS = [
    {"id": "openrouter", "name": "OpenRouter", "kind": "openai", "base": "https://openrouter.ai/api/v1",
     "key_env": "OPENROUTER_API_KEY"},
    {"id": "anthropic", "name": "Anthropic", "kind": "anthropic", "base": "https://api.anthropic.com/v1",
     "key_env": "ANTHROPIC_API_KEY"},
    {"id": "openai", "name": "OpenAI", "kind": "openai", "base": OPENAI_BASE,
     "key_env": "OPENAI_API_KEY"},
    {"id": "deepseek", "name": "DeepSeek", "kind": "openai", "base": "https://api.deepseek.com/v1",
     "key_env": "DEEPSEEK_API_KEY"},
    {"id": "groq", "name": "Groq", "kind": "openai", "base": "https://api.groq.com/openai/v1",
     "key_env": "GROQ_API_KEY"},
    {"id": "mistral", "name": "Mistral", "kind": "openai", "base": "https://api.mistral.ai/v1",
     "key_env": "MISTRAL_API_KEY"},
    {"id": "gemini", "name": "Google Gemini", "kind": "openai",
     "base": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY"},
    {"id": "ollama", "name": "Ollama на этом компьютере", "kind": "openai", "base": "http://127.0.0.1:11434/v1",
     "key_env": ""},
    {"id": "lmstudio", "name": "LM Studio на этом компьютере", "kind": "openai", "base": "http://127.0.0.1:1234/v1",
     "key_env": ""},
    {"id": "other", "name": "Другой (OpenAI-совместимый)", "kind": "openai", "base": "", "key_env": ""},
]

ANTHROPIC_VERSION = "2023-06-01"
RETRY_CODES = (408, 409, 425, 429, 500, 502, 503, 504, 529)


ADDRESS_ERROR = (
    "нужен адрес https:// или локальный http:// без логина, параметров и фрагмента",
    "use an https:// or loopback http:// address without credentials, a query or a fragment",
)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def cloud_destination(base: str) -> tuple[str, str, int, str]:
    """Return the origin and API prefix used for both credential matching and requests."""
    if not base or any(ord(c) <= 32 or ord(c) == 127 or c in "\\?#" for c in base):
        raise ValueError(*ADDRESS_ERROR)
    try:
        url = urllib.parse.urlsplit(base)
        host, port = url.hostname, url.port
    except ValueError as exc:
        raise ValueError(*ADDRESS_ERROR) from exc
    if not host or not re.fullmatch(r"[a-z0-9.:-]+", host):
        raise ValueError(*ADDRESS_ERROR)
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    if url.netloc.lower() != authority or port == 0:
        raise ValueError(*ADDRESS_ERROR)
    if url.scheme != "https" and not (url.scheme == "http" and host in LOOPBACK):
        raise ValueError(*ADDRESS_ERROR)
    return url.scheme, host, port or (443 if url.scheme == "https" else 80), url.path.rstrip("/")


class CloudError(Exception):
    """Облако не ответило. Текст — для человека."""


@dataclass
class Cloud:
    kind: str                      # "openai" | "anthropic"
    base: str                      # адрес без косой черты в конце
    model: str
    key: str = field(default="", repr=False)
    max_tokens: int = 512
    # Подстройка под модели, которые не принимают обычные поля (выясняется
    # первым отказом 400 и запоминается до конца прогона).
    _tokens_field: str = "max_tokens"
    _send_temperature: bool = True

    def __post_init__(self):
        self.base = self.base.strip().rstrip("/")
        cloud_destination(self.base)
        if self.kind not in ("openai", "anthropic"):
            raise ValueError("протокол — openai или anthropic")
        if not isinstance(self.key, str) or (self.key and not (
                self.key.isascii() and self.key.isprintable() and " " not in self.key)):
            raise ValueError("облако: ключ — латинские буквы, цифры и знаки, без пробелов")


def _is_openai_astra(c: Cloud) -> bool:
    return c.kind == "openai" and c.base == OPENAI_BASE and c.model == OPENAI_ASTRA


def _human(code: int, body: str) -> str:
    """Причина для человека — сразу на двух языках экрана."""
    what = {400: "запрос не принят / request rejected", 401: "ключ не принят / key not accepted",
            402: "на счёте у поставщика нет денег / no money on the account",
            403: "доступ запрещён / access denied", 404: "нет такой модели или адреса / no such model or address",
            408: "поставщик не дождался / the provider timed out",
            413: "вопрос слишком длинный для поставщика / the question is too long for the provider",
            429: "поставщик просит подождать / the provider asks to wait",
            500: "ошибка у поставщика / provider error", 502: "ошибка у поставщика / provider error",
            503: "поставщик недоступен / provider unavailable",
            529: "поставщик перегружен / provider overloaded"}.get(code, "ошибка / error")
    short = " ".join(body.split())[:300]
    return f"{what} ({code}){': ' + short if short else ''}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CloudOutcomeUnknown(CloudError):
    """Соединение закончилось до надёжного ответа на уже начатую отправку."""


def _request(c: Cloud, method: str, path: str, body: dict | None, timeout: float, *, one_shot: bool = False) -> dict:
    headers = {"content-type": "application/json", "user-agent": "prostranstvo-sandbox"}
    if c.kind == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if c.key:
            headers["x-api-key"] = c.key
    elif c.key:
        headers["authorization"] = f"Bearer {c.key}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(c.base + path, data=data, method=method, headers=headers)
    # Своя машина (Ollama, LM Studio) — мимо прокси из окружения: прокси
    # 127.0.0.1 пользователя не знает.
    scheme, host, _, _ = cloud_destination(c.base)
    local = scheme == "http" and host in LOOPBACK
    handlers = [urllib.request.ProxyHandler({})] if local else []
    if one_shot or c.key:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    deadline = time.monotonic() + timeout
    with opener.open(req, timeout=max(0.1 if one_shot else 1.0, timeout)) as r:
        if not one_shot:
            return json.loads(r.read().decode("utf-8", "replace"))
        cap = 1024 * 1024
        raw = bytearray()
        while len(raw) <= cap:
            if time.monotonic() >= deadline:
                raise TimeoutError("срок получения ответа истёк")
            chunk = r.read1(min(16384, cap - len(raw) + 1))
            if not chunk:
                return json.loads(raw.decode("utf-8", "replace"))
            raw.extend(chunk)
        raise ValueError("ответ поставщика больше 1 МиБ")


def _body(c: Cloud, question: str) -> tuple[str, dict]:
    msg = [{"role": "user", "content": question}]
    if c.kind == "anthropic":
        b = {"model": c.model, "max_tokens": c.max_tokens, "messages": msg}
        if c._send_temperature:
            b["temperature"] = 0
        return "/messages", b
    if _is_openai_astra(c):
        return "/chat/completions", {"model": c.model, "messages": msg, "max_completion_tokens": c.max_tokens,
                                     "reasoning_effort": "low"}
    b = {"model": c.model, "messages": msg, c._tokens_field: c.max_tokens}
    if c._send_temperature:
        b["temperature"] = 0
    return "/chat/completions", b


def _text(c: Cloud, r: dict) -> tuple[str, dict]:
    """Текст ответа и учёт поставщика. «cut» — поставщик сам сказал, что
    оборвал ответ на пределе токенов. Рассуждающие модели так теряют весь
    ответ: предел уходит на размышление, текст пустой. Признак нужен только
    экрану — ответ модели он не меняет."""
    if c.kind == "anthropic":
        text = "".join(x.get("text", "") for x in r.get("content", []) if x.get("type") == "text")
        u = r.get("usage") or {}
        return text, {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
                      "cut": r.get("stop_reason") == "max_tokens"}
    ch = (r.get("choices") or [{}])[0]
    text = ((ch.get("message") or {}).get("content")) or ""
    if isinstance(text, list):               # некоторые отдают куски
        text = "".join(x.get("text", "") for x in text if isinstance(x, dict))
    u = r.get("usage") or {}
    return text, {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0),
                  "cut": ch.get("finish_reason") == "length"}


def ask(c: Cloud, question: str, deadline: float) -> tuple[str, dict]:
    """Ответ облака на один вопрос. deadline — момент time.monotonic(), к
    которому ответ нужен: повторы при 429 и 5xx — только до него. Не вышло —
    CloudError с причиной для человека."""
    pause, last = 1.0, "не было ни одной попытки / no attempt was made"
    adapted = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0.5:
            raise CloudError(f"срок ответа вышел / timed out; {last}")
        path, body = _body(c, question)
        try:
            return _text(c, _request(c, "POST", path, body, left))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace") if e.fp else ""
            low = text.lower()
            if e.code == 400 and not adapted:
                # Модели с рассуждением у OpenAI просят другое поле для предела
                # и не принимают температуру. Один раз подстроиться — не повтор.
                if c.kind == "openai" and "max_completion_tokens" in low:
                    c._tokens_field, adapted = "max_completion_tokens", True
                    continue
                if "temperature" in low:
                    c._send_temperature, adapted = False, True
                    continue
            last = _human(e.code, text)
            if e.code not in RETRY_CODES:
                raise CloudError(last)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"нет связи / no connection: {getattr(e, 'reason', e)}"
        except (ValueError, KeyError, IndexError) as e:
            raise CloudError(f"непонятный ответ поставщика / unreadable provider answer: {e}")
        time.sleep(min(pause, max(0.0, deadline - time.monotonic() - 0.5)))
        pause = min(pause * 2, 8.0)


def ask_once(c: Cloud, question: str, timeout: float) -> tuple[str, dict]:
    """Одна HTTP-попытка для одного списания, с ограниченным ответом."""
    path, body = _body(c, question)
    try:
        answer, usage = _text(c, _request(c, "POST", path, body, timeout, one_shot=True))
        if not isinstance(answer, str) or not isinstance(usage, dict):
            raise ValueError("ожидался текст ответа модели")
        return answer, usage
    except urllib.error.HTTPError as exc:
        exc.close()
        raise CloudError(_human(exc.code, "")) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudOutcomeUnknown("ответ о приёме запроса не получен") from exc
    except (ValueError, KeyError, IndexError, AttributeError, TypeError) as exc:
        raise CloudError("непонятный или слишком большой ответ поставщика") from exc


def list_models(c: Cloud, timeout: float = 20.0) -> list[str]:
    """Имена моделей у поставщика (GET /models). CloudError — не вышло."""
    try:
        r = _request(c, "GET", "/models", None, timeout)
    except urllib.error.HTTPError as e:
        raise CloudError(_human(e.code, e.read().decode("utf-8", "replace") if e.fp else ""))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CloudError(f"нет связи / no connection: {getattr(e, 'reason', e)}")
    items = r.get("data") or r.get("models") or []
    names = [x.get("id") or x.get("name") for x in items if isinstance(x, dict)]
    return sorted(n for n in names if n)


def check(c: Cloud, timeout: float = 30.0) -> tuple[str, dict]:
    """Проверка связи мимо песочницы: короткий вопрос. Возвращает ответ и
    токены; CloudError — с причиной. В журнал среды не попадает."""
    probe = Cloud(c.kind, c.base, c.model, c.key, max_tokens=512 if _is_openai_astra(c) else 16)
    return ask(probe, "Ответь одним словом: ГОТОВ", time.monotonic() + timeout)
