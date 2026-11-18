"""Шаг 8в: перебор всех попыток — каждый вызов среды, какой может сделать агент.

Стратегии-противники шага 8б придумывались руками и пропустили самую
дорогую дыру. Здесь их даёт перебор: все номера таблицы и номера вне её,
все операции, классы аргументов, `spawn`; для `--kind 5б` и `6`; при
полном и при малом остатке; при прежнем правиле и с правилом «попытка
платит» (спецификация шага 8в, §4).

Каждая попытка делается в новой песочнице, одна, и о ней пишется: код
возврата, изменение расхода, какие записи появились, остановлен ли контур.

**Закон перебора (объявлен до прогона):** с правилом нет ни одной попытки,
у которой расход не вырос и контур не остановлен. При прежнем правиле
такие попытки перечисляются — это мера дыры.

Различимые классы попыток (код и виды записей) становятся стратегиями
«повторять, пока контур не остановится» — для сервера и телефона
(phone8/bound8b.py).

С ходом модели (шаг 9, контракт §17; `--model`): режим модели включён
(model = 1 или, с `--model-mode 2`, model = 2 — контракт §18; плата за байты
U = 906, процесс модели — заглушка) и перебираются
попытки, которые задевает новый ход: все операции на номере модели с
прежними классами аргументов и двумя своими (вопрос длиннее 4096; ответу
некуда лечь), справочник с именем «модель», `spawn`. Прочие попытки от
режима модели не меняются — их перебирает прежний прогон.

Чего перебор не умеет: состояния он берёт только два — свежий контур и
контур после трёх помощников (для предела экземпляров); попытки, которым
нужно долгое предисловие (принятая часть задачи, полная память узлов),
он видит лишь через классы кодов, которые те дают и в свежем состоянии.

python3 experiments/enum8v.py [--exe "КОМАНДА ИСПОЛНИТЕЛЯ"]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from experiments import programs5b as p5b  # noqa: E402

RESULTS = HERE / "results"

# Цены шага 8б; бюджет полный — из окна, малый — меньше g, ноль — уже пуст.
PRICES = {"price": 10, "fail_price": 10, "work_price": 10, "gate_price": 5}
CFG = {"5б": {"graph": "M3т", "levels": 4, "memory": 4, **PRICES},
       "6": {"graph": "M3т", "levels": 4, "memory": 4, **PRICES, "check_price": 10, "task_rng": "splitmix"}}
BUDGETS = {"полный": 625, "малый": 3, "ноль": 0}
TABLE = {"5б": 7, "6": 8}
# Ход модели: настройки пилота шага 9 и номер модели — последний в таблице.
MODEL_BYTE_UNIT = 906


def model_cfg(mode: int) -> dict:
    """mode — значение настройки model: 1 (модель снаружи) или 2 (на устройстве)."""
    return {"model": int(mode), "model_byte_unit": MODEL_BYTE_UNIT}


MODEL_ARGS = {"вопрос длиннее 4096": (0, 5000), "ответу некуда лечь": (65536 - 100, 4)}
RESULT_AT = 1000          # куда модуль кладёт код возврата; канарейка — в первых 16 байтах

# Данные модуля: имя из описи, сообщение, сдача ответа «задача 1, часть 0,
# ответ 1 2» (неверный: задачи нумеруются с 1).
DATA = {600: "цена".encode(), 640: "А".encode(), 660: bytes([1, 0, 1, 2]), 700: "модель".encode()}
ARGS = {
    "пусто": (0, 0),
    "имя «цена»": (600, len(DATA[600])),
    "сообщение «А»": (640, len(DATA[640])),
    "ответ 1.0": (660, 4),
    "узел 1": (1, 0),
    "узел 99": (99, 0),
    "вне памяти": (1 << 20, 4),
    "отрицательный": (-1, 4),
    "огромная длина": (0, (1 << 31) - 1),
}
OPS = list(range(-1, 15)) + [99]


def caps(kind: str) -> list[int]:
    return [-1] + list(range(TABLE[kind] + 1)) + [99]


def _hex(b: bytes) -> str:
    return "".join(f"\\{x:02x}" for x in b)


def module(probe: tuple | None, prefix: int = 0, loop: bool = False) -> str:
    """probe — ("call", номер, оп, a, b) или ("spawn",); None — только счёт
    (топливо). prefix — сколько помощников создать до попытки. loop —
    повторять попытку без конца (стратегия)."""
    if probe is None:
        body = ("(loop $l (local.set $x (i32.add (i32.mul (local.get $x) (i32.const 1103515245)) "
                "(i32.const 12345))) (br $l))")
    else:
        if probe[0] == "spawn":
            call = "(call $spawn)"
        else:
            _, cap, op, a, b = probe
            call = f"(call $call (i32.const {cap}) (i32.const {op}) (i32.const {a}) (i32.const {b}))"
        one = f"(i32.store (i32.const {RESULT_AT}) {call})"
        body = f"(loop $l {one} (br $l))" if loop else one
    pre = " ".join(["(drop (call $spawn))"] * prefix)
    data = "".join(f'\n  (data (i32.const {a}) "{_hex(v)}")' for a, v in DATA.items())
    return p5b.HEAD + data + f'''
  (func (export "main") (local $x i32)
    {pre}
    {body})
  (func (export "helper") (nop)))'''


# ---- исполнители

class JsonExe:
    """Внешний исполнитель по протоколу контракта шага 7, §1."""

    def __init__(self, exe: list[str], kind: str, model: int = 0):
        self.tmp = tempfile.TemporaryDirectory()
        extra = ["--model=--stub", "--model-timeout=20"] if model else []
        # Программа процесса модели — заглушка рядом (model9/serve.py), если
        # другая не задана в MODEL_SERVE (обвязка судьи, harness.py, --serve);
        # путь к ней внешний исполнитель берёт из MODEL_SERVE.
        env = {**os.environ}
        env.setdefault("MODEL_SERVE", str(HERE.parent / "model9" / "serve.py"))
        self.p = subprocess.Popen(exe + ["--root", self.tmp.name, "--kind", kind, *extra], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, encoding="utf-8", env=env)

    def call(self, cmd, *args):
        args = [{"b64": base64.b64encode(x).decode()} if isinstance(x, bytes) else x for x in args]
        self.p.stdin.write(json.dumps({"cmd": cmd, "args": args}, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        r = json.loads(self.p.stdout.readline())
        return r["status"], _unb64(r["value"])

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        self.tmp.cleanup()


def _unb64(x):
    if isinstance(x, dict):
        return base64.b64decode(x["b64"]) if set(x) == {"b64"} else {k: _unb64(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_unb64(v) for v in x]
    return x


def local(kind: str):
    from sandbox.process5 import LocalDriver
    tmp = tempfile.TemporaryDirectory()
    d = LocalDriver(Path(tmp.name), frozenset(), kind=kind)
    d.close = tmp.cleanup
    return d


# Первая реализация — тоже отдельным процессом: память песочниц Wasmtime в
# одном процессе не отдаётся, и тысячи песочниц её исчерпывают.
PY_EXE = [sys.executable, "-m", "sandbox.jsonfront"]
RESTART = 500             # попыток на один процесс исполнителя


# ---- одна попытка

def attempt(drv, kind: str, cfg: dict, probe: tuple, prefix: int = 0) -> dict:
    from wasmtime import wat2wasm
    st, wd = drv.call("new", cfg, None)
    if st != "ok":
        return {"error": f"new: {wd}"}
    st, lo = drv.call("load", 1, bytes(wat2wasm(module(probe, prefix))))
    st, ro = drv.call("run", 1)
    st, dump = drv.call("dump")
    c = dump["contours"]["1"] if "1" in dump["contours"] else dump["contours"][1]
    recs = [json.loads(x) for x in (Path(wd) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]
    start = max(n for n, r in enumerate(recs) if r["kind"] == "начало" and r.get("contour") == 1)
    after = [r for r in recs[start + 1:] if r.get("contour") == 1 or r.get("subject") in ("а1", "п1")]
    mem = c["memories"][0] if c["memories"] else b""
    code = int.from_bytes(mem[RESULT_AT:RESULT_AT + 4], "little", signed=True) if len(mem) > RESULT_AT else None
    stopped = tuple(ro) == ("ловушка", "бюджет исчерпан")
    spent_before = 0
    if prefix:
        # Предисловие — помощники: их плата и записи — не попытка.
        spent_before = sum(r.get("charge", 0) for r in after if r.get("kind") == "действие"
                           and r["signature"][0] == "создание помощника")
        last = max(n for n, r in enumerate(after) if r.get("kind") == "действие"
                   and r["signature"][0] == "копия прав помощнику")
        after = after[last + 1:]
    kinds = []
    for r in after:
        k = r["kind"] if r["kind"] != "действие" else "действие: " + r["signature"][0]
        if k not in kinds:
            kinds.append(k)
    return {"code": None if stopped else code, "run": list(ro), "spent": c["spent"] - spent_before,
            "records": kinds, "stopped": stopped, "load": list(lo)}


def probes(kind: str, model: int = 0):
    if model:
        cap = TABLE[kind]                       # номер модели (контракт §17)
        for op in OPS:
            for an, (a, b) in {**ARGS, **MODEL_ARGS}.items():
                yield f"call {cap} {op} {an}", ("call", cap, op, a, b), 0
        yield "справочник «модель»", ("call", p5b.CAP_RES, 2, 700, len("модель".encode())), 0
        yield "spawn", ("spawn",), 0
        yield "spawn при пределе экземпляров", ("spawn",), 3
        return
    for cap in caps(kind):
        for op in OPS:
            for an, (a, b) in ARGS.items():
                yield f"call {cap} {op} {an}", ("call", cap, op, a, b), 0
    yield "spawn", ("spawn",), 0
    yield "spawn при пределе экземпляров", ("spawn",), 3


def enumerate_all(make_driver, model: int = 0) -> list[dict]:
    """make_driver(kind) — новый исполнитель. model — 0 (без хода модели) или
    значение настройки model, 1 или 2; тогда исполнитель должен быть запущен
    с процессом модели."""
    rows = []
    for kind in ("5б", "6"):
        drv, used = make_driver(kind), 0
        try:
            for rule in (0, 1):
                for bname, budget in BUDGETS.items():
                    cfg = {**CFG[kind], "budget": budget, **({"paid_attempts": 1} if rule else {}),
                           **(model_cfg(model) if model else {})}
                    for name, probe, prefix in probes(kind, model):
                        if prefix and bname != "полный":
                            continue          # при малом бюджете помощники предисловия не создаются
                        if used == RESTART:
                            drv.close()
                            drv, used = make_driver(kind), 0
                        r = attempt(drv, kind, cfg, probe, prefix)
                        used += 1
                        rows.append({"kind": kind, "rule": rule, "budget": bname, "probe": name,
                                     "call": list(probe), "prefix": prefix, **r})
        finally:
            drv.close()
    return rows


def free(r: dict) -> bool:
    """Попытка без цены: расход не вырос, контур не остановлен."""
    return r["spent"] == 0 and not r["stopped"]


def classes(rows: list[dict]) -> dict[tuple, dict]:
    """Классы попыток с правилом при полном бюджете: код отказа (успехи —
    один код) и виды записей. Представитель — первая попытка класса."""
    out: dict[tuple, dict] = {}
    for r in rows:
        if r["rule"] == 1 and r["budget"] == "полный":
            code = r["code"] if r["code"] is not None and r["code"] < 0 else "успех"
            key = (r["kind"], code, tuple(r["records"]))
            out.setdefault(key, r)
    return out


def summary(rows: list[dict]) -> dict:
    s = {}
    for rule in (0, 1):
        sel = [r for r in rows if r["rule"] == rule]
        s[rule] = {"попыток": len(sel), "без цены": sum(1 for r in sel if free(r)),
                   "остановили контур": sum(1 for r in sel if r["stopped"]),
                   "коды без цены": dict(Counter(str(r["code"]) for r in sel if free(r)))}
    return s


def write(rows: list[dict], name: str, title: str = "Шаг 8в: перебор попыток") -> Path:
    cl = classes(rows)
    s = summary(rows)
    L = [f"# {title}", "",
         "Каждая попытка — одна, в новой песочнице. «Без цены» — расход не вырос и контур не остановлен.", "",
         "| Правило | попыток | без цены | остановили контур | коды без цены |", "|---|---|---|---|---|"]
    for rule, v in s.items():
        codes = ", ".join(f"{k}: {n}" for k, n in sorted(v["коды без цены"].items())) or "—"
        L.append(f"| {'попытка платит' if rule else 'прежнее'} | {v['попыток']} | {v['без цены']} | "
                 f"{v['остановили контур']} | {codes} |")
    L += ["", f"**Закон перебора:** с правилом попыток без цены — {s[1]['без цены']}. "
          f"{'Держится.' if s[1]['без цены'] == 0 else 'НЕ держится.'}", "",
          f"## Классы попыток с правилом, полный бюджет: {len(cl)}", "",
          "| Вид | код | записи | плата | представитель |", "|---|---|---|---|---|"]
    for (kind, code, recs), r in sorted(cl.items(), key=lambda x: (x[0][0], str(x[0][1]), x[0][2])):
        L.append(f"| {kind} | {code} | {', '.join(recs) or '—'} | {r['spent']} | {r['probe']} |")
    out = RESULTS / f"{name}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    (RESULTS / f"{name}.json").write_text(json.dumps({"summary": s, "rows": rows}, ensure_ascii=False, indent=0,
                                                     default=str), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="команда внешнего исполнителя одной строкой; без неё — первая реализация")
    ap.add_argument("--name", default=None, help="имя файлов итога; по умолчанию shag8v_enum или shag9_enum")
    ap.add_argument("--model", action="store_true", help="перебор попыток хода модели (шаг 9)")
    ap.add_argument("--model-mode", type=int, default=1, choices=[1, 2],
                    help="с --model: 1 — модель снаружи, 2 — на устройстве (контракт §18)")
    a = ap.parse_args()
    exe = shlex.split(a.exe) if a.exe else PY_EXE
    mode = a.model_mode if a.model else 0
    make = lambda kind: JsonExe(exe, kind, mode)  # noqa: E731
    rows = enumerate_all(make, mode)
    a.name = a.name or {0: "shag8v_enum", 1: "shag9_enum", 2: "shag9b_enum"}[mode]
    title = {0: "Шаг 8в: перебор попыток", 1: "Шаг 9: перебор попыток хода модели",
             2: "Шаг 9б: перебор попыток хода модели, модель на устройстве"}[mode]
    print(write(rows, a.name, title).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
