"""Прогон шага 4: неподвижная точка правил для правильной среды, порч Д1–Д8
и варианта с человеком-ретранслятором; сверка двух вычислителей.

Запуск из папки проекта:  python3 run_shag4.py
Результаты пишутся в experiments/results/.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.mir4 import INVENTORY, SUBJECTS, TITLES, VARIANTS  # noqa: E402
from verification.closure import LAW_TITLES, check, fixpoint  # noqa: E402
from verification.closure_direct import fixpoint_direct  # noqa: E402

OUT = ROOT / "experiments" / "results"


def rules_version() -> str:
    h = hashlib.sha256()
    for name in ("core/mir4.py", "verification/closure.py", "verification/closure_direct.py"):
        h.update((ROOT / name).read_bytes())
    return h.hexdigest()[:12]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rv = rules_version()
    rows, summary = [], {"rules_version": rv, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "variants": {}}
    lines = [f"ШАГ 4 — пути к нарушениям замкнутости. Версия правил {rv}.", ""]
    for v in (None,) + VARIANTS:
        name = v or "правильная"
        t0 = time.perf_counter()
        c = fixpoint(v)
        direct = fixpoint_direct(v)
        r = check(c)
        dt = time.perf_counter() - t0
        rows.append({
            "run_id": f"S4-{name}", "variant": name, "title": TITLES.get(v, "правильная среда"),
            "facts": len(c.facts), "rounds": c.rounds, "engines_agree": c.facts == direct,
            "laws": " ".join(sorted(r.violations)), "outside_inventory": len(r.fired_outside_inventory),
            "liveness_missing": len(r.liveness_missing), "wall_time": round(dt, 4), "rules_version": rv,
            "hypothesis": "Г0-м Г0-о И25",
        })
        summary["variants"][name] = {
            "title": TITLES.get(v, "правильная среда"), "facts": len(c.facts), "engines_agree": c.facts == direct,
            "laws": sorted(r.violations), "paths": r.paths,
            "outside_inventory": sorted(map(list, r.fired_outside_inventory)),
            "liveness_missing": r.liveness_missing,
        }
        lines.append(f"{name.upper()}: {TITLES.get(v, 'правильная среда')}")
        if not r.violations:
            lines.append("  нарушений нет")
        for law, path in r.paths.items():
            lines.append(f"  {law} ({LAW_TITLES[law]}):")
            lines += [f"    {i}. {step}" for i, step in enumerate(path, 1)]
        lines.append("")
        if v is None:
            summary["inventory_unused"] = sorted(map(list, INVENTORY - c.fired))
            summary["reachable"] = {
                s: sorted(f"{x[2]}:{x[3]}" for x in c.facts if x[0] == "право" and x[1] == s) for s in SUBJECTS
            }
            summary["information"] = {
                o: sorted(x[2] for x in c.facts if x[0] == "сведения" and x[1] == o)
                for o in ("память1", "память2", "выход1", "выход2", "E1", "E2", "СНАРУЖИ")
            }
        print(f"{name}: законы {sorted(r.violations) or 'чисто'}, вычислители согласны: {c.facts == direct}", flush=True)
    with (OUT / "shag4.csv").open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    (OUT / "shag4_puti.txt").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "shag4_svodka.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("готово")


if __name__ == "__main__":
    main()
