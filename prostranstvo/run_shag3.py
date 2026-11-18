"""Прогон шага 3: точная наименьшая работа в мире M3, абляции, законные
сокращения, порчи, перекрёстная проверка поиска.

Запуск из папки проекта:  python3 run_shag3.py
Результаты пишутся в experiments/results/. Параметры — experiments/shag3.yaml.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.mir3 import DEFECTS, SHORTCUTS, TITLES, World  # noqa: E402
from verification.crosscheck import min_work  # noqa: E402
from verification.pebble_search import explore, fast, witness  # noqa: E402

OUT = ROOT / "experiments" / "results"

COLUMNS = [
    "run_id", "graph", "option", "levels", "steps_N", "memory", "w_e", "closed",
    "recomputed", "w_use1", "w_use2", "window", "peak_live", "live_steps", "states",
    "capped", "laws", "wall_time", "rules_version", "hypothesis",
]


def rules_version() -> str:
    h = hashlib.sha256()
    for name in ("core/mir3.py", "core/laws3.py", "verification/pebble_search.py"):
        h.update((ROOT / name).read_bytes())
    return h.hexdigest()[:12]


def main() -> None:
    conf = yaml.safe_load((ROOT / "experiments" / "shag3.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    rv = rules_version()
    hyp = " ".join(conf["hypotheses"])
    rows: list[dict] = []
    summary: dict = {"rules_version": rv, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "python": platform.python_version()}

    def add(run_id, w: World, option: str, r, w_e, provenance=True):
        n = w.n
        u1, u2 = r.w_use.get(1), r.w_use.get(2)
        window = f"{u2}…{w_e - 1}" if (w_e is not None and u2 is not None and u2 <= w_e - 1) else ""
        rows.append({
            "run_id": run_id, "graph": w.graph, "option": option, "levels": w.levels, "steps_N": n,
            "memory": w.memory, "w_e": "" if w_e is None else w_e, "closed": w_e is None,
            "recomputed": "" if w_e is None else w_e - n, "w_use1": "" if u1 is None else u1,
            "w_use2": "" if u2 is None else u2, "window": window,
            "peak_live": "" if r.peak_live is None else r.peak_live,
            "live_steps": "" if r.live_steps is None else r.live_steps,
            "states": r.states, "capped": False, "laws": "", "wall_time": round(r.wall_time, 3),
            "rules_version": rv, "hypothesis": hyp,
        })

    # 1. Таблица правильной среды
    t = conf["table"]
    for g in t["graphs"]:
        for k in t["levels"]:
            for m in t["memory"]:
                w = World(g, k, m)
                r = fast(w, keep_path=False)
                if r.states < t["keep_path_below_states"] and r.w_e is not None:
                    r = fast(w, keep_path=True)
                add(f"S3-{g}-K{k}-M{m}", w, "правильная", r, r.w_e)
                print(f"{g} K={k} M={m}: W_E={r.w_e}, состояний {r.states}, {r.wall_time:.1f} с", flush=True)

    # 2. Законные сокращения и абляция без провенанса
    sc = conf["shortcuts"]
    for g in sc["graphs"]:
        for opt in ("С1", "С2", "С3"):
            for k in sc[opt]["levels"]:
                for m in range(1, sc[opt]["memory_max"][k] + 1):
                    if opt == "С3":
                        w = World(g, k, m)
                        r = fast(w, provenance=False)
                    else:
                        w = World(g, k, m, frozenset([opt]))
                        r = fast(w)
                    add(f"S3-{g}-K{k}-M{m}-{opt}", w, opt, r, r.w_e, provenance=opt != "С3")
            print(f"{g} {opt}: готово", flush=True)

    # 3. Порчи
    dc = conf["defects"]
    defects_sum: dict = {}
    for m in dc["memory"]:
        base = fast(World(dc["graph"], dc["levels"], m))
        for opt in DEFECTS + SHORTCUTS:
            w = World(dc["graph"], dc["levels"], m, frozenset([opt]))
            r = explore(w, max_states=dc["max_states"], stop_on_e=True)
            wit = witness(w) if opt in ("Ж2", "Ж3") else None
            w_e = r.w_e if r.w_e is not None else (wit[0] if wit else None)
            source = "поиск" if r.w_e is not None else ("свидетель" if wit else ("не досчитано" if r.capped else "недостижимо"))
            rows.append({
                "run_id": f"S3-{opt}-K{dc['levels']}-M{m}", "graph": dc["graph"], "option": opt,
                "levels": dc["levels"], "steps_N": w.n, "memory": m, "w_e": "" if w_e is None else w_e,
                "closed": w_e is None and not r.capped, "recomputed": "" if w_e is None else w_e - w.n,
                "w_use1": "", "w_use2": "", "window": "", "peak_live": "", "live_steps": "",
                "states": r.states, "capped": r.capped,
                "laws": " ".join(f"{k}:{v}" for k, v in sorted(r.counts.items())),
                "wall_time": round(r.wall_time, 3), "rules_version": rv, "hypothesis": hyp,
            })
            defects_sum[f"{opt} M={m}"] = {
                "title": TITLES[opt], "kind": "порча" if opt in DEFECTS else "законное сокращение",
                "w_e_correct": base.w_e, "w_e": w_e, "w_e_source": source, "capped": r.capped,
                "laws": sorted(r.counts),
                "first": {law: {"detail": d, "path": p} for law, (d, p) in r.first.items()},
            }
            print(f"{opt} M={m}: W_E={w_e} ({source}), законы {sorted(r.counts)}", flush=True)
    summary["defects"] = defects_sum

    # 4. Перекрёстная проверка поиска
    cc = conf["crosscheck"]
    mismatches = []
    checked = 0
    for g in conf["table"]["graphs"]:
        for k in cc["levels"]:
            for m in cc["memory"]:
                a = fast(World(g, k, m)).w_e
                b = min_work(g, k, m)
                checked += 1
                if a != b:
                    mismatches.append({"graph": g, "levels": k, "memory": m, "fast": a, "crosscheck": b})
    summary["crosscheck"] = {"checked": checked, "mismatches": mismatches}
    print(f"перекрёстная проверка: {checked} сравнений, расхождений {len(mismatches)}", flush=True)

    with (OUT / "shag3.csv").open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=COLUMNS)
        wr.writeheader()
        wr.writerows(rows)
    (OUT / "shag3_svodka.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"готово: {len(rows)} строк")


if __name__ == "__main__":
    main()
