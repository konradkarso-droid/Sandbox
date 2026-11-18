"""Прогон шага 1: полный перебор, порчи, случайный обход.

Запуск из папки проекта:  python3 run_shag1.py
Результаты пишутся в experiments/results/. Параметры — experiments/shag1.yaml.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import platform
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.laws import LAW_TITLES  # noqa: E402
from core.world import ALL_DEFECTS, DEFECT_TITLES, Params  # noqa: E402
from verification.exhaustive import SearchResult, explore, replay  # noqa: E402
from verification.random_walk import walk  # noqa: E402

OUT = ROOT / "experiments" / "results"

COLUMNS = [
    "run_id", "variant", "executor", "method", "task", "hypothesis", "seed",
    "budget", "price", "fail_price", "max_helpers", "delivery_cap",
    "states", "transitions", "steps", "spent_work", "min_cost_to_target",
    "max_deliveries", "receiver_delivered", "adversary_goal",
    "invariant_violations", "first_violation_law", "counterexample_len",
    "bound_reached", "wall_time", "rules_version", "transitions_hash", "stop_reason",
]


def rules_version() -> str:
    h = hashlib.sha256()
    for name in ("core/world.py", "core/laws.py"):
        h.update((ROOT / name).read_bytes())
    return h.hexdigest()[:12]


def skipped(cfg: dict, rules: list[dict]) -> bool:
    for r in rules:
        if (cfg["max_helpers"] == r["max_helpers"] and cfg["price"] == r["price"]
                and cfg["budget"] > r["budget_over"]):
            return True
    return False


def search_row(run_id: str, r: SearchResult, conf: dict, executor: str, rv: str) -> dict:
    p = r.params
    first_law = None
    ce_len = None
    if r.first:
        first_law, ce = min(r.first.items(), key=lambda kv: (len(kv[1].path), kv[0]))
        ce_len = len(ce.path)
    return {
        "run_id": run_id, "variant": conf["variant"], "executor": executor,
        "method": "полный перебор", "task": conf["task"],
        "hypothesis": " ".join(conf["hypotheses"]), "seed": "",
        "budget": p.budget, "price": p.price, "fail_price": p.fail_price,
        "max_helpers": p.max_helpers, "delivery_cap": p.cap(),
        "states": r.states, "transitions": r.transitions, "steps": "",
        "spent_work": r.max_spent,
        "min_cost_to_target": "" if r.min_cost_to_delivery is None else r.min_cost_to_delivery,
        "max_deliveries": r.max_deliveries,
        "receiver_delivered": r.min_cost_to_delivery is not None,
        "adversary_goal": r.adversary_goal,
        "invariant_violations": " ".join(f"{k}:{v}" for k, v in sorted(r.violation_counts.items())),
        "first_violation_law": first_law or "", "counterexample_len": "" if ce_len is None else ce_len,
        "bound_reached": r.bound_reached, "wall_time": round(r.wall_time, 3),
        "rules_version": rv, "transitions_hash": r.states_hash,
        "stop_reason": "перебор завершён" + (" (у границы)" if r.bound_reached else ""),
    }


def describe_path(path) -> str:
    if not path:
        return "    (нарушено уже в начальном состоянии)"
    return "\n".join(f"    {i}. {st.label}" for i, st in enumerate(path, 1))


def main() -> None:
    conf = yaml.safe_load((ROOT / "experiments" / "shag1.yaml").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    rv = rules_version()
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict] = []
    summary: dict = {"rules_version": rv, "started": started,
                     "python": platform.python_version(), "machine": platform.machine()}

    # 1. Полный перебор правильного исполнителя.
    ex = conf["exhaustive"]
    grid = itertools.product(ex["budget"], ex["price"], ex["fail_price"], ex["max_helpers"])
    clean_runs, dirty_runs, bound_hits = 0, [], []
    cost_checks = []
    for i, (b, c, f, k) in enumerate(grid, 1):
        cfg = {"budget": b, "price": c, "fail_price": f, "max_helpers": k}
        if skipped(cfg, ex.get("skip_if", [])):
            continue
        p = Params(b, c, f, k)
        r = explore(p)
        rows.append(search_row(f"S1-P{i:03d}", r, conf, "правильный", rv))
        if r.clean:
            clean_runs += 1
        else:
            dirty_runs.append(rows[-1]["run_id"])
        if r.bound_reached:
            bound_hits.append(rows[-1]["run_id"])
        expect_cost = c if b >= c else None
        expect_max = (b // c) if b >= c else 0
        cost_checks.append(r.min_cost_to_delivery == expect_cost and r.max_deliveries == expect_max)
        print(f"{rows[-1]['run_id']} B={b} c={c} c_отк={f} K={k}: состояний {r.states}, "
              f"нарушений {sum(r.violation_counts.values())}, {r.wall_time:.2f} с", flush=True)
    summary["exhaustive"] = {
        "runs": clean_runs + len(dirty_runs), "clean": clean_runs, "dirty": dirty_runs,
        "bound_reached": bound_hits, "cost_as_declared": all(cost_checks),
        "max_states": max(int(r["states"]) for r in rows),
        "total_states": sum(int(r["states"]) for r in rows),
        "total_transitions": sum(int(r["transitions"]) for r in rows),
        "total_time": round(sum(float(r["wall_time"]) for r in rows), 1),
    }

    # Эталонный путь к доставке (регрессия §26 п.11 концепции).
    dc = conf["defects_config"]
    p_def = Params(dc["budget"], dc["price"], dc["fail_price"], dc["max_helpers"])
    ref = explore(p_def)
    lines = ["КОНТРПРИМЕРЫ И ЭТАЛОННЫЙ ПУТЬ — шаг 1, малый мир учёта (M1)",
             f"Версия правил: {rv}. Запуск: {started}.",
             f"Параметры: бюджет {p_def.budget}, цена фиксации {p_def.price}, "
             f"цена неудачной попытки {p_def.fail_price}, помощников до {p_def.max_helpers}.",
             "",
             "ЭТАЛОННЫЙ ПУТЬ (правильный исполнитель): самая дешёвая доставка",
             f"  цена {ref.min_cost_to_delivery}, шагов {len(ref.delivery_path or [])}:",
             describe_path(ref.delivery_path), ""]
    summary["reference"] = {"min_cost": ref.min_cost_to_delivery,
                            "path": [s.label for s in (ref.delivery_path or [])],
                            "states": ref.states, "clean": ref.clean}

    # 2. Порчи: каждая обязана быть поймана; путь-контрпример проверяется
    # повторным прохождением через исполнитель.
    defects_summary = {}
    laws_fired: set[str] = set()
    for d in ALL_DEFECTS:
        r = explore(p_def, frozenset([d]))
        rows.append(search_row(f"S1-{d}", r, conf, f"порча {d}", rv))
        laws_fired |= set(r.violation_counts)
        replay_ok = all(
            law in {v.law for v in replay(p_def, frozenset([d]), ce.path)}
            for law, ce in r.first.items()
        )
        defects_summary[d] = {
            "title": DEFECT_TITLES[d], "caught_by": sorted(r.violation_counts),
            "adversary_goal": r.adversary_goal, "replay_ok": replay_ok,
            "shortest": {law: [s.label for s in ce.path] for law, ce in r.first.items()},
        }
        lines.append(f"ПОРЧА {d}: {DEFECT_TITLES[d]}")
        if not r.first:
            lines.append("  НЕ ПОЙМАНА — проверка законов слепа к этой порче!")
        for law, ce in sorted(r.first.items(), key=lambda kv: (len(kv[1].path), kv[0])):
            lines.append(f"  нарушен {law} ({LAW_TITLES[law]}): {ce.detail}")
            lines.append(f"  самый короткий путь, шагов {len(ce.path)}:")
            lines.append(describe_path(ce.path))
        lines.append(f"  повторное прохождение пути через исполнитель: {'подтверждает' if replay_ok else 'НЕ подтверждает'}")
        lines.append("")
        print(f"порча {d}: поймана законами {sorted(r.violation_counts)}", flush=True)
    summary["defects"] = defects_summary
    summary["laws_never_fired"] = sorted(set(LAW_TITLES) - laws_fired)
    (OUT / "shag1_kontrprimery.txt").write_text("\n".join(lines), encoding="utf-8")

    # 3. Случайный обход больших параметров.
    rw = conf["random_walk"]
    pw = Params(**rw["params"])
    walk_rows = []
    walk_summary = {}
    for executor in ("правильный", *ALL_DEFECTS):
        defects = frozenset() if executor == "правильный" else frozenset([executor])
        caught, steps_to_catch, times = 0, [], []
        for seed in rw["seeds"]:
            w = walk(pw, seed, rw["steps"], defects)
            times.append(w.wall_time)
            if w.violation_law:
                caught += 1
                steps_to_catch.append(w.violation_step)
            walk_rows.append({
                "run_id": f"S1-W-{executor if executor != 'правильный' else 'OK'}-{seed:02d}",
                "variant": conf["variant"], "executor": executor if executor == "правильный" else f"порча {executor}",
                "method": "случайный обход", "task": conf["task"],
                "hypothesis": " ".join(conf["hypotheses"]), "seed": seed,
                "budget": pw.budget, "price": pw.price, "fail_price": pw.fail_price,
                "max_helpers": pw.max_helpers, "delivery_cap": pw.cap(),
                "states": "", "transitions": "", "steps": w.steps_done, "spent_work": w.max_spent,
                "min_cost_to_target": "", "max_deliveries": w.deliveries,
                "receiver_delivered": w.deliveries > 0, "adversary_goal": w.violation_law == "И6",
                "invariant_violations": w.violation_law or "", "first_violation_law": w.violation_law or "",
                "counterexample_len": "" if w.violation_step is None else w.violation_step,
                "bound_reached": "", "wall_time": round(w.wall_time, 3), "rules_version": rv,
                "transitions_hash": "", "stop_reason": "нарушение" if w.violation_law else "шаги исчерпаны",
            })
        walk_summary[executor] = {
            "seeds": len(rw["seeds"]), "caught": caught,
            "median_step": sorted(steps_to_catch)[len(steps_to_catch) // 2] if steps_to_catch else None,
            "max_step": max(steps_to_catch) if steps_to_catch else None,
        }
        print(f"обход, {executor}: поймано на {caught} из {len(rw['seeds'])} зёрен", flush=True)
    summary["random_walk"] = {"params": rw["params"], "steps": rw["steps"], "by_executor": walk_summary}

    with (OUT / "shag1.csv").open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=COLUMNS)
        wr.writeheader()
        wr.writerows(rows + walk_rows)
    (OUT / "shag1_svodka.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"готово: {len(rows) + len(walk_rows)} строк в {OUT / 'shag1.csv'}")


if __name__ == "__main__":
    main()
