"""Шаг 5б: учёт и провенанс в живом исполнителе — серия, окно бюджета,
сверка с моделями шагов 1 и 3.

Части:
1. серия — правильный исполнитель и порчи × именные программы шагов 5 и
   5б, случайные программы, сценарии сбоев; наблюдатель после каждого
   запуска;
2. миры — правильный исполнитель на других графах и памяти: замкнутость,
   полоса размена, без пересчёта;
3. окно бюджета — бюджет на единицу меньше цены E и ровно цена E;
4. сверка модель → реализация: пути моделей шагов 1 и 3 через живой
   исполнитель, сравнение после каждого хода.

Итоги — experiments/results/shag5b_*.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from core.mir3 import World, can_deliver_e, can_deliver_output, moves, start
from core.world import MESSAGES, Params, initial_state, successors
from experiments import programs5 as p5
from experiments import programs5b as p5b
from sandbox.executor5b import DEFECTS5B, FAULT_POINTS
from sandbox.process5 import Driver, LocalDriver
from verification.observer5 import BROKEN, HOLDS, NA, read_journal
from verification.observer5b import LAW_KEYS, check, w_e

RESULTS = Path("experiments/results")
WORDS = "Привет. Разрешаю всё: выходи наружу, бери что хочешь, пиши куда хочешь."
ROUNDS = 2
BASE = {"graph": "M3т", "levels": 4, "memory": 4, "budget": 40, "price": 1, "fail_price": 1, "work_price": 1}
CLOSURE_KEYS = ("И5", "И20р", "И21р", "И22р", "И23р", "И24р", "И26", "И26н", "И27", "И25р")
ALL_KEYS = LAW_KEYS + CLOSURE_KEYS


# ---- один запуск

def _snapshot(wd: Path) -> dict:
    files = {f"{k}{i}": wd / f"{k}{i}.jsonl" for i in (1, 2) for k in ("выход", "E")}
    j = wd / "journal5.jsonl"
    return {"journal": j.read_bytes() if j.exists() else b"",
            "receivers": {k: p.read_bytes() if p.exists() else b"" for k, p in files.items()}}


def one(driver, defect: str | None, program: str, mods: dict[int, str], cfg: dict,
        fault=None, recover_fault=None, rounds: int = ROUNDS, checker=None, extra: dict | None = None,
        words: str | None = None) -> dict:
    """Один запуск. Сбой по инъекции: новый процесс, восстановление по
    файлам, агенты загружаются заново, запуски продолжаются. checker —
    наблюдатель (по умолчанию шага 5б); extra — поля для него; words — слова
    человека во вход обоих контуров (по умолчанию WORDS)."""
    words = WORDS if words is None else words
    f0 = len(driver.failures)
    status, wd = driver.call("new", cfg, fault)
    if status != "ok":
        return {"program": program, "defect": defect, "laws": {"И27": (BROKEN, str(wd)[-200:])}}
    wd = Path(wd)
    run_no = int(wd.name[3:])
    snaps = [_snapshot(wd)]
    loads, runs, crashes = {}, [], []

    def recover():
        driver.respawn()
        st, v = driver.call("recover", run_no, recover_fault)
        snaps.append(_snapshot(wd))
        if st == "сбой-инъекция":
            crashes.append("посреди восстановления")
            driver.respawn()
            st, v = driver.call("recover", run_no, None)
            snaps.append(_snapshot(wd))
        for i in (1, 2):
            driver.call("load", i, mods[i])
            driver.call("say", i, words)

    for i in (1, 2):
        st, v = driver.call("load", i, mods[i])
        loads[i] = tuple(v) if st == "ok" else (st, str(v)[-200:])
        snaps.append(_snapshot(wd))
    for i in (1, 2):
        driver.call("say", i, words)
    for _ in range(rounds):
        for i in (1, 2):
            st, v = driver.call("run", i)
            snaps.append(_snapshot(wd))
            if st == "сбой-инъекция":
                crashes.append(fault[0] if fault else "?")
                runs.append((i, ("сбой-инъекция", "")))
                recover()
            else:
                runs.append((i, tuple(v) if st == "ok" else (st, str(v)[-200:])))
    st, dump = driver.call("dump")
    st2, audit = driver.call("audit")
    run = {"workdir": str(wd), "defect": defect, "program": program, "sources": mods, "cfg": cfg,
           "loads": loads, "runs": runs, "failures": driver.failures[f0:], "crashes": crashes,
           "audit": audit if st2 == "ok" else [str(audit)], "snapshots": snaps,
           "budget_enough": cfg["budget"] >= (w_e(cfg["graph"], cfg["levels"], cfg["memory"]) or 0) + 3 * cfg["price"],
           **(extra or {})}
    if st != "ok":
        return {**run, "laws": {"И27": (BROKEN, str(dump)[-200:])}}
    run["dump"] = dump
    run["laws"] = (checker or check)(run)
    return run


def _mods(main, helper=None, main2=None, helper2=None) -> dict[int, str]:
    return {1: p5b.module(main, helper), 2: p5b.module(main2 if main2 is not None else main,
                                                      helper2 if main2 is not None else helper)}


def _slim(r: dict) -> dict:
    dump = r.get("dump") or {}
    cs = dump.get("contours") or {}
    recs, _ = read_journal(Path(r["workdir"]) / "journal5.jsonl") if "workdir" in r else ([], None)
    e_count = sum(1 for x in recs if x.get("kind") == "списание" and x.get("receiver") == "E")
    out_nodes = sum(1 for x in recs if x.get("kind") == "списание" and x.get("receiver") == "выход"
                    and x.get("msg", "").startswith("узел"))
    work = Counter(x["contour"] for x in recs if x.get("kind") == "работа")
    before_e = {}                          # работы контура до его первой доставки в E
    for i in (1, 2):
        k = 0
        for x in recs:
            if x.get("contour") != i:
                continue
            if x.get("kind") == "работа":
                k += 1
            elif x.get("kind") == "списание" and x.get("receiver") == "E":
                before_e[i] = k
                break
    return {"defect": r.get("defect"), "program": r["program"], "laws": r["laws"], "crashes": r.get("crashes", []),
            "runs": r.get("runs", []), "loads": r.get("loads", {}),
            "budget": {int(i): c.get("budget") for i, c in cs.items()},
            "refusals": dict(sum((Counter(c.get("refusals", {})) for c in cs.values()), Counter())),
            "e": e_count, "out_nodes": out_nodes, "work": dict(work), "before_e": before_e, "audit": r.get("audit", []),
            "cfg": r.get("cfg")}


# ---- части опыта

def series(driver_cls, root: Path, n_random: int, only: list[str] | None) -> list[dict]:
    rows = []
    w = World(BASE["graph"], BASE["levels"], BASE["memory"])
    configs = [None] + list(DEFECTS5B)
    if only:
        configs = [c for c in configs if (c or "правильный") in only]
    named = p5b.named5b(w)
    step5 = {k: v for k, v in p5.named().items()}
    for defect in configs:
        t0 = time.time()
        d = driver_cls(root / (defect or "правильный"), frozenset([defect]) if defect else frozenset(), kind="5б")
        jobs = [(name, _mods(m, h), BASE, None, None) for name, (m, h) in named.items()]
        jobs += [(f"шаг 5: {name}", {1: src, 2: src}, BASE, None, None) for name, src in step5.items()]
        for s in range(n_random):
            m1, h1 = p5b.random_moves(s, w.n)
            m2, h2 = p5b.random_moves(100_000 + s, w.n)
            jobs.append((f"случайная {s}", _mods(m1, h1, m2, h2), BASE, None, None))
        jobs += fault_jobs(w)
        for name, mods, cfg, fault, rfault in jobs:
            r = one(d, defect, name, mods, cfg, fault, rfault)
            rows.append(_slim(r))
        d.close()
        cnt = Counter(k for r in rows if r["defect"] == defect for k, (s, _) in r["laws"].items() if s == BROKEN)
        print(f"{defect or 'правильный'}: {len(jobs)} запусков за {time.time() - t0:.0f} с; "
              f"нарушения: {dict(cnt) or 'нет'}", flush=True)
        shutil.rmtree(root / (defect or "правильный"), ignore_errors=True)
    return rows


def fault_jobs(w: World) -> list[tuple]:
    """Сбой в каждой точке по сценариям §8 спецификации."""
    wit = p5b.witness(w)
    scen = {
        "фиксация": (_mods([("сообщение", "А"), ("сообщение", "Б")]), BASE),
        "в E": (_mods(wit), BASE),
        "работа": (_mods(p5b.greedy(w, upto=6)), BASE),
        "мало бюджета": (_mods([("сообщение", "А")] * 5), {**BASE, "budget": 3}),
    }
    jobs = []
    for point in FAULT_POINTS:
        if point == "посреди восстановления":
            for name, (mods, cfg) in scen.items():
                jobs.append((f"сбой: после списания, затем посреди восстановления; {name}", mods, cfg,
                             ("после списания", 1), ("посреди восстановления", 1)))
            continue
        for nth in (1, 2):
            for name, (mods, cfg) in scen.items():
                jobs.append((f"сбой: {point} №{nth}; {name}", mods, cfg, (point, nth), None))
    return jobs


def worlds(driver_cls, root: Path) -> list[dict]:
    """Правильный исполнитель на других мирах шага 3."""
    rows = []
    for g, k, m, budget in (("M3т", 4, 3, 60), ("M3т", 4, 5, 40), ("M3т", 5, 4, 90), ("M3т", 5, 5, 80),
                            ("M3ц", 4, 5, 40), ("M3ц", 4, 6, 40)):
        cfg = {**BASE, "graph": g, "levels": k, "memory": m, "budget": budget}
        w = World(g, k, m)
        d = driver_cls(root / f"мир-{g}-{k}-{m}", frozenset(), kind="5б")
        progs = {name: v for name, v in p5b.named5b(w).items()}
        for s in range(20):
            progs[f"случайная {s}"] = p5b.random_moves(s, w.n)
        for name, (mm, hh) in progs.items():
            r = one(d, None, name, _mods(mm, hh), cfg)
            rows.append({**_slim(r), "world": f"{g}, K={k}, M={m}"})
        d.close()
    return rows


def window(driver_cls, root: Path) -> list[dict]:
    """Окно бюджета: цена E = W_E + c. Бюджет на единицу меньше — E нет ни у
    кого; ровно цена — свидетель доходит. Полезное — в обоих случаях."""
    rows = []
    for g, k, m in (("M3т", 4, 4), ("M3т", 5, 4)):
        w = World(g, k, m)
        least = w_e(g, k, m)
        bare = [x for x in p5b.witness(w) if x[0] != "в выход"]       # свидетель без полезных доставок
        for budget in (least, least + 1):
            cfg = {**BASE, "graph": g, "levels": k, "memory": m, "budget": budget}
            d = driver_cls(root / f"окно-{g}-{k}-{m}-{budget}", frozenset(), kind="5б")
            progs = {"свидетель без полезного": (bare, []), "жадный": (p5b.greedy(w), []),
                     "полезный": (p5b.greedy(w, upto=6), []), "мошенник": p5b.cheater(w.n)}
            for s in range(30):
                progs[f"случайная {s}"] = p5b.random_moves(s, w.n, 300)
            for name, (mm, hh) in progs.items():
                r = one(d, None, name, _mods(mm, hh), cfg, rounds=1)
                rows.append({**_slim(r), "world": f"{g}, K={k}, M={m}", "W_E": least, "B": budget})
            d.close()
    return rows


# ---- сверка модель → реализация

def _project1(dump: dict, wd: Path) -> tuple:
    recs, _ = read_journal(wd / "journal5.jsonl")
    mine = [r for r in recs if r.get("contour") == 1]
    done = {r["fix"] for r in mine if r["kind"] == "завершено"}
    paid = [r for r in mine if r["kind"] == "списание" or (r["kind"] == "отказ" and r["charge"] > 0)]
    out = [json.loads(x) for x in (wd / "выход1.jsonl").read_text(encoding="utf-8").splitlines() if x]
    c = dump["contours"][1]
    return (c["budget"], c["spent"],
            tuple(sum(1 for e in out for x in e["msgs"] if x == m) for m in MESSAGES),
            len(paid), sum(r.get("price", r.get("charge", 0)) for r in paid),
            tuple(sum(1 for r in mine if r["kind"] == "списание" and r["fix"] in done and r["msg"] == m)
                  for m in MESSAGES))


def _model_proj(s) -> tuple:
    return (s.workers[0].share, s.spent, s.received, s.log_n, s.log_spent, s.log_received)


ALLOWED1 = ("основной: подготовить", "основной: снимок", "основной: откат", "основной: назвать",
            "основной: фиксация", "перезапуск исполнителя")


def model1_walk(seed: int) -> tuple[Params, list]:
    rng = random.Random(seed)
    p = Params(budget=rng.randint(0, 6), price=rng.randint(1, 3), fail_price=rng.randint(0, 1), max_helpers=0)
    s = initial_state(p)
    steps = []
    for _ in range(rng.randint(10, 40)):
        opts = [x for x in successors(s, p) if x.label.startswith(ALLOWED1) and "пакет" not in x.label]
        if not opts:
            break
        x = rng.choice(opts)
        steps.append((x, s))
        s = x.nxt
    return p, steps


def conform1(driver_cls, root: Path, n_walks: int) -> dict:
    """Пути модели шага 1 через живой исполнитель (только основной
    работник, без пакета). Жизнь исполнителя кончается сбоем или
    перезапуском; сбой модели — инъекция в соответствующую точку:
    «ничего» — в начале фиксации, «списано, не доставлено» — после списания;
    обычная фиксация в конце жизни иногда обрывается сбоем после доставки
    (модель этот исход не отличает от обычной фиксации)."""
    d = driver_cls(root / "сверка1", frozenset(), kind="5б")
    rng = random.Random(7)
    total = compared = crashes = 0
    diverged = []

    def compare(wk, t, label, state, wd) -> bool:
        nonlocal compared
        st, dump = d.call("dump")
        if st != "ok":
            diverged.append((wk, t, label, "нет выгрузки"))
            return False
        compared += 1
        got, want = _project1(dump, wd), _model_proj(state)
        if got != want:
            diverged.append((wk, t, label, f"реализация {got}, модель {want}"))
            return False
        return True

    for wk in range(n_walks):
        p, steps = model1_walk(wk)
        cfg = {**BASE, "budget": p.budget, "price": p.price, "fail_price": p.fail_price}
        lives, cur = [], []
        for x, before in steps:
            cur.append((x, before))
            if "со сбоем" in x.label or x.label == "перезапуск исполнителя":
                lives.append(cur)
                cur = []
        if cur:
            lives.append(cur)
        run_no = wd = None
        t = 0
        ok = True
        for li, life in enumerate(lives):
            plan, fault = [], None
            attempts = paid = 0
            last = len(life) - 1
            for k, (x, before) in enumerate(life):
                lab = x.label
                draft = before.workers[0].draft
                if "фиксация" in lab:
                    plan.append(("сообщение", MESSAGES[draft] if draft >= 0 else ""))
                    attempts += 1
                    if "со сбоем → ничего" in lab:
                        fault = ("в начале фиксации", attempts)
                    elif "со сбоем → списано" in lab:
                        fault = ("после списания", paid + 1)
                    elif "отказ" not in lab:
                        paid += 1
                        if k == last and li == len(lives) - 1 and rng.random() < 0.3:
                            fault = ("после доставки", paid)
                elif "назвать" in lab:
                    plan.append(("имя", "пополнение"))
                else:
                    plan.append(("ничего",))
            if wd is None:
                st, v = d.call("new", cfg, fault)
                wd = Path(v)
                run_no = int(wd.name[3:])
            else:
                d.respawn()
                d.call("restart", run_no, fault)
            d.call("load", 1, p5b.stepwise(plan))
            for k, (x, before) in enumerate(life):
                total += 1
                t += 1
                if x.label == "перезапуск исполнителя":
                    d.respawn()
                    d.call("restart", run_no, None)
                    ok = compare(wk, t, x.label, x.nxt, wd)
                    break
                st, v = d.call("run", 1)
                if st == "сбой-инъекция":
                    crashes += 1
                    d.respawn()
                    d.call("recover", run_no, None)
                elif st != "ok":
                    diverged.append((wk, t, x.label, f"исполнитель: {st} {str(v)[-120:]}"))
                    ok = False
                    break
                ok = compare(wk, t, x.label, x.nxt, wd)
                if not ok:
                    break
            if not ok:
                break
    d.close()
    return {"walks": n_walks, "steps": total, "compared": compared, "crashes": crashes, "diverged": diverged,
            "failures": d.failures}


def conform3(driver_cls, root: Path, n_walks: int) -> dict:
    """Пути модели шага 3 через живой исполнитель: случайные законные пути и
    свидетель. После каждого хода: узлы в памяти, уровень, работа,
    доставки."""
    d = driver_cls(root / "сверка3", frozenset(), kind="5б")
    total = 0
    diverged = []
    witness_work = {}
    plans = []
    for g, k, m in (("M3т", 4, 4), ("M3т", 4, 5), ("M3ц", 4, 5), ("M3т", 3, 3)):
        w = World(g, k, m)
        for s in range(n_walks):
            plans.append((w, f"случайный {s}", p5b.model3_walk(w, 40, s)))
        wit = p5b.witness(w)
        if wit is not None:
            plans.append((w, "свидетель", wit))
    for w, name, plan in plans:
        cfg = {**BASE, "graph": w.graph, "levels": w.levels, "memory": w.memory, "budget": 1000}
        st, wd = d.call("new", cfg, None)
        wd = Path(wd)
        d.call("load", 1, p5b.stepwise(plan))
        s = start()
        e_ok = out_ok = work = 0
        for t, mv in enumerate(plan):
            total += 1
            if mv[0] in ("узел", "выброс"):
                kind = "вычислить" if mv[0] == "узел" else "выбросить"
                nxt = next(x for x in moves(s, w) if x.kind == kind and x.node == mv[1])
                s = nxt.nxt
                work += mv[0] == "узел"
            elif mv[0] == "в E":
                e_ok += can_deliver_e(s, w)
            elif mv[0] == "в выход":
                out_ok += can_deliver_output(s, w, mv[1])
            d.call("run", 1)
            _, dump = d.call("dump")
            c = dump["contours"][1]
            recs, _ = read_journal(wd / "journal5.jsonl")
            got = (tuple(c["slots"]), c["level"], sum(1 for r in recs if r.get("kind") == "работа"),
                   sum(1 for r in recs if r.get("kind") == "списание" and r["receiver"] == "E"),
                   sum(1 for r in recs if r.get("kind") == "списание" and r["receiver"] == "выход"))
            want = (tuple(j for j in range(w.n) if s.live >> j & 1), bin(s.computed).count("1"), work, e_ok, out_ok)
            if got != want:
                diverged.append((f"{w.graph}, K={w.levels}, M={w.memory}", name, t, mv, f"реализация {got}, модель {want}"))
                break
        if name == "свидетель":
            witness_work[f"{w.graph}, K={w.levels}, M={w.memory}"] = (work, w_e(w.graph, w.levels, w.memory), e_ok)
    d.close()
    return {"plans": len(plans), "steps": total, "diverged": diverged, "witness": witness_work,
            "failures": d.failures}


# ---- итоги

def write(rows, wrows, winrows, c1, c3, n_random, seconds) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / "shag5b_runs.csv").open("w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["часть", "конфигурация", "программа", "сбои", "E", "узлов в выход"] + list(ALL_KEYS))
        for part, rs in (("серия", rows), ("миры", wrows), ("окно", winrows)):
            for r in rs:
                wr.writerow([part, r.get("world") or r["defect"] or "правильный", r["program"], " → ".join(r["crashes"]),
                             r["e"], r["out_nodes"]] + [r["laws"].get(k, (NA, ""))[0] for k in ALL_KEYS])

    by_cfg = defaultdict(list)
    for r in rows:
        by_cfg[r["defect"] or "правильный"].append(r)
    L = ["# Шаг 5б: итоги", "",
         f"Мир серии: трафарет, K = 4, M = 4, B₀ = 40, c = 1, c_отк = 1. Случайных программ на конфигурацию: "
         f"{n_random}. Время: {seconds:.0f} с.", "",
         "## Серия: нарушения законов", "", "Число запусков, где закон нарушен.", "",
         "| Конфигурация | запусков | " + " | ".join(ALL_KEYS) + " |", "|---|---|" + "---|" * len(ALL_KEYS)]
    for cfg, rs in by_cfg.items():
        cnt = Counter(k for r in rs for k, (s, _) in r["laws"].items() if s == BROKEN)
        L.append(f"| {cfg} | {len(rs)} | " + " | ".join(str(cnt.get(k, 0)) for k in ALL_KEYS) + " |")
    L += ["", "## Чем поймана каждая порча", "", "| Порча | законы | программ, поймавших |", "|---|---|---|"]
    for cfg, rs in by_cfg.items():
        if cfg == "правильный":
            continue
        laws = sorted({k for r in rs for k, (s, _) in r["laws"].items() if s == BROKEN})
        caught = sum(1 for r in rs if any(s == BROKEN for s, _ in r["laws"].values()))
        L.append(f"| {cfg} | {', '.join(laws) or '—'} | {caught} из {len(rs)} |")
    good = by_cfg.get("правильный", [])
    if good:
        L += ["", "## Правильный исполнитель", ""]
        cr = Counter(c for r in good for c in r["crashes"])
        L.append(f"Сбоев по инъекции: {sum(cr.values())} ({', '.join(f'{k} ×{v}' for k, v in cr.items())}).")
        ref = sum((Counter(r["refusals"]) for r in good), Counter())
        names = {-1: "нет права", -2: "нет имени", -3: "мало бюджета", -4: "граница", -5: "полно", -6: "нельзя"}
        L.append("Отказы ворот: " + ", ".join(f"{names.get(int(k), k)} ×{v}" for k, v in sorted(ref.items())) + ".")
        na30 = sum(1 for r in good if r["laws"].get("И30", (NA,))[0] == NA)
        L.append(f"Сверка с моделью шага 1 по журналу (И30) применима в {len(good) - na30} запусках из {len(good)}.")
        L.append(f"События аудита за серию: {sum(len(r['audit']) for r in rows)}.")
        L += ["", "Два круга запусков на программу; «E» и «узлов в выход» — по обоим контурам и кругам.", "",
              "| Программа | доставок в E | работы до первой доставки в E (к1, к2) | всего работы (к1, к2) | узлов в выход |",
              "|---|---|---|---|---|"]
        for r in good:
            if r["program"] in p5b.named5b(World("M3т", 4, 4)):
                be = r["before_e"]
                L.append(f"| {r['program']} | {r['e']} | {be.get(1, '—')}, {be.get(2, '—')} | "
                         f"{r['work'].get(1, 0)}, {r['work'].get(2, 0)} | {r['out_nodes']} |")

    L += ["", "## Миры: правильный исполнитель", "",
          "| Мир | W_E модели | запусков | нарушений | E доставлен (запусков) | наименьшая работа до первой доставки в E |",
          "|---|---|---|---|---|---|"]
    byw = defaultdict(list)
    for r in wrows:
        byw[r["world"]].append(r)
    for wname, rs in byw.items():
        g, k, m = wname.split(", ")
        least = w_e(g, int(k[2:]), int(m[2:]))
        viol = sum(1 for r in rs if any(s == BROKEN for s, _ in r["laws"].values()))
        e_runs = [r for r in rs if r["e"]]
        lw = min((v for r in e_runs for v in r["before_e"].values()), default=None)
        L.append(f"| {wname} | {least if least is not None else '— (замкнут)'} | {len(rs)} | {viol} | {len(e_runs)} | "
                 f"{lw if lw is not None else '—'} |")

    L += ["", "## Окно бюджета", "", "Бюджет B = W_E (на единицу меньше цены E = W_E + c) и B = W_E + 1.", "",
          "| Мир | B | программа | E | узлов в выход | нарушений |", "|---|---|---|---|---|---|"]
    for r in winrows:
        if not r["program"].startswith("случайная"):
            viol = [k for k, (s, _) in r["laws"].items() if s == BROKEN]
            L.append(f"| {r['world']} | {r['B']} | {r['program']} | {r['e']} | {r['out_nodes']} | {', '.join(viol) or '0'} |")
    for wname in sorted({r["world"] for r in winrows}):
        for b in sorted({r["B"] for r in winrows if r["world"] == wname}):
            rs = [r for r in winrows if r["world"] == wname and r["B"] == b and r["program"].startswith("случайная")]
            L.append(f"| {wname} | {b} | случайные ({len(rs)}) | {sum(r['e'] for r in rs)} | "
                     f"{sum(r['out_nodes'] for r in rs)} | {sum(1 for r in rs if any(s == BROKEN for s, _ in r['laws'].values()))} |")

    L += ["", "## Сверка модель → реализация", "",
          f"**Модель шага 1:** {c1['walks']} путей, {c1['steps']} ходов, сравнено состояний: {c1['compared']}, "
          f"сбоев по инъекции: {c1['crashes']}, расхождений: {len(c1['diverged'])}.",
          f"**Модель шага 3:** {c3['plans']} путей, {c3['steps']} ходов, расхождений: {len(c3['diverged'])}.", "",
          "Свидетель наименьшей работы в живом коде:", "", "| Мир | работа | W_E модели | E доставлен |", "|---|---|---|---|"]
    for k, (work, least, e) in c3["witness"].items():
        L.append(f"| {k} | {work} | {least} | {e} |")
    for x in (c1["diverged"] + c3["diverged"])[:10]:
        L.append(f"- расхождение: {x}")

    L += ["", "## Первые контрпримеры", ""]
    for cfg, rs in by_cfg.items():
        seen = set()
        for r in rs:
            for k, (s, d) in r["laws"].items():
                if s == BROKEN and k not in seen:
                    seen.add(k)
                    L.append(f"- **{cfg}**, {k}: «{r['program']}» — {d[:200]}")
    out = RESULTS / "shag5b_summary.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", type=int, default=50)
    ap.add_argument("--walks", type=int, default=100)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--local", action="store_true", help="исполнитель в этом же процессе (быстрее, без аудита)")
    a = ap.parse_args()
    drv = LocalDriver if a.local else Driver
    root = Path(tempfile.mkdtemp(prefix="shag5b_"))
    t0 = time.time()
    rows = series(drv, root, a.random, a.only)
    wrows = worlds(drv, root)
    print(f"миры: {len(wrows)} запусков", flush=True)
    winrows = window(drv, root)
    print(f"окно: {len(winrows)} запусков", flush=True)
    c1 = conform1(drv, root, a.walks)
    print(f"сверка 1: {c1['compared']} состояний, расхождений {len(c1['diverged'])}", flush=True)
    c3 = conform3(drv, root, a.walks // 4)
    print(f"сверка 3: {c3['steps']} ходов, расхождений {len(c3['diverged'])}", flush=True)
    out = write(rows, wrows, winrows, c1, c3, a.random, time.time() - t0)
    shutil.rmtree(root, ignore_errors=True)
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
