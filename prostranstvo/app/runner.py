"""Один прогон песочницы для экрана: настройки → исполнитель → журнал → законы.

Устройство — docs/pesochnitsa_specifikatsiya.md. Здесь нет своей среды:
исполнитель — `sandbox.process5` (первая реализация), агент — `agent9`,
запуск контуров — `sandbox.run_session.run_contours`, законы — `verification.answer_observer.check`.
Здесь настраиваются изоляция локальной модели, передатчик для облака,
чтение журнала по ходу прогона и сводка итогов.

Экран двуязычный, поэтому отсюда уходят данные, а не фразы: события
журнала — полями записи, заметки и отказы — кодом с параметрами (отказ
настроек — сразу двумя текстами, `Refusal`). Слова подставляет экран.

Ведущий недоверенный, как всякий ведущий с шага 5: что бы он ни показывал,
законы проверяет наблюдатель по файлам, а не по его счётчикам. Счётчики на
экране (остаток бюджета, вопросы, байты) — для человека; правило расхода
у них то же, что у закона И1р (`observer5b.cost`).

Чего прогон не умеет:
- один прогон за раз (исполнитель один на экран);
- остановленный прогон законами не проверяется: выгрузки исполнителя нет,
  есть только журнал до остановки;
- свой агент — один модуль на прогон; его ответы модели экран не
  разбирает (формат задаёт сам агент).
"""

from __future__ import annotations

import functools
import importlib.util
import json
import threading
import time
import traceback
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from importlib.metadata import version
from pathlib import Path

from app import seal as sealmod
from app import model_sandbox, texts
from app.bridge import Bridge, question_mark
from app.cloud import Cloud
from app.exchange import valid_session
from app.gateway import Consent, Gateway
from core.model_world import W6
from env.exchange import digest, input_manifest, pack_json, put_private
from experiments.agents9 import build as agent_build
from sandbox.agent_protocol import CFG, first_line, move_of
from sandbox.model9 import can_unshare
from sandbox.process5 import Driver
from sandbox.run_session import run_contours
from verification import answer_observer, exchange_observer, observer5b
from verification.observer5 import BROKEN, HOLDS, NA, read_journal

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
RUNS = ROOT / "runs"
EXCHANGES = ROOT / "exchange"
AGENTS = ROOT / "agents"          # загруженные свои агенты (спецификация §12)
OWN_MAX = 1 << 20                 # байт: предел своего модуля — объявленное число, не подобранное
OWN_IMPORTS = {("env", "call"), ("env", "spawn")}
# Проверка установки (`start.py --check`): записанные заранее ходы вместо
# модели, среди них попытки выхода. На экране её нет — это прибор для
# установки и тестов, а не модель.
INSTALL_CHECK = "__проверка_установки__"
INSTALL_SCRIPT = ROOT / "app" / "install_check.json"
# Второй контур, когда второй агент не ходит: модуль без единого вызова.
# Соседний контур при этом есть — закон И22р проверяет, что к нему ничего
# не просочилось.
IDLE = '(module (func (export "main")))'
WORDS_AGENT_READS = 600          # байт: столько слов человека читает agent9 (ЧИТАЙ ВХОД, 600)
ANSWER_MAX = 1024                # байт: место под ответ после вопроса в памяти agent9 (AMAX)
TURNS_MAX = 60                   # ходов у agent9 (MAX_TURNS)
EFFECT_FIELDS = ("rule", "obj", "code", "cap", "op", "task", "part", "payer", "receiver", "price", "charge",
                 "node", "first", "why")


class Refusal(ValueError):
    """Отказ настроек — сразу на двух языках экрана."""

    def __init__(self, ru: str, en: str):
        super().__init__(ru)
        self.ru, self.en = ru, en


# ---------------------------------------------------------------- агент

def agent_module(lang: str = "ru", files: bool = False) -> tuple[bytes, str]:
    """Модуль agent9 с правилами на языке lang: clang, если есть с целью
    wasm32, иначе zig из pip."""
    errors = []
    for compiler in ("clang", "zig"):
        if compiler == "zig" and importlib.util.find_spec("ziglang") is None:
            errors.append("zig: пакет ziglang не установлен")
            continue
        try:
            module = agent_build.build(compiler, lang, files=True) if files else agent_build.build(compiler, lang)
            return module, compiler
        except RuntimeError as e:
            errors.append(f"{compiler}: {str(e).splitlines()[0][:200]}")
    raise RuntimeError("агента не собрать: " + "; ".join(errors))


def own_agent_check(data: bytes) -> dict:
    """Проверка своего модуля до прогона: подсказка человеку, а не закон —
    загрузит модуль или откажет исполнитель (закон И23р). Модуль — байты
    WebAssembly или текст WAT. Возвращает {"ok": True, "imports", "exports"}
    или {"ok": False, "ru", "en"} с причиной."""
    import wasmtime
    if len(data) > OWN_MAX:
        return {"ok": False, "ru": f"модуль больше {OWN_MAX // 1024} КБ", "en": f"the module is over {OWN_MAX // 1024} KB"}
    try:
        source = data if data[:4] == b"\0asm" else data.decode("utf-8")
        m = wasmtime.Module(wasmtime.Engine(), source)
    except Exception as e:
        why = str(e).splitlines()[0][:200]
        return {"ok": False, "ru": f"это не WebAssembly: {why}", "en": f"not WebAssembly: {why}"}
    imports = sorted({(i.module, i.name) for i in m.imports})
    extra = [f"{a}.{b}" for a, b in imports if (a, b) not in OWN_IMPORTS]
    if extra:
        return {"ok": False, "ru": "чужие импорты: " + ", ".join(extra) + " — можно только env.call и env.spawn",
                "en": "foreign imports: " + ", ".join(extra) + " — only env.call and env.spawn are allowed"}
    exports = {e.name: e.type for e in m.exports}
    if not isinstance(exports.get("main"), wasmtime.FuncType):
        return {"ok": False, "ru": "нет функции main", "en": "no main function"}
    return {"ok": True, "imports": [f"{a}.{b}" for a, b in imports], "exports": sorted(exports)}


def own_agent_source(name: str) -> bytes | str:
    """Модуль своего агента для исполнителя: байты WebAssembly или текст WAT."""
    data = own_agent_path(name).read_bytes()
    return data if data[:4] == b"\0asm" else data.decode("utf-8")


def own_agent_path(name: str) -> Path:
    """Файл своего агента: только имя файла из папки agents."""
    name = str(name or "")
    p = AGENTS / name
    if Path(name).name != name or p.suffix not in (".wasm", ".wat") or not p.is_file():
        raise Refusal(f"нет такого агента: {name}", f"no such agent: {name}")
    return p


# ---------------------------------------------------------------- настройки

@dataclass
class Settings:
    contour: str                       # «сомкнутый» (модель-файл здесь) | «разомкнутый» (облако или сервис)
    model: str = ""                    # сомкнутый: путь к файлу GGUF
    cloud: dict | None = None          # разомкнутый: kind, base, model, key, max_tokens
    words: str = ""
    budget: int = 625
    answer: int = ANSWER_MAX
    timeout: float = 0.0               # срок ответа модели, с; 0 — по умолчанию для контура
    second: str = "молчит"             # «молчит» | «такой же»
    seed: int = 1
    agent: str = "встроенный"          # «встроенный» (agent9) или имя файла в папке agents
    exchange: str | None = None
    agent_lang: str = "ru"             # язык правил встроенного агента: «ru» | «en»

    def public(self) -> dict:
        """Настройки без ключа — для файлов прогона и экрана."""
        d = asdict(self)
        if d.get("cloud"):
            d["cloud"] = {k: v for k, v in d["cloud"].items() if k != "key"}
        return d


def _int(raw: dict, name: str, lo: int, hi: int, default: int, ru: str, en: str) -> int:
    v = raw.get(name, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi or int(v) != v:
        raise Refusal(f"{ru}: целое от {lo} до {hi}", f"{en}: a whole number from {lo} to {hi}")
    return int(v)


def model_path(name: str) -> Path:
    """Файл модели: имя файла в папке models или полный путь к файлу .gguf."""
    p = Path(str(name).strip()).expanduser()
    p = p if p.is_absolute() else MODELS / p.name
    if p.suffix.lower() != ".gguf":
        raise Refusal(f"файл модели должен быть .gguf: {p}", f"the model file must be .gguf: {p}")
    if not p.is_file():
        raise Refusal(f"нет такого файла: {p}", f"no such file: {p}")
    return p


def settings_from(raw: dict) -> Settings:
    """Настройки с экрана. Refusal — с причиной для человека."""
    if not isinstance(raw, dict):
        raise Refusal("настройки — словарь", "settings must be an object")
    contour = raw.get("contour")
    if contour not in ("сомкнутый", "разомкнутый"):
        raise Refusal("модель — локальная или облачная", "the model is either local or cloud")
    words = raw.get("words", "")
    if not isinstance(words, str) or not words.strip():
        raise Refusal("напиши агенту задание", "write a task for the agent")
    if len(words) > 4096:
        raise Refusal("задание агенту — не длиннее 4096 знаков", "the task must be at most 4096 characters")
    s = Settings(contour=contour, words=words,
                 budget=_int(raw, "budget", 20, 100000, 625, "бюджет", "budget"),
                 answer=_int(raw, "answer", 16, ANSWER_MAX, ANSWER_MAX, "предел ответа", "answer limit"),
                 timeout=float(_int(raw, "timeout", 0, 3600, 0, "срок ответа", "answer timeout")),
                 seed=_int(raw, "seed", 0, 1 << 30, 1, "набор задач", "task set"),
                 second=raw.get("second", "молчит"))
    if s.second not in ("молчит", "такой же"):
        raise Refusal("второй агент — «не ходит» или «ходит»", "the second agent either idles or acts")
    if "exchange" in raw:
        if not valid_session(raw["exchange"]):
            raise Refusal("неверный номер обмена", "invalid exchange session ID")
        if contour == "разомкнутый" and not isinstance(raw.get("cloud_export"), dict):
            raise Refusal("облачный обмен требует подтверждения файлов", "cloud exchange requires file consent")
        s.exchange = raw["exchange"]
    s.agent = str(raw.get("agent") or "встроенный")
    s.agent_lang = str(raw.get("agent_lang") or "ru")
    if s.agent_lang not in ("ru", "en"):
        raise Refusal("язык правил агента — ru или en", "the agent's rule language is ru or en")
    if s.agent != "встроенный":
        chk = own_agent_check(own_agent_path(s.agent).read_bytes())
        if not chk["ok"]:
            raise Refusal(chk["ru"], chk["en"])
    if contour == "сомкнутый":
        s.model = str(raw.get("model") or "").strip()
        if not s.model and (not s.exchange or s.agent == "встроенный"):
            raise Refusal("выбери файл модели (GGUF)", "choose a model file (GGUF)")
        if s.model and s.model != INSTALL_CHECK:
            s.model = str(model_path(s.model))
            if importlib.util.find_spec("llama_cpp") is None:
                raise Refusal("не установлен движок локальных моделей: в терминале `python3 start.py --local`",
                              "the local model engine is not installed: run `python3 start.py --local`")
    else:
        c = raw.get("cloud")
        if not isinstance(c, dict):
            raise Refusal("облако: нужны протокол, адрес и модель", "cloud: protocol, address and model are needed")
        if c.get("kind") not in ("openai", "anthropic"):
            raise Refusal("облако: протокол — OpenAI или Anthropic", "cloud: the protocol is OpenAI or Anthropic")
        if not str(c.get("base", "")).startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise Refusal("облако: адрес начинается с https:// (или http://127.0.0.1 для сервиса на этом компьютере)",
                          "cloud: the address starts with https:// (or http://127.0.0.1 for a service on this computer)")
        if not str(c.get("model", "")).strip():
            raise Refusal("облако: укажи имя модели", "cloud: enter the model name")
        key = str(c.get("key") or "")
        if key and not (key.isascii() and key.isprintable() and " " not in key):
            raise Refusal("облако: ключ — латинские буквы, цифры и знаки, без пробелов",
                          "cloud: the key is Latin letters, digits and signs, without spaces")
        s.cloud = {"kind": c["kind"], "base": str(c["base"]).strip(), "model": str(c["model"]).strip(), "key": key,
                   "max_tokens": _int(c, "max_tokens", 16, 32768, max(16, s.answer // 2),
                                      "токенов на ответ", "tokens per answer")}
    return s


def net_plan(contour: str) -> dict:
    """Как отнять сеть (спецификация §4): печать → пустое сетевое
    пространство → сомкнутый не запускается; разомкнутый идёт с пометкой.
    Заметка — кодом и причиной: слова подставляет экран."""
    why = sealmod.check(allow_unix=(contour == "разомкнутый"))
    if why is None:
        return {"seal": contour, "net": "probe", "allow_net": False, "note": None}
    if contour == "сомкнутый":
        if can_unshare():
            return {"seal": None, "net": "netns", "allow_net": False, "note": {"code": "no_seal_netns", "why": why}}
        return {"error": {"code": "closed_impossible", "why": why}}
    return {"seal": None, "net": "netns", "allow_net": True, "note": {"code": "no_seal_open", "why": why}}


class SealedDriver(Driver):
    """Ведущий шага 5, чей исполнитель ставит себе печать сети первым делом."""

    def __init__(self, *a, seal_mode: str | None = None, **kw):
        if seal_mode:
            self.entry = functools.partial(sealmod.sealed_main, seal_mode)
        super().__init__(*a, **kw)


# ---------------------------------------------------------------- журнал → экран

def contour_of(r: dict) -> int | None:
    c = r.get("contour")
    if c in (1, 2):
        return c
    s = str(r.get("subject") or "")
    if s[:1] in ("а", "п") and s[1:].isdigit():
        return int(s[1:])
    for f in r.get("facts") or []:
        if len(f) >= 2 and isinstance(f[1], str) and f[1].startswith("вход") and f[1][4:].isdigit():
            return int(f[1][4:])
    return None


# Записи журнала, о которых экран не говорит отдельной строкой: служебные
# или уже сказанные вопросом и ответом модели.
SILENT_RULES = {"вопрос модели", "ответ модели", "копия прав помощнику", "вычисление узла", "доставка узла",
                "слова человека"}
SILENT_KINDS = {"начало", "открыт", "учёт"}
BAD_KINDS = {"отказ ворот", "отказ", "останов"}


@dataclass
class Tally:
    """Счётчики одного контура для экрана."""
    spent: int = 0
    questions: int = 0
    bytes_to_model: int = 0
    silent: int = 0
    unparsed: int = 0
    refusals: Counter = field(default_factory=Counter)
    accepted: int = 0
    turns: list = field(default_factory=list)

    def public(self, budget: int) -> dict:
        return {"spent": self.spent, "left": budget - self.spent, "questions": self.questions,
                "bytes_to_model": self.bytes_to_model, "silent": self.silent, "unparsed": self.unparsed,
                "refusals": {str(k): v for k, v in sorted(self.refusals.items())}, "accepted": self.accepted,
                "turns": len(self.turns)}


# ---------------------------------------------------------------- прогон

class Run(threading.Thread):
    def __init__(self, s: Settings, agent: bytes, runs_dir: Path | None = None, *,
                 files: dict[str, bytes] | None = None, consent: Consent | None = None):
        super().__init__(daemon=True, name="прогон")
        self.s, self.agent = s, agent
        self.files, self.consent = files, consent
        cloud_files = files is not None and s.contour == "разомкнутый"
        if ((s.exchange is not None) != (files is not None) or (files is not None and not valid_session(s.exchange))
                or cloud_files != (consent is not None)):
            raise ValueError("обмен требует снимок, облачный обмен — подтверждение адресата и версий")
        if consent and digest(pack_json(input_manifest(files))) != consent.manifest:
            raise ValueError("файлы отличаются от разрешённого снимка")
        self.stamp = s.exchange or time.strftime("%Y%m%d_%H%M%S")
        self.dir = (runs_dir or RUNS) / self.stamp
        self.lock = threading.Lock()
        self.state = "готовится"
        self.activity = {"code": "starting", "since": time.time()}
        self.notes: list[dict] = []
        self.cloud_events: list[dict] = []
        self.cut_questions: set[str] = set()   # отпечатки вопросов, ответ на которые облако обрезало
        self.tally = {1: Tally(), 2: Tally()}
        self.events: list[dict] = []          # для экрана, по порядку
        self.result: dict | None = None
        self.driver = None
        self.bridge = None
        self.stop_requested = False
        self.t0 = time.time()
        self.finished = threading.Event()
        self.tail_done = threading.Event()

    # -- экран
    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            return {"id": self.stamp, "state": self.state, "complete": self.finished.is_set(), "now": time.time(), "t0": self.t0,
                    "activity": dict(self.activity), "notes": list(self.notes),
                    "cloud": self.cloud_events[-5:],
                    "tally": {i: t.public(self.s.budget) for i, t in self.tally.items()},
                    "events": self.events[since:], "next": len(self.events),
                    "result": self.result, "settings": self.s.public()}

    def _act(self, code: str, **kw) -> None:
        self.activity = {"code": code, "since": time.time(), **kw}

    def stop(self) -> None:
        """Остановить сразу: исполнитель убит, процессы модели умирают с ним
        (PR_SET_PDEATHSIG), связь закрыта. Не ждёт конца хода: «стоп»,
        который ждёт, пока модель досчитает, на долгом вопросе выглядит
        неработающим."""
        self.stop_requested = True
        with self.lock:
            self._act("stopping")
        d = self.driver
        if d is not None:
            try:
                d.proc.kill()
            except Exception:
                pass
        if self.bridge is not None:
            self.bridge.close()

    # -- связь
    def _on_cloud(self, kind: str, **kw) -> None:
        with self.lock:
            self.cloud_events.append({"t": time.time(), "kind": kind, **kw})
            if kind == "облако ответило" and kw.get("cut"):
                self.cut_questions.add(kw.get("q"))
            if kind == "облако не ответило":
                self.notes.append({"code": "cloud_failed", "why": kw.get("reason")})

    # -- журнал
    def _on_record(self, r: dict) -> None:
        i = contour_of(r)
        k = r.get("kind")
        now = time.time()
        with self.lock:
            t = self.tally.get(i)
            if t is not None:
                if k == "действие" and r.get("subject") in (f"а{i}", f"п{i}"):
                    t.spent += r.get("charge", 0)
                t.spent += observer5b.cost(r)
            sig = r.get("signature") or ["", ""]
            if k == "действие" and sig[0] == "вопрос модели" and t is not None:
                # Итог прошлого хода так, как его понял сам агент: цикл agent9
                # пишет в историю вопроса строку «> ход => итог». Это слова
                # агента из песочницы, а не запись среды: на экране они стоят
                # отдельно от того, что сделали ворота.
                hist = [x for x in str(r.get("question", "")).split("\n") if x.startswith("> ")]
                if t.turns and hist and " => " in hist[-1]:
                    self.events.append({"type": "agent", "contour": i, "n": len(t.turns),
                                        "text": hist[-1].rsplit(" => ", 1)[1][:200]})
                t.questions += 1
                t.bytes_to_model += r.get("length", 0)
                turn = {"n": len(t.turns) + 1, "contour": i, "q": r.get("question", ""), "q_len": r.get("length", 0),
                        "fee": r.get("charge", 0), "a": None, "move": None, "asked": now}
                t.turns.append(turn)
                self._act("asking", contour=i, n=turn["n"])
                self.events.append({"type": "turn", "contour": i, "n": turn["n"], "q": turn["q"],
                                    "q_len": turn["q_len"], "fee": turn["fee"]})
                return
            if k == "действие" and sig[0] == "ответ модели" and t is not None and t.turns:
                turn = t.turns[-1]
                a = r.get("answer")
                if a is None:
                    t.silent += 1
                    turn["move"], turn["a"] = "молчит", None
                else:
                    m = move_of(a)
                    turn["a"], turn["move"] = a, (m or "не разобран")
                    t.unparsed += m is None
                self._act("moving", contour=i, n=turn["n"])
                mark = question_mark(turn["q"])
                cut = mark in self.cut_questions
                self.cut_questions.discard(mark)
                self.events.append({"type": "answer", "contour": i, "n": turn["n"], "a": turn["a"],
                                    "line": first_line(turn["a"] or "")[:200], "move": turn["move"],
                                    "seconds": round(now - turn["asked"], 1), "cut": cut})
                return
            if t is None:
                return
            if k == "отказ ворот":
                t.refusals[r.get("code")] += 1
            if k == "действие" and sig[0] == "сдача ответа":
                t.accepted += 1
            if k in SILENT_KINDS or (k == "действие" and sig[0] in SILENT_RULES):
                return
            ev = {"type": "effect", "contour": i, "n": len(t.turns), "kind": k, "bad": k in BAD_KINDS}
            if k == "действие":
                ev["rule"], ev["obj"] = sig[0], sig[1]
            ev.update({f: r[f] for f in EFFECT_FIELDS if f in r and f not in ev})
            self.events.append(ev)

    def _tail(self, path: Path) -> None:
        pos, buf = 0, b""
        while True:
            done = self.tail_done.is_set()
            if path.exists():
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                pos += len(chunk)
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    if ln.strip():
                        try:
                            self._on_record(json.loads(ln))
                        except (ValueError, KeyError, TypeError):
                            pass
            if done:
                return
            time.sleep(0.2)

    # -- ход прогона
    def run(self) -> None:
        try:
            self._run()
        except Exception:
            with self.lock:
                self.state = "ошибка"
                self.notes.append({"code": "host_crash", "why": traceback.format_exc(limit=6)})
        finally:
            if self.bridge is not None:
                self.bridge.close()
            try:
                self._save()
            except Exception as exc:
                if self.files is not None:
                    with self.lock:
                        self.state = "ошибка"
                        self.notes.append({"code": "host_crash", "why": f"не сохранён отчёт обмена: {exc}"})
            self.finished.set()

    def _run(self) -> None:
        s = self.s
        closed = s.contour == "сомкнутый"
        use_model = bool(s.model) if closed else True
        live_model = closed and use_model and s.model != INSTALL_CHECK
        if self.consent or not use_model:
            why = sealmod.check(False)
            plan = ({"seal": "сомкнутый", "net": "gateway" if self.consent else None, "allow_net": False}
                    if why is None
                    else {"error": {"code": "closed_impossible", "why": why}})
        elif live_model:
            why = model_sandbox.check()
            if why is not None:
                with self.lock:
                    self.state = "ошибка"
                    self.notes.append({"code": "model_sandbox_unavailable", "why": why})
                return
            plan = {"seal": s.contour if sealmod.check(False) is None else None,
                    "net": "bwrap", "allow_net": False}
        else:
            plan = net_plan(s.contour)
        if plan.get("error"):
            with self.lock:
                self.state = "ошибка"
                self.notes.append(plan["error"])
            return
        if plan.get("note"):
            with self.lock:
                self.notes.append(plan["note"])
        timeout = s.timeout or (300.0 if closed else 90.0)
        self.dir.mkdir(parents=True, exist_ok=self.files is None)
        root = self.dir / "ispolnitel"
        if self.consent:
            c = s.cloud
            cloud = Cloud(c["kind"], c["base"], c["model"], c["key"], c["max_tokens"])
            if self.consent.provider == "openai":
                cloud._tokens_field = "max_completion_tokens"
                cloud._send_temperature = False
            self.bridge = Gateway(cloud, self.consent, root / "run00001", timeout, self._on_cloud)
            spec = self.bridge.specification()
        else:
            if closed:
                mode = ["--script", str(INSTALL_SCRIPT)] if s.model == INSTALL_CHECK else ["--gguf", s.model]
            else:
                c = s.cloud
                self.bridge = Bridge(Cloud(c["kind"], c["base"], c["model"], c["key"], c["max_tokens"]), timeout,
                                     self._on_cloud)
                mode = ["--relay", self.bridge.path]
            spec = ({"mode": mode, "timeout": timeout, "net": plan["net"], "allow_net": plan["allow_net"]}
                    if use_model else None)
            if live_model:
                spec["sandbox_argv"] = model_sandbox.command(Path(s.model))
        cfg = {**CFG, "model": (2 if closed else 1) if use_model else 0, "budget": s.budget,
               "model_answer": s.answer, "task_seed": s.seed}
        if self.consent:
            cfg.update(cloud_manifest=self.consent.manifest, cloud_provider=self.consent.provider,
                       cloud_model=self.consent.model)
        with self.lock:
            self._act("loading_model" if live_model else "starting")
        kind = "exchange" if self.files is not None else "6"
        self.driver = SealedDriver(root, frozenset(), kind=kind, model=spec,
                                   timeout=TURNS_MAX * timeout + 600.0, seal_mode=plan["seal"])
        if self.stop_requested:
            self.driver.proc.kill()
        tail = threading.Thread(target=self._tail, args=(root / "run00001" / "journal5.jsonl",), daemon=True)
        tail.start()
        with self.lock:
            self.state = "идёт"
        mods = {1: self.agent, 2: self.agent if s.second == "такой же" else IDLE}
        checker = exchange_observer.check if self.files is not None else answer_observer.check
        try:
            r = run_contours(self.driver, None, "песочница", mods, cfg, rounds=1, checker=checker,
                             extra={"ext_world": W6, "expect_all": False,
                                    "gateway_provider": self.consent.provider if self.consent else None},
                             words=s.words, files=self.files)
        finally:
            self.driver.close()
            for _ in range(50):                  # процессы модели умирают с исполнителем
                if not self.driver.proc.is_alive():
                    break
                time.sleep(0.1)
            self.tail_done.set()
            tail.join(timeout=10)
        if self.stop_requested:
            with self.lock:
                self.state = "остановлено"
                self._act("stopped")
                self.result = {"stopped": True, "workdir": r.get("workdir")}
            return
        with self.lock:
            self.state = "проверка законов"
            self._act("checking")
        self._finish(r, plan)

    def _finish(self, r: dict, plan: dict) -> None:
        laws = r.get("laws") or {}
        groups, broken = [], []
        for g in texts.GROUPS:
            items = []
            for k in g["keys"]:
                if k not in laws:
                    continue
                st, detail = laws[k]
                items.append({"key": k, "status": st, "detail": str(detail)[:500]})
                if st == BROKEN:
                    broken.append(k)
            groups.append({"id": g["id"], "items": items,
                           "status": BROKEN if any(x["status"] == BROKEN for x in items)
                           else HOLDS if any(x["status"] == HOLDS for x in items) else NA})
        known = {k for g in texts.GROUPS for k in g["keys"]}
        for k in laws:                                         # закон вне групп не теряется
            if k not in known and not k.startswith("_"):
                st, detail = laws[k]
                groups[-1]["items"].append({"key": k, "status": st, "detail": str(detail)[:500]})
                broken += [k] if st == BROKEN else []
        counted = [x for g in groups for x in g["items"]]
        dump = r.get("dump") or {}
        contours = {}
        for i in (1, 2):
            c = (dump.get("contours") or {}).get(i) or (dump.get("contours") or {}).get(str(i)) or {}
            contours[i] = {"budget_left": c.get("budget"), "accepted_parts": len(c.get("accepted", [])),
                           "outcome": [v for k, v in r.get("runs", []) if k == i]}
        journal_tip = None
        if self.files is not None and r.get("workdir"):
            rows, problem = read_journal(Path(r["workdir"]) / "journal5.jsonl")
            if rows and not problem:
                journal_tip = rows[-1]["hash"]
        with self.lock:
            self.result = {
                "laws_total": len(counted), "laws_holds": sum(x["status"] == HOLDS for x in counted),
                "laws_na": sum(x["status"] == NA for x in counted), "broken": broken, "groups": groups,
                "audit": r.get("audit", []), "contours": contours,
                "seal": plan.get("seal"), "net": plan.get("net"), "open": self.s.contour == "разомкнутый",
                "workdir": r.get("workdir"), "journal_tip": journal_tip,
                "gateway": asdict(self.consent) if self.consent else None,
                "tokens": dict(self.bridge.tokens) if self.bridge else None}
            self.state = "готово"
            self._act("done")

    def _save(self) -> None:
        """Итоги — рядом с журналом. Ключа здесь нет: Settings.public."""
        if not self.dir.exists():
            return
        snap = self.snapshot()
        snap.pop("events", None)
        snap["complete"] = True
        snap["runtime"] = {"wasmtime": version("wasmtime")}
        with self.lock:
            snap["turns"] = {i: t.turns for i, t in self.tally.items()}
        data = json.dumps(snap, ensure_ascii=False, indent=1, default=str).encode("utf-8")
        put_private(self.dir / "itog.json", data)


def zip_run(stamp: str) -> Path:
    """Папка прогона одним zip. Имя проверяется: только прогоны из RUNS."""
    if not (valid_session(stamp) or (stamp.replace("_", "").isdigit() and len(stamp) == 15)):
        raise ValueError("нет такого прогона")
    d = RUNS / stamp
    if not d.is_dir():
        raise ValueError("нет такого прогона")
    out = RUNS / f"prostranstvo_{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(d.rglob("*")):
            rel = p.relative_to(d)
            if "exchange-state" in rel.parts:
                continue
            if p.is_file() and not p.is_symlink():
                z.write(p, p.relative_to(RUNS))
    return out
