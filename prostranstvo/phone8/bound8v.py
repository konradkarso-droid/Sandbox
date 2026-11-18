"""Шаг 8в: нагрузки для проверки предела с правилом «попытка платит».

Два набора стратегий «потратить побольше», все при B = 625 и ценах шага 8б
с правилом (спецификация шага 8в):

- **стратегии шага 8б** — те же программы, что в phone8/bound8b.py: для
  сравнения «до и после». С правилом они кончаются остановом, а не
  топливом;
- **стратегии перебора** — по одной на каждый класс попыток из
  experiments/enum8v.py: попытка-представитель, повторённая без конца.
  Класс — код отказа (успехи — один класс) и виды записей. Классы `--kind
  6`, у которых такие же записи, как у класса `--kind 5б`, не повторяются;
  остаётся только то, чего в 5б нет.

Пишет модули pv_*.wasm и план `assets/w8/plan8v.json` рядом с нагрузками
шага 8. У нагрузки `--kind 6` в плане свои поля «exec» и «cfg».

python3 phone8/bound8v.py [--exe "КОМАНДА ИСПОЛНИТЕЛЯ"] [--repeat N] [--only ИМЯ]
— собрать; с исполнителем — ещё и прогнать каждую нагрузку на сервере.
"""

from __future__ import annotations

import argparse
import json
import shlex
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from experiments import enum8v as E  # noqa: E402
from phone8 import bound8b as B8  # noqa: E402
from phone8.workloads8 import OUT, wasm  # noqa: E402

CFG = {**B8.CFG, "paid_attempts": 1}
CFG6 = {**CFG, "check_price": 10, "task_rng": "splitmix"}
ENUM = HERE.parent / "experiments" / "results" / "shag8v_enum.json"


def _class_name(kind: str, code, records: tuple) -> str:
    what = ", ".join(r.replace("действие: ", "") for r in records) or "без записей"
    return f"{'успех' if code == 'успех' else code}: {what}" + (" (6)" if kind == "6" else "")


def enum_strategies() -> list[tuple[str, str, tuple, int]]:
    """(имя, вид исполнителя, попытка, предисловие) по классам перебора."""
    rows = json.loads(ENUM.read_text(encoding="utf-8"))["rows"]
    cl = E.classes(rows)
    seen5 = {(code, recs) for (kind, code, recs) in cl if kind == "5б"}
    out = []
    for (kind, code, recs), r in cl.items():
        if kind == "6" and (code, recs) in seen5:
            continue
        out.append((_class_name(kind, code, recs), kind, tuple(r["call"]), r["prefix"]))
    return out


def build() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("pv_*.wasm"):
        f.unlink()
    work, kinds = {}, []
    for n, (name, (moves, tail, helper)) in enumerate(B8.STRATEGIES.items()):
        fname = f"pv_8b_{n:02d}.wasm"
        (OUT / fname).write_bytes(wasm(B8.build_module(moves, tail, helper)))
        work[f"8б: {name}"] = [{"file": fname, "n": 1}]
    for n, (name, kind, probe, prefix) in enumerate(enum_strategies()):
        fname = f"pv_enum_{n:02d}.wasm"
        (OUT / fname).write_bytes(wasm(E.module(probe, prefix, loop=True)))
        item = {"file": fname, "n": 1}
        if kind == "6":
            item.update(exec="6", cfg=CFG6)
        work[f"перебор: {name}"] = [item]
    kinds = list(work)
    plan = {"cfg": CFG, "kinds": kinds, "work": work, "channels": []}
    (OUT / "plan8v.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan


def energy_plan(phone_file: Path, n: int = 8) -> dict:
    """План отдельного прогона на энергию: n стратегий, самых дорогих по
    энергии в прогоне «предел 8в» (ΔP — больший из двух кругов), и «топливо»
    — для цены единицы топлива. Нагрузки те же, что в plan8v.json."""
    import run_shag8b as R8
    full = json.loads((OUT / "plan8v.json").read_text(encoding="utf-8"))
    st = R8.bound_check(phone_file, 1e9, 1e9)["strategies"]
    ranked = sorted((v["energy_mJ"], k) for k, v in st.items() if v["energy_mJ"] is not None and v["spent"] > 0)
    kinds = ["8б: топливо"] + [k for _, k in reversed(ranked[-n:])]
    plan = {"cfg": full["cfg"], "kinds": kinds, "work": {k: full["work"][k] for k in kinds}, "channels": [],
            "chosen_by": {k: st[k]["energy_mJ"] for k in kinds}}
    (OUT / "plan8ve.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan


def check(exe: list[str], plan: dict, n: int = 1, only: str | None = None) -> list[dict]:
    """Каждая нагрузка на сервере n раз: исход, бюджет, время run, размер
    журнала. Сервер не телефон: это проверка нагрузок и порядок величин."""
    out = []
    for name, ws in plan["work"].items():
        if only and only not in name:
            continue
        w = ws[0]
        drv = E.JsonExe(exe, w.get("exec", "5б"))
        try:
            data = (OUT / w["file"]).read_bytes()
            ts = []
            for _ in range(n):
                st, wd = drv.call("new", w.get("cfg", plan["cfg"]), None)
                lo = drv.call("load", 1, data)[1]
                t0 = time.perf_counter_ns()
                ro = drv.call("run", 1)[1]
                ts.append((time.perf_counter_ns() - t0) / 1e6)
                c = drv.call("dump")[1]["contours"]["1"]
                jkb = (Path(wd) / "journal5.jsonl").stat().st_size / 1024
            ts.sort()
            row = {"name": name, "load": lo, "run": ro, "spent": c["spent"], "fuel_left": c["fuel"],
                   "refusals": c["refusals"], "ms_min": ts[0], "ms_median": statistics.median(ts), "ms_max": ts[-1],
                   "journal_kb": jkb}
            out.append(row)
            print(f"{name:52s} {ro[0]}: {ro[1]:18s} бюджет {c['spent']:3d}, run мс {ts[0]:7.1f} / "
                  f"{statistics.median(ts):7.1f} / {ts[-1]:7.1f}, журнал {jkb:6.1f} КБ", flush=True)
        finally:
            drv.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="команда исполнителя одной строкой")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--only")
    ap.add_argument("--out", help="куда записать итог сервера (JSON)")
    ap.add_argument("--energy-from", help="файл стенда «предел 8в»: собрать план прогона на энергию")
    a = ap.parse_args()
    plan = build()
    if a.energy_from:
        e = energy_plan(Path(a.energy_from))
        print("план на энергию:", ", ".join(e["kinds"]))
    print(f"собрано нагрузок: {len(plan['work'])}")
    if a.exe:
        rows = check(shlex.split(a.exe), plan, a.repeat, a.only)
        if a.out:
            Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
