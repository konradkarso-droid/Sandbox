"""Прогон шага 2: сверка с моделью, опыты О1, О2, О6, О3, порчи Р1–Р13.

Запуск из папки проекта:  python3 run_shag2.py
Результаты пишутся в experiments/results/. Параметры — experiments/shag2.yaml.
Рабочие файлы опытов (журналы, получатели) — во временной папке, которая
удаляется в конце.
"""

from __future__ import annotations

import csv
import hashlib
import json
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.world import Params  # noqa: E402
from env.executor import ALL_DEFECTS, DEFECT_TITLES, FAULT_POINTS  # noqa: E402
from experiments.shag2_opyty import O6_SCENARIOS, o1, o2, o3, o6  # noqa: E402
from verification.conformance import run_sequence  # noqa: E402
from verification.observer import LAW_TITLES  # noqa: E402

OUT = ROOT / "experiments" / "results"

COLUMNS = [
    "run_id", "experiment", "variant", "executor", "seed", "scenario", "fault_point",
    "requests", "refused", "threads", "crashes", "restarts", "k", "way", "commands",
    "journal_records", "spent_work", "bound", "below_bound", "invariant_violations",
    "first_violation", "stop_reason", "wall_time", "rules_version", "hypothesis",
]


def rules_version() -> str:
    h = hashlib.sha256()
    for name in ("env/journal.py", "env/receiver.py", "env/executor.py", "env/agent.py",
                 "verification/observer.py", "core/world.py", "core/laws.py"):
        h.update((ROOT / name).read_bytes())
    return h.hexdigest()[:12]


def defect_variant(d: str) -> str:
    return "M2" if d in ("Р7", "Р8") else "M1"


def main() -> None:
    conf = yaml.safe_load((ROOT / "experiments" / "shag2.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="prostranstvo_shag2_"))
    rv = rules_version()
    hyp = " ".join(conf["hypotheses"])
    rows: list[dict] = []
    summary: dict = {"rules_version": rv, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "python": platform.python_version()}
    ce_lines = [f"ШАГ 2 — первые нарушения по каждой порче и опыту. Версия правил {rv}.", ""]

    def row(run_id, r, executor, t, **extra):
        v = r["violations"]
        first = next(iter(sorted(v.first.items())), ("", ""))
        rows.append({
            "run_id": run_id, "experiment": r["experiment"], "variant": r["variant"],
            "executor": executor, "seed": r.get("seed", ""), "scenario": r.get("scenario", ""),
            "fault_point": r.get("fault", ""), "requests": r.get("requests", ""),
            "refused": r.get("refused", ""), "threads": r.get("threads", ""),
            "crashes": r.get("crashes", ""), "restarts": r.get("restarts", ""),
            "k": r.get("k", ""), "way": r.get("way", ""), "commands": r.get("commands", ""),
            "journal_records": r.get("journal_records", ""), "spent_work": r.get("spent", ""),
            "bound": r.get("bound", ""), "below_bound": r.get("below_bound", ""),
            "invariant_violations": v.laws, "first_violation": f"{first[0]} {first[1]}".strip(),
            "stop_reason": r.get("stop", r.get("outcome", "")), "wall_time": round(t, 3),
            "rules_version": rv, "hypothesis": hyp, **extra,
        })

    def timed(fn, *a, **kw):
        t0 = time.perf_counter()
        r = fn(*a, **kw)
        return r, time.perf_counter() - t0

    # ---------------------------------------------------- сверка с моделью
    cc = conf["conformance"]
    conf_sum = {"configs": [], "defects": {}}
    for ci, c in enumerate(cc["configs"], 1):
        p = Params(c["budget"], c["price"], c["fail_price"], c["max_helpers"])
        t0 = time.perf_counter()
        bad = [r for r in (run_sequence(p, s, cc["steps"], tmp / "conf") for s in range(1, cc["sequences"] + 1)) if r["step"]]
        conf_sum["configs"].append({"params": c, "sequences": cc["sequences"], "steps": cc["steps"],
                                    "divergences": len(bad), "time": round(time.perf_counter() - t0, 1),
                                    "first": None if not bad else {k: str(v) for k, v in bad[0].items()}})
        print(f"сверка, конфигурация {ci}: расхождений {len(bad)} из {cc['sequences']}", flush=True)
    p0 = Params(**cc["configs"][0])
    for d in ALL_DEFECTS:
        hits = [run_sequence(p0, s, cc["steps"], tmp / "conf", frozenset([d])) for s in range(1, cc["defect_sequences"] + 1)]
        div = [h for h in hits if h["step"]]
        conf_sum["defects"][d] = {"divergences": len(div), "of": len(hits),
                                  "first": None if not div else f"шаг {div[0]['step']}: {div[0]['label']}"}
    summary["conformance"] = conf_sum
    print("сверка с порчами готова", flush=True)

    # ---------------------------------------------------- правильный исполнитель
    clean: dict = {}
    for v in conf["variants"]:
        for s in range(1, conf["o1"]["seeds"] + 1):
            r, t = timed(o1, v, s, tmp / "o1")
            row(f"S2-О1-{v}-{s:02d}", r, "правильный", t)
        for s in range(1, conf["o2"]["seeds"] + 1):
            r, t = timed(o2, v, s, tmp / "o2", steps=conf["o2"]["steps"])
            row(f"S2-О2-{v}-{s:02d}", r, "правильный", t)
        for sc in O6_SCENARIOS:
            for fp in FAULT_POINTS[:-1]:
                for rf in (False, True):
                    r, t = timed(o6, v, sc, fp, rf, tmp / "o6")
                    row(f"S2-О6-{v}-{sc}-{fp}-{'вс' if rf else 'нв'}", r, "правильный", t)
        for k in conf["o3"]["k"]:
            for way in conf["o3"]["ways"]:
                if way.startswith("на") and int(way[3:]) > k:
                    continue
                r, t = timed(o3, v, k, way, tmp / "o3")
                row(f"S2-О3-{v}-{k}-{way}", r, "правильный", t)
        print(f"правильный исполнитель, {v}: готово", flush=True)
    for v in conf["variants"]:
        for exp in ("О1", "О2", "О6", "О3"):
            rs = [x for x in rows if x["executor"] == "правильный" and x["variant"] == v and x["experiment"] == exp]
            laws: dict[str, int] = {}
            for x in rs:
                for part in x["invariant_violations"].split():
                    law, n = part.split(":")
                    laws[law] = laws.get(law, 0) + int(n)
            clean[f"{v} {exp}"] = {"runs": len(rs), "runs_with_violations": sum(bool(x["invariant_violations"]) for x in rs),
                                   "laws": laws}
    summary["correct"] = clean
    o3_rows = [{k: x[k] for k in ("variant", "k", "way", "commands", "journal_records", "spent_work", "bound", "below_bound")}
               for x in rows if x["experiment"] == "О3" and x["executor"] == "правильный"]
    summary["o3_table"] = o3_rows
    o6_out: dict[str, int] = {}
    for x in rows:
        if x["experiment"] == "О6" and x["executor"] == "правильный":
            o6_out[x["stop_reason"].split(":")[0]] = o6_out.get(x["stop_reason"].split(":")[0], 0) + 1
    summary["o6_outcomes"] = o6_out

    # ---------------------------------------------------- порчи
    defects_sum: dict = {}
    fired: set[str] = set()
    for d in ALL_DEFECTS:
        D = frozenset([d])
        v = defect_variant(d)
        per = {}
        ce_lines.append(f"ПОРЧА {d} ({v}): {DEFECT_TITLES[d]}")
        runs = {
            "О1": [timed(o1, v, s, tmp / "o1", D) for s in range(1, conf["o1"]["defect_seeds"] + 1)],
            "О2": [timed(o2, v, s, tmp / "o2", D, steps=conf["o2"]["steps"]) for s in range(1, conf["o2"]["defect_seeds"] + 1)],
            "О6": [timed(o6, v, sc, fp, rf, tmp / "o6", D) for sc in O6_SCENARIOS for fp in FAULT_POINTS[:-1] for rf in (False, True)],
            "О3": [timed(o3, v, k, way, tmp / "o3", D) for k in conf["o3"]["k"] for way in conf["o3"]["ways"]
                   if not (way.startswith("на") and int(way[3:]) > k)],
        }
        for exp, rs in runs.items():
            laws: set[str] = set()
            caught = 0
            first = None
            for i, (r, t) in enumerate(rs, 1):
                row(f"S2-{d}-{exp}-{i:02d}", r, f"порча {d}", t)
                if r["violations"].counts:
                    caught += 1
                    laws |= set(r["violations"].counts)
                    if first is None:
                        first = sorted(r["violations"].first.items())[0]
            fired |= laws
            per[exp] = {"caught": caught, "of": len(rs), "laws": sorted(laws)}
            if first:
                ce_lines.append(f"  {exp}: поймана в {caught} из {len(rs)}; первое нарушение {first[0]} — {first[1]}")
            else:
                ce_lines.append(f"  {exp}: не проявилась ({len(rs)} прогонов)")
        per["сверка"] = conf_sum["defects"][d]
        defects_sum[d] = {"title": DEFECT_TITLES[d], "variant": v, "by_experiment": per,
                          "caught_anywhere": any(x["caught"] for k, x in per.items() if k != "сверка")}
        ce_lines.append("")
        print(f"порча {d}: " + ", ".join(f"{k} {x['caught']}/{x['of']}" for k, x in per.items() if k != "сверка"), flush=True)
    summary["defects"] = defects_sum
    summary["laws_never_fired"] = sorted(set(LAW_TITLES) - fired - {"И1-M0"})

    with (OUT / "shag2.csv").open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=COLUMNS)
        wr.writeheader()
        wr.writerows(rows)
    (OUT / "shag2_kontrprimery.txt").write_text("\n".join(ce_lines), encoding="utf-8")
    (OUT / "shag2_svodka.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"готово: {len(rows)} строк в {OUT / 'shag2.csv'}")


if __name__ == "__main__":
    main()
