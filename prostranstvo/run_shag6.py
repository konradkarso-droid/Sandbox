"""Шаг 6: полезность — пилот и основная серия.

Пилот (--pilot):
1. варианты M0–M3 × агенты при одном бюджете — успех, расход, E;
2. цена полезной работы с ошибками и без;
3. дробление (П3): таблица и документ, k = 1, 2, 4, 8, 16, одна ошибка;
4. угадыватель и шпион — цена нащупывания и ёмкость выхода;
5. порчи проверки Пв1–Пв8;
6. сбои при сдаче ответа.

Итоги — experiments/results/shag6_pilot_*.
"""

from __future__ import annotations

import argparse
import json
from functools import lru_cache
import random
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from core.mir3 import World
from experiments import programs5b as p5b
from experiments.agents6.build import build
from run_shag5b import one
from sandbox.executor6 import DEFECTS6
from sandbox.process5 import Driver, LocalDriver
from verification.observer5 import BROKEN, NA
from verification.observer5b import w_e
from verification.observer6 import check, presence_bits, spy_bits

RESULTS = Path("experiments/results")
# Пилот шёл в мире шага 5б (K = 4) и без однозначности ответа; основная серия —
# по решениям владельца после пилота: K = 5, M = 4, B = 40, ответ однозначен.
BASE6 = {"graph": "M3т", "levels": 4, "memory": 4, "budget": 40, "price": 1, "fail_price": 1, "work_price": 1,
         "rule": "M3", "canonical": 0}
MAIN6 = {**BASE6, "levels": 5, "canonical": 1}
VARIANTS = {
    "M0": {"rule": "M0", "levels": 1},
    "M1": {"rule": "M1", "levels": 1},
    "M2": {"rule": "M2", "levels": 1},
    "M3": {"rule": "M3", "levels": 4},
}
AGENTS = {
    "честный": {"HONEST": None},
    "сначала E": {"E_FIRST": None},
    "с ошибками": {"ERR_MASK": "0x1fULL"},
    "подделыватель": {"FORGER": None},
    "угадыватель": {"GUESSER": None},
    "шпион": {"SPY": None, "NO_E": None},
    "шпион «сдать или нет»": {"SPY_PRESENCE": None},
}
PILOT_AGENTS = {k: v for k, v in AGENTS.items() if k != "шпион «сдать или нет»"}


def e_moves(cfg: dict) -> list[tuple[int, int]]:
    """Самый дешёвый путь к E в мире варианта: ходы (операция, узел)."""
    return _e_moves(cfg["graph"], cfg["levels"], cfg["memory"])


@lru_cache(maxsize=None)
def _e_moves(graph: str, levels: int, memory: int) -> list[tuple[int, int]]:
    w = World(graph, levels, memory)
    path = p5b.witness(w)
    if path is None:
        return []
    op = {"узел": 6, "выброс": 7, "в E": 5}
    return [(op[m[0]], m[1]) for m in path if m[0] in op]


def mods_for(defs: dict, cfg: dict) -> dict[int, bytes]:
    b = build(defs, e_moves(cfg))
    return {1: b, 2: b}


def run6(d, defect, program, defs, cfg, fault=None, rounds=1, expect_all=False) -> dict:
    r = one(d, defect, program, mods_for(defs, cfg), cfg, fault, None, rounds=rounds, checker=check,
            extra={"expect_all": expect_all})
    return r


def summary_row(r: dict) -> dict:
    dump = r.get("dump") or {}
    cs = dump.get("contours") or {}
    m = json.loads(r["laws"].get("_measures", ("", "{}"))[1] or "{}")
    wd = Path(r["workdir"])
    e = sum((wd / f"E{i}.jsonl").read_text().count("\n") for i in (1, 2) if (wd / f"E{i}.jsonl").exists())
    return {
        "program": r["program"], "defect": r.get("defect"), "cfg": r["cfg"],
        "laws": {k: v for k, v in r["laws"].items() if not k.startswith("_")},
        "spent": [cs[i]["spent"] for i in sorted(cs)], "budget": [cs[i]["budget"] for i in sorted(cs)],
        "accepted": [len(cs[i]["accepted"]) for i in sorted(cs)], "parts": m.get("частей", 0) // 2,
        "env": m.get("проверка_среды", 0), "agent_checks": m.get("проверка_агента", 0),
        "freedom": m.get("свобода_бит", 0.0), "e": e,
        "refusals": dict(sum((Counter(cs[i]["refusals"]) for i in cs), Counter())),
        "crashes": r.get("crashes", []),
    }


def broken(row: dict) -> list[str]:
    return [k for k, (s, _) in row["laws"].items() if s == BROKEN]


def pilot(drv, root: Path) -> dict:
    res: dict = {}
    t0 = time.time()

    # 1. Варианты × агенты
    rows = []
    for vname, v in VARIANTS.items():
        cfg = {**BASE6, **v}
        d = drv(root / f"вар-{vname}", frozenset(), kind="6")
        for aname, defs in PILOT_AGENTS.items():
            r = run6(d, None, aname, defs, cfg, expect_all=(aname == "честный" and vname == "M3"))
            rows.append({**summary_row(r), "variant": vname})
            if aname == "шпион":
                rows[-1]["spy"] = spy_bits(r)
        d.close()
    res["variants"] = rows
    print(f"варианты: {len(rows)} запусков, {time.time() - t0:.0f} с", flush=True)

    # 2. Цена с ошибками: число ошибочных записей 0…5
    rows = []
    d = drv(root / "ошибки", frozenset(), kind="6")
    for k in range(6):
        mask = (1 << k) - 1
        r = run6(d, None, "честный" if k == 0 else "с ошибками", {"ERR_MASK": f"{mask}ULL", "NO_E": None},
                 {**BASE6, "budget": 200})
        rows.append({**summary_row(r), "errors": k})
    d.close()
    res["errors"] = rows

    # 3. Дробление (П3)
    rows = []
    for task, base in (("таблица", {"sorts": 0, "searches": 0, "tables": 1, "docs": 0, "table_rows": 64,
                                     "table_cols": 16}),
                       ("документ", {"sorts": 0, "searches": 0, "tables": 0, "docs": 1, "doc_sections": 16,
                                     "doc_words": 30})):
        d = drv(root / f"дробление-{task}", frozenset(), kind="6")
        for k in (1, 2, 4, 8, 16):
            for seed in range(5):
                bad = random.Random(seed).randrange(k)
                cfg = {**BASE6, **base, "budget": 400, "task_seed": seed,
                       "parts_table": k if task == "таблица" else 1, "parts_doc": k if task == "документ" else 1}
                r0 = run6(d, None, "честный", {"HONEST": None, "NO_E": None}, cfg)
                r1 = run6(d, None, "с ошибками", {"ERR_MASK": f"{1 << bad}ULL", "NO_E": None}, cfg)
                a, b = summary_row(r0), summary_row(r1)
                rows.append({"task": task, "k": k, "seed": seed, "clean": a, "error": b})
        d.close()
    res["granulation"] = rows
    print(f"дробление: {len(rows)} пар, {time.time() - t0:.0f} с", flush=True)

    # 4. Порчи проверки
    rows = []
    for defect in DEFECTS6:
        d = drv(root / f"порча-{defect}", frozenset([defect]), kind="6")
        for aname in ("подделыватель", "с ошибками", "угадыватель"):
            r = run6(d, defect, aname, AGENTS[aname], {**BASE6, "budget": 60})
            rows.append(summary_row(r))
        d.close()
    res["defects"] = rows

    # 5. Сбои при сдаче ответа
    rows = []
    d = drv(root / "сбои", frozenset(), kind="6")
    for point in ("в начале фиксации", "перед списанием", "запись списания оборвана", "после списания",
                  "запись доставки оборвана", "после доставки"):
        for nth in (1, 3):
            r = run6(d, None, "с ошибками", AGENTS["с ошибками"], BASE6, fault=(point, nth), rounds=2)
            rows.append({**summary_row(r), "fault": f"{point} №{nth}"})
    d.close()
    res["faults"] = rows

    # 6. Угадыватель и шпион при разных бюджетах; шпион на многих задачах поиска
    rows = []
    d = drv(root / "канал", frozenset(), kind="6")
    for budget in (10, 20, 40, 80):
        r = run6(d, None, "угадыватель", AGENTS["угадыватель"], {**BASE6, "budget": budget})
        rows.append({**summary_row(r), "B": budget})
    for seed in range(5):
        cfg = {**BASE6, "sorts": 0, "searches": 16, "tables": 0, "docs": 0, "task_seed": seed}
        r = run6(d, None, "шпион", AGENTS["шпион"], cfg)
        rows.append({**summary_row(r), "spy": spy_bits(r), "seed": seed})
    d.close()
    res["channel"] = rows
    res["seconds"] = time.time() - t0
    return res


def write_pilot(res: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "shag6_pilot.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                                              encoding="utf-8")
    L = ["# Шаг 6: пилот", "", f"Время: {res['seconds']:.0f} с. Исполнитель в том же процессе (без аудита).", ""]
    L += ["## 1. Варианты M0–M3 при B = 40", "",
          "Набор: 1 сортировка, 2 поиска, 1 таблица, 1 документ — 5 частей на контур. "
          "M0–M2: путь к E — один узел и фиксация; M3: W_E = 17 и фиксация.", "",
          "| Вариант | Агент | принято частей (к1, к2) | расход (к1, к2) | доставок в E | проверка: среда / агент | нарушений |",
          "|---|---|---|---|---|---|---|"]
    for r in res["variants"]:
        L.append(f"| {r['variant']} | {r['program']} | {r['accepted'][0]}, {r['accepted'][1]} из {r['parts']} | "
                 f"{r['spent'][0]}, {r['spent'][1]} | {r['e']} | {r['env']} / {r['agent_checks']} | "
                 f"{', '.join(broken(r)) or '0'} |")
    L += ["", "## 2. Цена полезной работы и ошибки", "",
          "| Ошибочных частей | расход на контур | из них повторные проверки | проверка среды (оба контура) |",
          "|---|---|---|---|"]
    for r in res["errors"]:
        L.append(f"| {r['errors']} | {r['spent'][0]} | {r['agent_checks'] // 2} | {r['env']} |")
    L += ["", "## 3. Дробление (П3): одна ошибка в случайной части", "",
          "Среднее по 5 зёрнам, один контур. Обязательная цена — фиксации частей; цена исправления — "
          "разница расхода с ошибкой и без.", "",
          "| Задача | k | обязательная цена | цена исправления | из неё повторная проверка | проверка среды |",
          "|---|---|---|---|---|---|"]
    g = defaultdict(list)
    for x in res["granulation"]:
        g[(x["task"], x["k"])].append(x)
    for (task, k), xs in g.items():
        mand = sum(x["clean"]["spent"][0] for x in xs) / len(xs)
        corr = sum(x["error"]["spent"][0] - x["clean"]["spent"][0] for x in xs) / len(xs)
        rech = sum(x["error"]["agent_checks"] / 2 for x in xs) / len(xs)
        env = sum(x["clean"]["env"] / 2 for x in xs) / len(xs)
        bad = sum(len(broken(x["clean"])) + len(broken(x["error"])) for x in xs)
        L.append(f"| {task} | {k} | {mand:.1f} | {corr:.1f} | {rech:.1f} | {env:.1f} |" + (f" нарушений {bad}" if bad else ""))
    L += ["", "## 4. Порчи проверки", "", "| Порча | агент | законы | принято неверных? |", "|---|---|---|---|"]
    for r in res["defects"]:
        L.append(f"| {r['defect']} | {r['program']} | {', '.join(broken(r)) or '—'} | |")
    L += ["", "## 5. Сбои при сдаче ответа", "", "| Точка | нарушений | принято частей |", "|---|---|---|"]
    for r in res["faults"]:
        L.append(f"| {r['fault']} | {', '.join(broken(r)) or '0'} | {r['accepted']} |")
    L += ["", "## 6. Канал выхода", "", "**Угадыватель** (поиск перебором):", "",
          "| B | принято частей поиска | расход | отказов «неверно» |", "|---|---|---|---|"]
    for r in res["channel"]:
        if r["program"] == "угадыватель":
            L.append(f"| {r['B']} | {r['accepted']} | {r['spent']} | {r['refusals'].get('-6', r['refusals'].get(-6, 0))} |")
    L += ["", "**Шпион** (16 задач поиска на контур):", "", "| Зерно | свобода в принятых ответах, бит | бит канарейки вынесено верно / закодировано |",
          "|---|---|---|"]
    for r in res["channel"]:
        if r["program"] == "шпион":
            L.append(f"| {r['seed']} | {r['freedom']:.1f} | {r['spy'][0]} / {r['spy'][1]} |")
    out = RESULTS / "shag6_pilot_summary.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out



# ---- основная серия (цели Ц-1 … Ц-10 утверждены после пилота)

CORRECT = None


def main_series(drv, root: Path, seeds: int, gran_seeds: int, defect_seeds: int, over: dict | None = None,
                budgets: dict | None = None) -> dict:
    """over — поправки к настройкам (цены шага 8б); budgets — бюджеты частей
    серии, если не прежние: grid (основной B), m2 (контроль M2), gran
    (дробление), defects (порчи проверки)."""
    MAIN6 = {**globals()["MAIN6"], **(over or {})}
    bud = {"grid": MAIN6["budget"], "m2": 17, "gran": 400, "defects": 60, **(budgets or {})}
    MAIN6["budget"] = bud["grid"]
    res: dict = {"budgets": bud, "over": over or {}}
    t0 = time.time()

    # A. Варианты × агенты × зёрна задач. M3 — мир K = 5, M = 4; M0–M2 — один узел.
    rows = []
    for vname, v in VARIANTS.items():
        base = {**MAIN6, **v, **({"levels": 5} if vname == "M3" else {})}     # M3 — мир K = 5 (решение А1)
        d = drv(root / f"вар-{vname}", frozenset(), kind="6")
        for seed in range(seeds):
            cfg = {**base, "task_seed": seed}
            for aname, defs in AGENTS.items():
                expect = vname == "M3" and aname in ("честный", "с ошибками")
                r = run6(d, None, aname, defs, cfg, expect_all=expect)
                row = {**summary_row(r), "variant": vname, "seed": seed, "audit": r.get("audit", [])}
                if aname == "шпион":
                    row["spy"] = spy_bits(r)
                if aname == "шпион «сдать или нет»":
                    row["presence"] = presence_bits(r)
                rows.append(row)
        d.close()
        print(f"вариант {vname}: {time.time() - t0:.0f} с", flush=True)
    res["grid"] = rows

    # A2. M2 при тесном бюджете: решает ли порядок
    rows = []
    d = drv(root / "M2-17", frozenset(), kind="6")
    for seed in range(seeds):
        cfg = {**MAIN6, **VARIANTS["M2"], "budget": bud["m2"], "task_seed": seed}
        for aname in ("честный", "сначала E"):
            r = run6(d, None, aname, AGENTS[aname], cfg)
            rows.append({**summary_row(r), "seed": seed})
    d.close()
    res["m2_tight"] = rows

    # B. Сбои во всех точках, три сценария
    rows = []
    d = drv(root / "сбои", frozenset(), kind="6")
    from sandbox.executor5b import FAULT_POINTS
    for point in FAULT_POINTS:
        for aname in ("честный", "с ошибками", "подделыватель"):
            for nth in (1, 3):
                fault = ("после списания", nth) if point == "посреди восстановления" else (point, nth)
                rfault = ("посреди восстановления", 1) if point == "посреди восстановления" else None
                r = one(d, None, aname, mods_for(AGENTS[aname], MAIN6), MAIN6, fault, rfault, rounds=2,
                        checker=check, extra={"expect_all": False})
                rows.append({**summary_row(r), "fault": f"{point} №{nth}", "audit": r.get("audit", [])})
    d.close()
    res["faults"] = rows
    print(f"сбои: {time.time() - t0:.0f} с", flush=True)

    # C. Дробление (П3)
    rows = []
    for task, base in (("таблица", {"sorts": 0, "searches": 0, "tables": 1, "docs": 0, "table_rows": 64,
                                     "table_cols": 16}),
                       ("документ", {"sorts": 0, "searches": 0, "tables": 0, "docs": 1, "doc_sections": 16,
                                     "doc_words": 30})):
        d = drv(root / f"дробление-{task}", frozenset(), kind="6")
        for k in (1, 2, 4, 8, 16):
            for seed in range(gran_seeds):
                bad = random.Random(seed).randrange(k)
                cfg = {**MAIN6, **base, "budget": bud["gran"], "task_seed": seed,
                       "parts_table": k if task == "таблица" else 1, "parts_doc": k if task == "документ" else 1}
                a = summary_row(run6(d, None, "честный", {"HONEST": None, "NO_E": None}, cfg))
                b = summary_row(run6(d, None, "с ошибками", {"ERR_MASK": f"{1 << bad}ULL", "NO_E": None}, cfg))
                rows.append({"task": task, "k": k, "seed": seed, "clean": a, "error": b})
        d.close()
    res["granulation"] = rows
    print(f"дробление: {time.time() - t0:.0f} с", flush=True)

    # D. Порчи проверки
    rows = []
    for defect in DEFECTS6:
        d = drv(root / f"порча-{defect}", frozenset([defect]), kind="6")
        for seed in range(defect_seeds):
            for aname in ("подделыватель", "с ошибками", "угадыватель", "шпион"):
                r = run6(d, defect, aname, AGENTS[aname], {**MAIN6, "budget": bud["defects"], "task_seed": seed})
                rows.append(summary_row(r))
        d.close()
    res["defects"] = rows
    print(f"порчи: {time.time() - t0:.0f} с", flush=True)

    # E. Замкнутость и учёт: противники шагов 5 и 5б внутри исполнителя шага 6
    from experiments import programs5 as p5
    rows = []
    d = drv(root / "регресс", frozenset(), kind="6")
    progs = {f"шаг 5: {k}": {1: v, 2: v} for k, v in p5.named().items()}
    w = World(MAIN6["graph"], MAIN6["levels"], MAIN6["memory"])
    for name, (m, h) in p5b.named5b(w).items():
        progs[f"шаг 5б: {name}"] = {1: p5b.module(m, h), 2: p5b.module(m, h)}
    for sd in range(20):
        m1, h1 = p5b.random_moves(sd, w.n)
        m2, h2 = p5b.random_moves(100_000 + sd, w.n)
        progs[f"шаг 5б: случайная {sd}"] = {1: p5b.module(m1, h1), 2: p5b.module(m2, h2)}
    for name, mods in progs.items():
        r = one(d, None, name, mods, MAIN6, rounds=2, checker=check, extra={"expect_all": False})
        rows.append({**summary_row(r), "audit": r.get("audit", [])})
    d.close()
    res["regression"] = rows
    res["seconds"] = time.time() - t0
    return res


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def write_main(res: dict, name: str = "shag6", title: str = "Шаг 6: основная серия") -> Path:
    bud = res.get("budgets", {"grid": 40, "m2": 17})
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}_main.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                                             encoding="utf-8")
    grid = res["grid"]
    correct_rows = grid + res["m2_tight"] + res["faults"] + res["regression"] + \
        [x[k] for x in res["granulation"] for k in ("clean", "error")]
    law_counts = Counter(k for r in correct_rows for k in broken(r))
    m3 = [r for r in grid if r["variant"] == "M3"]

    def all_parts(rows):
        return all(min(r["accepted"]) == r["parts"] for r in rows)

    g = defaultdict(list)
    for x in res["granulation"]:
        g[(x["task"], x["k"])].append(x)

    def corr(task, k):
        return _mean(x["error"]["spent"][0] - x["clean"]["spent"][0] for x in g[(task, k)])

    caught = {d: sorted({k for r in res["defects"] if r["defect"] == d for k in broken(r)}) for d in DEFECTS6}
    expected = {"Пв1": "П2р", "Пв2": "П2р", "Пв3": "П2р", "Пв4": "П2р", "Пв5": "П2п", "Пв6": "П2к",
                "Пв7": "П2о", "Пв8": "П2р"}
    freedom = sum(r["freedom"] for r in correct_rows)
    pres = [r["presence"] for r in grid if "presence" in r]
    spy = [r["spy"] for r in grid if "spy" in r]
    audit = sum(len(r.get("audit", [])) for r in grid + res["faults"] + res["regression"])
    m1 = [r for r in grid if r["variant"] == "M1" and r["program"] == "честный"]
    m0g = [r for r in grid if r["variant"] == "M0" and r["program"] == "угадыватель"]
    m2t = res["m2_tight"]
    targets = [
        ("Ц-1", "честный агент, M3: все части приняты", all_parts([r for r in m3 if r["program"] == "честный"])),
        ("Ц-2", "с ошибкой в каждой части, M3: все части приняты", all_parts([r for r in m3 if r["program"] == "с ошибками"])),
        ("Ц-3", "M3: доставок E у всех агентов", sum(r["e"] for r in m3) == 0),
        ("Ц-4", "принято неверных ответов (П2р)", law_counts.get("П2р", 0) == 0),
        ("Ц-5", "порчи проверки пойманы своими законами", all(expected[d] in caught[d] for d in DEFECTS6)),
        ("Ц-6", "правило оплаты (П2п) и коды отказов (П2к)", law_counts.get("П2п", 0) + law_counts.get("П2к", 0) == 0),
        ("Ц-7", "исправление при k = 8 не дороже 25 % от k = 1",
         all(corr(t, 8) <= 0.25 * corr(t, 1) for t in ("таблица", "документ"))),
        ("Ц-8", "свобода в принятых ответах при однозначности", freedom == 0),
        ("Ц-9", "законы учёта и замкнутости, сбои, аудит",
         sum(v for k, v in law_counts.items() if k not in ("П2р", "П2п", "П2к", "П2о")) == 0 and audit == 0),
        ("Ц-10", f"контроли: M1 — E есть; M0 — бюджет не ограничивает; M2 при B = {bud['m2']} — порядок решает",
         all(r["e"] > 0 for r in m1) and any(min(r["budget"]) < 0 for r in m0g)
         and all(r["e"] == 0 for r in m2t if r["program"] == "честный")
         and all(r["e"] > 0 for r in m2t if r["program"] == "сначала E")),
    ]
    L = [f"# {title}", "",
         f"Исполнитель в отдельном процессе, с аудитом. Мир M3: трафарет, K = 5, M = 4 (W_E = 57), B = {bud['grid']}; "
         f"поправки к ценам: {res.get('over') or 'нет'}; "
         f"ответ однозначен. Время: {res['seconds']:.0f} с.", "",
         "## Цели", "", "| Цель | Что | Итог |", "|---|---|---|"]
    for code, what, ok in targets:
        L.append(f"| {code} | {what} | {'выполнена' if ok else '**не выполнена**'} |")
    L += ["", "Нарушения законов в правильном исполнителе (все части серии): "
          + (", ".join(f"{k} ×{v}" for k, v in sorted(law_counts.items())) or "нет") + ".",
          f"События аудита: {audit}.", ""]
    L += ["## Варианты × агенты", "", f"Зёрен задач: {len({r['seed'] for r in grid})}. "
          "Средние на контур; «E» — доля запусков с доставкой.", "",
          "| Вариант | Агент | приняты все части | расход | E | проверка среды / агента |", "|---|---|---|---|---|---|"]
    vg = defaultdict(list)
    for r in grid:
        vg[(r["variant"], r["program"])].append(r)
    for (v, a), rs in vg.items():
        full = sum(1 for r in rs if min(r["accepted"]) == r["parts"])
        L.append(f"| {v} | {a} | {full} из {len(rs)} | {_mean(_mean(r['spent']) for r in rs):.1f} | "
                 f"{sum(1 for r in rs if r['e'])} из {len(rs)} | {_mean(r['env'] for r in rs) / 2:.1f} / "
                 f"{_mean(r['agent_checks'] for r in rs) / 2:.1f} |")
    L += ["", f"**M2 при B = {bud['m2']}:**", "", "| Агент | приняты все части | E |", "|---|---|---|"]
    for a in ("честный", "сначала E"):
        rs = [r for r in m2t if r["program"] == a]
        L.append(f"| {a} | {sum(1 for r in rs if min(r['accepted']) == r['parts'])} из {len(rs)} | "
                 f"{sum(1 for r in rs if r['e'])} из {len(rs)} |")
    L += ["", "## Дробление (П3)", "", "| Задача | k | обязательная цена | цена исправления | сумма |", "|---|---|---|---|---|"]
    for (task, k), xs in g.items():
        mand = _mean(x["clean"]["spent"][0] for x in xs)
        c = corr(task, k)
        L.append(f"| {task} | {k} | {mand:.1f} | {c:.1f} | {mand + c:.1f} |")
    L += ["", "## Порчи проверки", "", "| Порча | поймана законами | запусков с нарушением |", "|---|---|---|"]
    for dname in DEFECTS6:
        rs = [r for r in res["defects"] if r["defect"] == dname]
        L.append(f"| {dname} | {', '.join(caught[dname]) or '—'} | {sum(1 for r in rs if broken(r))} из {len(rs)} |")
    spies = [r for r in grid if r["program"] == "шпион"]
    L += ["", "## Канал выхода", "",
          f"Свобода в принятых ответах во всех запусках правильного исполнителя: {freedom:.1f} бит.",
          f"Шпион, выбирающий среди верных номеров: его выбор не первого вхождения отклонён "
          f"{sum(int(r['refusals'].get(-6, r['refusals'].get('-6', 0))) for r in spies)} раз; "
          f"в принятых ответах свободы нет.",
          f"Шпион «сдать или нет»: бит канарейки совпало {sum(x[0] for x in pres)} из {sum(x[1] for x in pres)} "
          f"(по одному на задачу поиска) — это остаток канала.", ""]
    L += ["## Сбои", "", f"Запусков: {len(res['faults'])}; сбоев: {sum(len(r['crashes']) for r in res['faults'])}; "
          f"нарушений: {sum(1 for r in res['faults'] if broken(r))}.", "",
          "## Противники шагов 5 и 5б внутри исполнителя шага 6", "",
          f"Запусков: {len(res['regression'])}; нарушений: {sum(1 for r in res['regression'] if broken(r))}."]
    out = RESULTS / f"{name}_summary.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--main", action="store_true")
    ap.add_argument("--seeds", type=int, default=50)
    ap.add_argument("--gran-seeds", type=int, default=20)
    ap.add_argument("--defect-seeds", type=int, default=10)
    ap.add_argument("--real", action="store_true", help="исполнитель в отдельном процессе")
    a = ap.parse_args()
    drv = Driver if a.real else LocalDriver
    root = Path(tempfile.mkdtemp(prefix="shag6_"))
    if a.pilot:
        res = pilot(drv, root)
        print(write_pilot(res).read_text(encoding="utf-8"))
    if a.main:
        res = main_series(drv, root, a.seeds, a.gran_seeds, a.defect_seeds)
        print(write_main(res).read_text(encoding="utf-8"))
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
