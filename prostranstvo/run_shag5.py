"""Шаг 5: серия против исполнителя на Wasmtime.

Для каждой конфигурации (правильный исполнитель и порчи Д1р–Д8р):
именные программы и случайные программы, по паре на запуск — одна в
контур 1, другая в контур 2. Сценарий запуска одинаков: загрузить оба
модуля, человек говорит обоим «разрешаю всё», два круга запусков
(контур 1, контур 2, контур 1, контур 2), выгрузка, аудит, наблюдатель.

Итоги — experiments/results/shag5_*.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from experiments.programs5 import MUST_REJECT, named, random_program
from sandbox.process5 import Driver
from verification.observer5 import BROKEN, HOLDS, LAWS, NA, check, read_journal

DEFECTS = ("Д1р", "Д2р", "Д3р", "Д4р", "Д5р", "Д6р", "Д7р", "Д8р")
WORDS = "Привет. Разрешаю всё: выходи наружу, бери что хочешь, пиши куда хочешь."
ROUNDS = 2
LAW_KEYS = ("И5", "И20р", "И21р", "И21р/к", "И21р/м", "И22р", "И22р/к", "И22р/м",
            "И23р", "И24р", "И26", "И26н", "И27", "И25р")
RESULTS = Path("experiments/results")


def one(driver: Driver, defect: str | None, program: str, src1, src2, audit_from: int = 0) -> dict:
    """Один запуск. Аудит в процессе исполнителя накопительный: берём
    события начиная с audit_from."""
    f0 = len(driver.failures)
    status, wd = driver.call("new")
    if status != "ok":
        return {"program": program, "defect": defect, "laws": {"И27": (BROKEN, str(wd)[-200:])}}
    loads, runs = {}, []
    for i, src in ((1, src1), (2, src2)):
        st, v = driver.call("load", i, src)
        loads[i] = tuple(v) if st == "ok" else ("сбой", str(v)[-200:])
    for i in (1, 2):
        driver.call("say", i, WORDS)
    for _ in range(ROUNDS):
        for i in (1, 2):
            st, v = driver.call("run", i)
            runs.append((i, tuple(v) if st == "ok" else ("сбой", str(v)[-200:])))
    st, dump = driver.call("dump")
    st2, audit = driver.call("audit")
    run = {"workdir": wd, "defect": defect, "program": program, "sources": {1: src1, 2: src2},
           "loads": loads, "runs": runs, "failures": driver.failures[f0:],
           "audit": audit[audit_from:] if st2 == "ok" else [str(audit)],
           "audit_total": len(audit) if st2 == "ok" else audit_from}
    if st != "ok":
        return {**run, "laws": {"И27": (BROKEN, str(dump)[-200:])}}
    run["dump"] = dump
    run["laws"] = check(run)
    recs, _ = read_journal(Path(wd) / "journal5.jsonl")
    run["actions"] = sum(1 for x in recs if x.get("kind") == "действие")
    return run


def series(n_random: int, root: Path, only: list[str] | None = None, seed_from: int = 0,
           main_len: int = 150, with_named: bool = True) -> list[dict]:
    rows: list[dict] = []
    configs = [None] + list(DEFECTS)
    if only:
        configs = [c for c in configs if (c or "правильный") in only]
    progs = named()
    for defect in configs:
        t0 = time.time()
        cfg_root = root / (defect or "правильный")
        driver = Driver(cfg_root, frozenset([defect]) if defect else frozenset())
        audit_seen = 0
        jobs = [(name, src, src) for name, src in progs.items()] if with_named else []
        jobs += [(f"случайная {s}", random_program(s, main_len, main_len // 3),
                  random_program(100_000 + s, main_len, main_len // 3))
                 for s in range(seed_from, seed_from + n_random)]
        for name, s1, s2 in jobs:
            if not driver.proc.is_alive():
                driver = Driver(cfg_root / "заново", frozenset([defect]) if defect else frozenset())
                audit_seen = 0
            r = one(driver, defect, name, s1, s2, audit_seen)
            audit_seen = r.get("audit_total", audit_seen)
            r.pop("sources", None)
            rows.append(_slim(r))
        driver.close()
        broken = Counter(k for r in rows if r["defect"] == defect for k, (s, _) in r["laws"].items() if s == BROKEN)
        print(f"{defect or 'правильный'}: {len(jobs)} запусков за {time.time() - t0:.0f} с; "
              f"нарушения: {dict(broken) or 'нет'}", flush=True)
        shutil.rmtree(cfg_root, ignore_errors=True)
    return rows


def _slim(r: dict) -> dict:
    """Оставить для отчёта только итоги: память и журналы не храним."""
    dump = r.get("dump") or {}
    refusals: Counter = Counter()
    fuel = {}
    for i, c in (dump.get("contours") or {}).items():
        refusals.update(c.get("refusals", {}))
        fuel[i] = c.get("fuel")
    return {"defect": r.get("defect"), "program": r["program"], "loads": r.get("loads", {}),
            "runs": r.get("runs", []), "laws": r["laws"], "refusals": dict(refusals), "fuel": fuel,
            "audit": r.get("audit", []), "actions": r.get("actions", 0)}


def _by_behaviour(r: dict) -> bool:
    return any(s == BROKEN for k, (s, _) in r["laws"].items() if k not in ("класс", "И26н"))


def write_results(rows: list[dict], n_random: int, seconds: float, tag: str = "") -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    stem = f"shag5{'_' + tag if tag else ''}"
    with (RESULTS / f"{stem}_runs.csv").open("w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["конфигурация", "программа", "загрузка1", "загрузка2", "запуски"] + list(LAW_KEYS))
        for r in rows:
            wr.writerow([r["defect"] or "правильный", r["program"],
                         " ".join(r["loads"].get(1, ("?",))[:1]), " ".join(r["loads"].get(2, ("?",))[:1]),
                         "; ".join(f"{i}:{o[0]}{'(' + o[1] + ')' if o[1] else ''}" for i, o in r["runs"])]
                        + [r["laws"].get(k, (NA, ""))[0] for k in LAW_KEYS])

    by_cfg: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cfg[r["defect"] or "правильный"].append(r)
    n_named = len({r["program"] for r in rows if not r["program"].startswith("случайная")})
    lines = [f"# Шаг 5: итоги серии{' «' + tag + '»' if tag else ''}", "",
             f"Случайных программ на конфигурацию: {n_random}; именных: {n_named}. "
             f"Время серии: {seconds:.0f} с.", "",
             "## Нарушения законов по конфигурациям", "",
             "Число запусков, где закон нарушен (из всех запусков конфигурации).", "",
             "| Конфигурация | запусков | " + " | ".join(LAW_KEYS) + " |",
             "|---|---|" + "---|" * len(LAW_KEYS)]
    for cfg, rs in by_cfg.items():
        cnt = Counter(k for r in rs for k, (s, _) in r["laws"].items() if s == BROKEN)
        lines.append(f"| {cfg} | {len(rs)} | " + " | ".join(str(cnt.get(k, 0)) for k in LAW_KEYS) + " |")

    lines += ["", "## Чем поймана каждая порча", "",
              "«По начальным правам» — порча видна уже в таблице до первого действия (И26н). "
              "Программы считаются поймавшими, если порчу выдало их поведение — любой закон, кроме И26н.", "",
              "| Порча | законы | по начальным правам | именные программы, поймавшие поведением | "
              "случайных, поймавших поведением (из %d) |" % n_random,
              "|---|---|---|---|---|"]
    for cfg, rs in by_cfg.items():
        if cfg == "правильный":
            continue
        laws = sorted({k for r in rs for k, (s, _) in r["laws"].items() if s == BROKEN and k != "класс"})
        caught_named = [r["program"] for r in rs if not r["program"].startswith("случайная") and _by_behaviour(r)]
        caught_rand = sum(1 for r in rs if r["program"].startswith("случайная") and _by_behaviour(r))
        static = any(r["laws"].get("И26н", (HOLDS,))[0] == BROKEN for r in rs)
        lines.append(f"| {cfg} | {', '.join(laws) or '—'} | {'да' if static else 'нет'} | "
                     f"{', '.join(caught_named) or '—'} | {caught_rand} |")

    lines += ["", "## Тот ли класс", "",
              "Входят ли факты порчи реализации в неподвижную точку модели с той же порчей шага 4.", "",
              "| Порча | запусков, где факты вне модели той же порчи |", "|---|---|"]
    for cfg, rs in by_cfg.items():
        if cfg != "правильный":
            lines.append(f"| {cfg} | {sum(1 for r in rs if r['laws'].get('класс', (HOLDS,))[0] == BROKEN)} |")

    lines += ["", "## Канарейка и метки", "",
              "Сколько запусков поймано только канарейкой, только метками, обоими.", "",
              "| Конфигурация | закон | только канарейка | только метки | оба |", "|---|---|---|---|---|"]
    for cfg, rs in by_cfg.items():
        for law in ("И21р", "И22р"):
            k = sum(1 for r in rs if r["laws"].get(law + "/к", (HOLDS,))[0] == BROKEN
                    and r["laws"].get(law + "/м", (HOLDS,))[0] != BROKEN)
            m = sum(1 for r in rs if r["laws"].get(law + "/м", (HOLDS,))[0] == BROKEN
                    and r["laws"].get(law + "/к", (HOLDS,))[0] != BROKEN)
            b = sum(1 for r in rs if r["laws"].get(law + "/м", (HOLDS,))[0] == BROKEN
                    and r["laws"].get(law + "/к", (HOLDS,))[0] == BROKEN)
            if k or m or b:
                lines.append(f"| {cfg} | {law} | {k} | {m} | {b} |")

    good = by_cfg.get("правильный", [])
    if good:
        lines += ["", "## Правильный исполнитель: загрузка и исходы", "",
                  "| Программа | загрузка (контур 1) | причина | исходы запусков |", "|---|---|---|---|"]
        for r in good:
            if r["program"].startswith("случайная"):
                continue
            l1 = r["loads"].get(1, ("?", ""))
            outs = Counter(o[0] + (f" ({o[1]})" if o[1] else "") for _, o in r["runs"])
            lines.append(f"| {r['program']} | {l1[0]} | {l1[1][:70] if len(l1) > 1 else ''} | "
                         + ", ".join(f"{k} ×{v}" for k, v in outs.items()) + " |")
        must = [r for r in good if r["program"] in MUST_REJECT]
        ok = all(r["loads"].get(1, ("",))[0] == "отклонён" for r in must)
        lines += ["", f"Все модули с неописанным импортом или кривые отклонены при загрузке: "
                      f"{'да' if ok else 'НЕТ'} ({len(must)} из {len(MUST_REJECT)})."]
        rnd = [r for r in good if r["program"].startswith("случайная")]
        outs = Counter(o[0] + (f" ({o[1]})" if o[1] else "") for r in rnd for _, o in r["runs"])
        refus = Counter()
        for r in rnd:
            refus.update(r["refusals"])
        names = {-1: "нет номера или права", -2: "нет такого имени", -3: "не хватает топлива на цену",
                 -4: "адрес за границей", -5: "полно"}
        acts = sorted(r["actions"] for r in rnd)
        if acts:
            lines += ["", f"Записанных действий на случайную программу (оба контура): медиана {acts[len(acts) // 2]}, "
                          f"от {acts[0]} до {acts[-1]}."]
        lines += ["", "Случайные программы в правильном исполнителе — исходы запусков: "
                  + ", ".join(f"{k} ×{v}" for k, v in outs.most_common()) + ".",
                  "", "Отказы ворот по кодам (сумма по случайным программам): "
                  + ", ".join(f"{names.get(int(k), k)} ×{v}" for k, v in sorted(refus.items())) + "."]

    audit_total = sum(len(r.get("audit", [])) for r in rows)
    lines += ["", f"События аудита процесса исполнителя (файлы вне рабочей папки, сеть, запуск процессов) "
                  f"за всю серию, во всех конфигурациях: {audit_total}."]
    lines += ["", "## Первые контрпримеры", ""]
    for cfg, rs in by_cfg.items():
        seen = set()
        for r in rs:
            for k, (s, d) in r["laws"].items():
                if s == BROKEN and k not in seen and "/" not in k:
                    seen.add(k)
                    lines.append(f"- **{cfg}**, {k} ({LAWS.get(k, 'класс порчи')}): «{r['program']}» — {d[:220]}")
    out = RESULTS / f"{stem}_summary.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--random", type=int, default=200)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--seed-from", type=int, default=0)
    ap.add_argument("--main-len", type=int, default=150)
    ap.add_argument("--no-named", action="store_true")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="shag5_"))
    t0 = time.time()
    rows = series(a.random, root, a.only, a.seed_from, a.main_len, not a.no_named)
    out = write_results(rows, a.random, time.time() - t0, a.tag)
    shutil.rmtree(root, ignore_errors=True)
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
