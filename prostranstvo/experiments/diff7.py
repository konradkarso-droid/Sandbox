"""Шаг 7: различающее сравнение двух реализаций исполнителя.

Одни и те же программы, данные и настройки — через первую реализацию
(sandbox/jsonfront.py) и вторую (отдельная программа) одной и той же
обвязкой (judge/harness.py из чистой комнаты). Всё случайное совпадает:
секрет узлов и канарейки заданы, задачи — из переносимого генератора.

Сравнивается после приведения к общему виду (спецификация шага 7, §3):
журнал без отпечатков и причин отказа загрузки, получатели, выгрузка без
топлива, исходы по виду. Программы, остановленные топливом или стеком,
сравниваются только по виду исхода: место остановки зависит от движка.

Ещё здесь:
- порча готовых файлов (§4) — чувствительность наблюдателей к файлам
  каждой реализации;
- разбор записи системных вызовов (strace) процесса исполнителя.
"""

from __future__ import annotations

import codecs
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

# Обвязка чистой комнаты лежит рядом с этим репозиторием (папка rust7);
# другое место можно задать переменной окружения JUDGE.
HERE = Path(__file__).resolve().parents[1]
JUDGE = Path(os.environ.get("JUDGE", HERE.parent / "rust7" / "judge"))
sys.path.insert(0, str(JUDGE))

import harness as H  # noqa: E402
from core.mir3 import World  # noqa: E402
from experiments import programs5 as p5  # noqa: E402
from experiments import programs5b as p5b  # noqa: E402
from experiments.agents6.build import build  # noqa: E402
from verification import observer5b, observer6  # noqa: E402
from verification.observer5 import BROKEN, read_journal  # noqa: E402

PY_EXE = f"env PYTHONPATH={shlex.quote(str(HERE))} python3 -m sandbox.jsonfront"
FIX = {"fixed_secret": "5a" * 32, "fixed_canary": "c0ffee"}
BASE = {**H.BASE, **FIX}
BASE6 = {**H.BASE6, **FIX}
TRAPS = ("топливо кончилось", "стек исчерпан", "обращение за границу памяти", "unreachable")


# ---- приведение к общему виду

def _outcome(o):
    kind, why = (list(o) + ["", ""])[:2]
    if kind == "ловушка":
        return [kind, why if why in TRAPS else "другая"]
    if kind == "отклонён":
        return [kind, "агент уже загружен" if why == "агент уже загружен" else "—"]
    return [kind, why]


# Два места, где первоначальный контракт допускал два прочтения и
# реализации разошлись (контракт, §14; отчёт шага 7, §3.6). С neutral=True
# сравнение их не видит: так проверяется, что за первым различием в журнале
# не прячутся другие.
HELPER_RULES = ("создание помощника", "копия прав помощнику")


def _initial_rights(rights: list) -> list:
    """Начальная таблица (§6 контракта): номера 0–6, для --kind 6 номер 7 —
    задания, и последним — модель (§17: 7 в виде 5б, 8 в виде 6)."""
    return [x for n, x in enumerate(rights) if n < 7 or (n == 7 and str(x[0]).startswith("задания"))
            or (n in (7, 8) and str(x[0]).startswith("модель"))]


def _journal(wd: Path, neutral: bool = False) -> list[dict]:
    recs, broken = read_journal(wd / "journal5.jsonl")
    out = []
    for r in recs:
        r = {k: v for k, v in r.items() if k not in ("seq", "prev", "hash")}
        if r.get("kind") in ("загрузка отклонена", "загрузка: ловушка"):
            r["reason"] = "—" if r["kind"] == "загрузка отклонена" else _outcome(["ловушка", r["reason"]])[1]
        if neutral and r.get("kind") == "действие" and (r.get("signature") or [None])[0] in HELPER_RULES:
            r["subject"] = "*"
        if neutral and r.get("kind") == "начало":
            r["rights"] = _initial_rights(r.get("rights", []))
        out.append(r)
    return out + ([{"разрыв": broken}] if broken else [])


def _receivers(wd: Path) -> dict:
    out = {}
    for i in (1, 2):
        for k in ("выход", "E"):
            p = wd / f"{k}{i}.jsonl"
            out[f"{k}{i}"] = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x] if p.exists() else []
    return out


def _dump(d: dict) -> dict:
    d = copy.deepcopy(d)
    for c in d.get("contours", {}).values():
        c.pop("fuel", None)
        c["refusals"] = {str(k): v for k, v in c.get("refusals", {}).items()}
        c["accepted"] = [list(x) for x in c.get("accepted", [])]
        c["memories"] = [hashlib.sha256(bytes(m)).hexdigest() for m in c.get("memories", [])]
    for o in d.get("objects", {}).values():
        o["data"] = bytes(o["data"]).hex()
        o["refs"] = [list(x) for x in o.get("refs", [])]
    d["contours"] = {str(k): v for k, v in d.get("contours", {}).items()}
    return d


def snapshot(run: dict, neutral: bool = False) -> dict:
    if "workdir" not in run:
        # Исполнитель отказал ещё на `new`: снимок — только этот исход.
        return {"stopped": False, "new": "отказ", "broken": sorted(H.broken(run))}
    wd = Path(run["workdir"])
    stopped = any(o[1][0] == "ловушка" and o[1][1] in ("топливо кончилось", "стек исчерпан") for o in run.get("runs", []))
    stopped |= any(v[0] == "ловушка" and v[1] in ("топливо кончилось", "стек исчерпан") for v in run.get("loads", {}).values())
    return {
        "stopped": stopped,
        "loads": {str(i): _outcome(v) for i, v in run.get("loads", {}).items()},
        "runs": [[i, _outcome(o)] for i, o in run.get("runs", [])],
        "crashes": run.get("crashes", []),
        "journal": _journal(wd, neutral),
        "receivers": _receivers(wd),
        "dump": _dump(run["dump"]) if run.get("dump") else None,
        "broken": sorted(H.broken(run)),
    }


def first_difference(a, b, path="") -> str | None:
    if type(a) != type(b):
        return f"{path}: {str(a)[:150]} ≠ {str(b)[:150]}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return f"{path}/{k}: есть только в {'первой' if k in a else 'второй'}"
            d = first_difference(a[k], b[k], f"{path}/{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        if len(a) != len(b):
            return f"{path}: длина {len(a)} ≠ {len(b)}; лишнее: {str((a if len(a) > len(b) else b)[min(len(a), len(b))])[:200]}"
        return None
    return None if a == b else f"{path}: {str(a)[:150]} ≠ {str(b)[:150]}"


def compare(sa: dict, sb: dict) -> tuple[str, str | None]:
    """(вид, первое различие). Вид: «совпали», «сравнено по исходу» (запуск
    остановлен топливом или стеком), «расхождение»."""
    if sa["stopped"] or sb["stopped"]:
        keys = ("loads", "runs", "broken")
        d = first_difference({k: sa[k] for k in keys}, {k: sb[k] for k in keys})
        return ("сравнено по исходу" if d is None else "расхождение"), d
    d = first_difference(sa, sb)
    return ("совпали" if d is None else "расхождение"), d


# ---- случаи

def cases(quick: bool, n_rand: int | None = None, new_prices: bool | str = False) -> list[tuple]:
    """(набор, вид исполнителя, имя, модули, настройки, сбой, сбой восстановления, кругов, наблюдатель, доп.)
    n_rand — сколько случайных программ шагов 5 и 5б (зёрна 0…n_rand−1).
    new_prices — цены шага 8б (контракт §15) во всех случаях, как в обвязке."""
    paid = {"paid_attempts": 1} if new_prices == "paid" else {}       # шаг 8в, контракт §16
    BASE = {**globals()["BASE"], **({**H.PRICES8B, **paid} if new_prices else {})}
    BASE6 = {**globals()["BASE6"], **({**H.PRICES8B_6, **paid} if new_prices else {})}
    out = []
    n_rand = n_rand if n_rand is not None else (3 if quick else 30)
    for name, src in p5.named().items():
        out.append(("шаг 5", "5б", name, {1: src, 2: src}, BASE, None, None, 2, None, None))
    for s in range(n_rand):
        out.append(("шаг 5", "5б", f"случайная {s}", {1: p5.random_program(s), 2: p5.random_program(100_000 + s)},
                    BASE, None, None, 2, None, None))
    w = World(BASE["graph"], BASE["levels"], BASE["memory"])
    for name, (m, h) in p5b.named5b(w).items():
        src = p5b.module(m, h)
        out.append(("5б", "5б", name, {1: src, 2: src}, BASE, None, None, 2, None, None))
    for s in range(n_rand):
        m1, h1 = p5b.random_moves(s, w.n)
        m2, h2 = p5b.random_moves(100_000 + s, w.n)
        out.append(("5б", "5б", f"случайная {s}", {1: p5b.module(m1, h1), 2: p5b.module(m2, h2)}, BASE, None, None,
                    2, None, None))
    scen = {"фиксация": ([("сообщение", "А"), ("сообщение", "Б")], BASE), "в E": (p5b.witness(w), BASE),
            "работа": (p5b.greedy(w, upto=6), BASE), "мало бюджета": ([("сообщение", "А")] * 5, {**BASE, "budget": 3})}
    for point in H.FAULT_POINTS:
        for name, (mv, cfg) in scen.items():
            src = p5b.module(mv)
            if point == "посреди восстановления":
                out.append(("5б сбои", "5б", f"{point}; {name}", {1: src, 2: src}, cfg, ("после списания", 1),
                            (point, 1), 2, None, None))
            else:
                for nth in (1, 2):
                    out.append(("5б сбои", "5б", f"{point} №{nth}; {name}", {1: src, 2: src}, cfg, (point, nth), None,
                                2, None, None))
    for vname, v in H.VARIANTS.items():
        for seed in range(2 if quick else 5):
            cfg = {**BASE6, **v, "task_seed": seed}
            for aname, defs in H.AGENTS6.items():
                b = build(defs, H.e_moves(cfg))
                out.append(("шаг 6", "6", f"{vname} {aname} #{seed}", {1: b, 2: b}, cfg, None, None, 1,
                            observer6.check, {"expect_all": vname == "M3" and aname in ("честный", "с ошибками")}))
    for point in H.FAULT_POINTS:
        for aname in ("честный", "с ошибками", "подделыватель"):
            b = build(H.AGENTS6[aname], H.e_moves(BASE6))
            fault = ("после списания", 1) if point == "посреди восстановления" else (point, 3)
            rf = (point, 1) if point == "посреди восстановления" else None
            out.append(("6 сбои", "6", f"{point}; {aname}", {1: b, 2: b}, BASE6, fault, rf, 1, observer6.check,
                        {"expect_all": False}))
    return out


def run_both(rust_exe: str, root: Path, quick: bool, trace: bool = True, n_rand: int | None = None,
             first_exe: str = PY_EXE, neutral: bool = False, new_prices: bool | str = False) -> list[dict]:
    """first_exe — с чем сравнивать; по умолчанию первая реализация (Python).
    neutral — не видеть двух известных мест двоякого прочтения контракта."""
    rows = []
    drivers = {}
    for impl, exe in (("первая", first_exe), ("вторая", rust_exe)):
        for kind in ("5б", "6"):
            drivers[(impl, kind)] = H.ExeDriver(exe, root / impl / kind, kind, trace=trace and impl == "вторая")
    for suite, kind, name, mods, cfg, fault, rfault, rounds, checker, extra in cases(quick, n_rand, new_prices):
        snaps = {}
        runs = {}
        for impl in ("первая", "вторая"):
            r = H.one(drivers[(impl, kind)], name, mods, cfg, fault, rfault, rounds=rounds, checker=checker,
                      extra=extra)
            runs[impl] = r
            snaps[impl] = snapshot(r, neutral)
        verdict, diff = compare(snaps["первая"], snaps["вторая"])
        rows.append({"suite": suite, "name": name, "verdict": verdict, "diff": diff,
                     "broken": {k: v["broken"] for k, v in snaps.items()},
                     "workdirs": {k: v.get("workdir") for k, v in runs.items()}})
    for d in drivers.values():
        d.close()
    return rows


def run_both_9(rust_exe: str, root: Path, quick: bool, first_exe: str = PY_EXE, neutral: bool = False,
               mode: int = 1, nets: tuple = (None, None), envs: tuple = (None, None)) -> list[dict]:
    """Сверка на случаях шага 9 (вопрос модели, контракт §17) — тех же, что
    гоняет набор обвязки `--suite 9`. Процесс модели у обеих реализаций — одна
    и та же заглушка судьи, поэтому ответы совпадают байт в байт.

    mode — значение настройки model в случаях с моделью (1 или 2, §18);
    nets — способ «без сети» для первой и второй стороны: None (netns) или
    "probe" — тогда исполнителю подкладывается подменная socket(), как на
    Android; envs — переменные окружения сторон поверх обычных (например,
    своя MODEL_SERVE). Сторонами могут быть и одна реализация в двух
    способах или с двумя программами модели."""
    rows = []
    drivers: dict[tuple, H.ExeDriver] = {}
    for n, c in enumerate(H.cases_9(quick, root / "сценарии", mode)):
        snaps, runs = {}, {}
        for impl, exe, net, env in (("первая", first_exe, nets[0], envs[0]), ("вторая", rust_exe, nets[1], envs[1])):
            key = (impl, c["kind"], c["model"], c["timeout"], n if c["fresh"] else None)
            if key not in drivers:
                kw = {"model_net": "probe", "nosock": "eperm"} if net == "probe" else {}
                drivers[key] = H.ExeDriver(exe, root / impl / f"{n:03d}", c["kind"], model=c["model"],
                                           model_timeout=c["timeout"], env=env, **kw)
            time.sleep(c.get("pause", 0))
            # Секрет узлов и канарейка заданы, как во всех случаях сверки (FIX).
            r = H.one(drivers[key], c["name"], c["mods"], {**c["cfg"], **FIX}, c["fault"], c["rfault"],
                      rounds=c["rounds"], checker=c["checker"], extra=c["extra"])
            runs[impl], snaps[impl] = r, snapshot(r, neutral)
            if c["fresh"]:
                drivers.pop(key).close()
        verdict, diff = compare(snaps["первая"], snaps["вторая"])
        rows.append({"suite": "9", "name": c["name"], "verdict": verdict, "diff": diff,
                     "broken": {k: v["broken"] for k, v in snaps.items()},
                     "workdirs": {k: v.get("workdir") for k, v in runs.items()}})
    for d in drivers.values():
        d.close()
    return rows


def all_differences(a, b, path="") -> list[tuple[str, object, object]]:
    """Все различия двух снимков: (путь, значение в первом, во втором)."""
    if type(a) != type(b):
        return [(path, a, b)]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append((f"{path}/{k}", a.get(k, "нет"), b.get(k, "нет")))
            else:
                out += all_differences(a[k], b[k], f"{path}/{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [(path, f"длина {len(a)}", f"длина {len(b)}")]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in all_differences(x, y, f"{path}[{i}]")]
    return [] if a == b else [(path, a, b)]


# Строка истории ходов цикла агента шага 9 после хода «СПРАВКА модель».
ASKED_MODE = "СПРАВКА модель => ответ "


def run_modes_9(root: Path, quick: bool, exe: str = PY_EXE) -> list[dict]:
    """Режим 2 против режима 1 на одном исполнителе (контракт §18): те же
    случаи шага 9, настройка model — 1 и 2. По контракту ворота ведут себя
    одинаково, и снимки обязаны различаться только значением model в записи
    «учёт».

    Второе разрешённое различие — не от ворот: то, что агент сам сделал с
    ответом справочника (справочник возвращает номер режима кодом возврата).
    Цикл агента шага 9 (experiments/agents9/agent9.c) пишет его в свою
    историю ходов строкой ASKED_MODE; история лежит в памяти агента и уходит
    в следующий вопрос модели. Поэтому разрешены вопрос, отличающийся ровно
    этой строкой, и байты памяти «знак 1 против знака 2».

    Каждому случаю — строка: все различия и те из них, что не разрешены
    («лишние»). Память сравнивается байтами, а не отпечатком."""
    rows = []
    drivers: dict[tuple, H.ExeDriver] = {}
    by_mode = {m: H.cases_9(quick, root / "сценарии" / str(m), m) for m in (1, 2)}
    for n, (c1, c2) in enumerate(zip(by_mode[1], by_mode[2])):
        snaps, mems = {}, {}
        for mode, c in ((1, c1), (2, c2)):
            key = (mode, c["kind"], c["model"].split()[0], c["timeout"], n if c["fresh"] or c["model"] != "--stub" else None)
            if key not in drivers:
                drivers[key] = H.ExeDriver(exe, root / f"режим{mode}" / f"{n:03d}", c["kind"], model=c["model"],
                                           model_timeout=c["timeout"])
            time.sleep(c.get("pause", 0))
            r = H.one(drivers[key], c["name"], c["mods"], {**c["cfg"], **FIX}, c["fault"], c["rfault"],
                      rounds=c["rounds"], checker=c["checker"], extra=c["extra"])
            snaps[mode] = snapshot(r)
            mems[mode] = {str(i): [bytes(m) for m in cc.get("memories", [])]
                          for i, cc in (r.get("dump") or {}).get("contours", {}).items()}
            if key[-1] is not None:
                drivers.pop(key).close()
        diffs = all_differences(snaps[1], snaps[2])
        extra = []
        for path, x, y in diffs:
            if path.endswith("/model") and "/journal[" in path and (x, y) == (1, 2):
                continue                      # запись «учёт»
            if "/memories[" in path:
                continue                      # отпечаток памяти: байты сверяются ниже
            if path.endswith("/question") and isinstance(x, str) and x == str(y).replace(ASKED_MODE + "2", ASKED_MODE + "1"):
                continue                      # агент вписал в вопрос ответ справочника
            extra.append((path, str(x)[:80], str(y)[:80]))
        mem_bytes = 0
        for i in mems[1]:
            for m1, m2 in zip(mems[1][i], mems[2].get(i, [])):
                if len(m1) != len(m2):
                    extra.append((f"память контура {i}", f"длина {len(m1)}", f"длина {len(m2)}"))
                    continue
                for k, (p, q) in enumerate(zip(m1, m2)):
                    if p != q:
                        mem_bytes += 1
                        if (p, q) != (ord("1"), ord("2")):
                            extra.append((f"память контура {i}, байт {k}", p, q))
        rows.append({"name": c1["name"], "различий": len(diffs), "байт памяти": mem_bytes, "лишние": extra[:5],
                     "broken": {m: snaps[m]["broken"] for m in (1, 2)}})
    for d in drivers.values():
        d.close()
    return rows


# ---- порча готовых файлов (§4)

def _rehash(lines: list[dict]) -> bytes:
    prev = "0" * 64
    out = []
    for n, r in enumerate(lines):
        body = {k: v for k, v in r.items() if k not in ("seq", "prev", "hash")}
        body.update(seq=n, prev=prev)
        h = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        out.append(json.dumps({**body, "hash": h}, ensure_ascii=False, sort_keys=True))
        prev = h
    return ("\n".join(out) + "\n").encode()


def mutations(run: dict) -> list[tuple[str, str, object]]:
    """(имя порчи, закон, который обязан её поймать, функция порчи папки)."""
    def jr(wd):
        return [json.loads(x) for x in (wd / "journal5.jsonl").read_text(encoding="utf-8").splitlines() if x]

    def drop_debit(wd):
        recs = jr(wd)
        k = next(i for i, r in enumerate(recs) if r.get("kind") == "списание")
        (wd / "journal5.jsonl").write_bytes(_rehash(recs[:k] + recs[k + 1:]))

    def zero_price(wd):
        recs = jr(wd)
        k = next(i for i, r in enumerate(recs) if r.get("kind") == "списание")
        recs[k]["price"] = 0
        (wd / "journal5.jsonl").write_bytes(_rehash(recs))

    def dup_delivery(wd):
        p = next(p for p in sorted(wd.glob("выход*.jsonl")) if p.read_text(encoding="utf-8").strip())
        lines = p.read_text(encoding="utf-8").splitlines()
        e = json.loads(lines[0])
        e["msgs"] = [e["msgs"][0] + " "]
        p.write_text("\n".join(lines + [json.dumps(e, ensure_ascii=False)]) + "\n", encoding="utf-8")

    def odd_work(wd):
        recs = jr(wd)
        k = next(i for i, r in enumerate(recs) if r.get("kind") == "работа")
        extra = {**recs[k], "node": 9, "first": True, "preds": []}
        (wd / "journal5.jsonl").write_bytes(_rehash(recs[:k + 1] + [extra] + recs[k + 1:]))

    def break_chain(wd):
        lines = (wd / "journal5.jsonl").read_text(encoding="utf-8").splitlines()
        r = json.loads(lines[2])
        r["kind"] = r["kind"] + " "
        lines[2] = json.dumps(r, ensure_ascii=False, sort_keys=True)
        (wd / "journal5.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def wrong_answer(wd):
        p = wd / "выход1.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        k = next(i for i, x in enumerate(lines) if "задача" in x)
        e = json.loads(lines[k])
        head, hexed = e["msgs"][0].split(": ", 1)
        raw = bytearray(bytes.fromhex(hexed))
        raw[-1] ^= 1
        e["msgs"] = [f"{head}: {raw.hex()}"]
        lines[k] = json.dumps(e, ensure_ascii=False)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        recs = jr(wd)
        for r in recs:
            if r.get("kind") == "списание" and r.get("fix") == e["fix"]:
                r["msg"] = e["msgs"][0]
        (wd / "journal5.jsonl").write_bytes(_rehash(recs))

    def payer_swap(wd):
        recs = jr(wd)
        k = next(i for i, r in enumerate(recs) if r.get("kind") == "проверка" and r.get("payer") == "агент")
        recs[k]["payer"] = "среда"
        (wd / "journal5.jsonl").write_bytes(_rehash(recs))

    out = [("удалено списание", "И3р", drop_debit), ("цена списания 0", "И3р", zero_price),
           ("удвоена доставка", "И10р", dup_delivery), ("вписан чужой узел", "И29", odd_work),
           ("разорвана цепочка", "И5", break_chain)]
    if run["cfg"].get("rule"):
        out += [("принят неверный ответ", "П2р", wrong_answer), ("проверку оплатила среда", "П2п", payer_swap)]
    return out


def sensitivity(run: dict, checker) -> list[dict]:
    rows = []
    for name, law, fn in mutations(run):
        wd = Path(run["workdir"])
        copy_wd = wd.parent / (wd.name + "-порча")
        shutil.rmtree(copy_wd, ignore_errors=True)
        shutil.copytree(wd, copy_wd)
        try:
            fn(copy_wd)
        except StopIteration:
            rows.append({"порча": name, "закон": law, "итог": "нечего портить"})
            continue
        r2 = {**run, "workdir": str(copy_wd), "snapshots": []}
        laws = checker(r2)
        caught = [k for k, (s, _) in laws.items() if s == BROKEN and not k.startswith("_")]
        rows.append({"порча": name, "закон": law, "итог": "поймана" if law in caught else "НЕ поймана",
                     "сработали": caught})
    return rows


# ---- системные вызовы

SYSTEM_READ = ("/etc/ld.so", "/lib", "/usr/lib", "/proc/self", "/sys/", "/dev/urandom", "/etc/localtime")


def _strace_path(s: str) -> str:
    """strace пишет байты не из ASCII восьмеричными escape-последовательностями
    (\\321\\201…); без раскодирования русские имена папок не совпадут с корнем."""
    return codecs.escape_decode(s.encode())[0].decode("utf-8", "replace")


def strace_events(folder: Path, root: Path, exe: str) -> list[str]:
    """События процесса второй реализации, которых быть не должно: сеть,
    запуск процессов, файлы вне рабочей папки (кроме чтения системных
    библиотек при старте). Запуск разрешён один — самой программы exe."""
    bad = []
    exe = shlex.split(exe)[0]
    for f in sorted(folder.glob("strace*.txt")):
        for line in f.read_text(errors="replace").splitlines():
            if re.search(r"\b(socket|connect|bind|sendto|sendmsg)\(", line) and "= -1" not in line:
                bad.append(f"сеть: {line[:160]}")
            elif re.search(r"\b(fork|vfork|clone3?)\(", line) and "CLONE_THREAD" not in line:
                bad.append(f"процесс: {line[:160]}")
            elif "execve(" in line and not line.split("execve(", 1)[1].startswith(f'"{exe}"'):
                bad.append(f"запуск: {line[:160]}")
            else:
                m = re.search(r'open(?:at)?\((?:AT_FDCWD, )?"([^"]+)", ([A-Z_|]+)', line)
                if m:
                    path, flags = _strace_path(m.group(1)), m.group(2)
                    writing = any(x in flags for x in ("O_WRONLY", "O_RDWR", "O_CREAT"))
                    inside = path.startswith(str(root))
                    if not inside and (writing or not path.startswith(SYSTEM_READ)):
                        bad.append(f"файл: {line[:160]}")
            m = re.search(r'(?:unlink|rename)(?:at2?)?\([^"]*"([^"]+)"', line)
            if m and not _strace_path(m.group(1)).startswith(str(root)):
                bad.append(f"файлы: {line[:160]}")
    return bad


def strace_seen(folder: Path) -> dict:
    """Сколько вызовов каждого вида записано: показывает, что запись strace
    не пуста и разбор видит обращения к файлам, а не молчит от ошибки."""
    seen: dict = {}
    for f in folder.glob("strace*.txt"):
        for line in f.read_text(errors="replace").splitlines():
            m = re.match(r"\d+\s+(\w+)\(", line)
            if m:
                seen[m.group(1)] = seen.get(m.group(1), 0) + 1
    return seen
