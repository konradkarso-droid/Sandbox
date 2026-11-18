"""Шаг 8б: нагрузки для проверки предела расхода на телефоне.

Каждая нагрузка — один контур, который пытается обойтись телефону как
можно дороже при бюджете B = 625 и новых ценах (контракт шага 7, §15):
сначала тратит бюджет на один из видов работы среды, потом сжигает
остаток топлива. Предел из отчёта шага 8б: не больше 93 мс и 91 мДж за
жизнь контура (без запаса — 62 мс и 61 мДж).

Пишет модули и план `assets/w8/plan8b.json` рядом с нагрузками шага 8.

python3 phone8/bound8b.py [--exe ИСПОЛНИТЕЛЬ]  — собрать; с исполнителем —
ещё и прогнать каждую нагрузку и показать расход.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core.mir3 import World  # noqa: E402
from experiments import programs5b as p5b  # noqa: E402
from phone8.workloads8 import OUT, wasm  # noqa: E402

FUEL = 3_000_000
# Цены шага 8б и бюджет из пересчитанного окна; граф K = 6, чтобы путь
# жадного агента был длиннее, чем позволяет бюджет.
CFG = {"budget": 625, "graph": "M3т", "levels": 6, "memory": 4,
       "price": 10, "fail_price": 10, "work_price": 10, "gate_price": 5}

# Хвост «счёт»: тот же цикл, что у нагрузки «счёт» шага 8, — на телефоне
# это самая дорогая во времени единица топлива. Бесконечный: кончается
# топливом.
TAIL_COUNT = ("(loop $l (local.set $x (i32.add (i32.mul (local.get $x) (i32.const 1103515245)) "
              "(i32.const 12345))) (br $l))")


def build_module(moves: list[tuple], tail: str, helper: list[tuple] | None = None) -> str:
    """Ходы, затем бесконечный хвост. tail — «счёт», «отказы» (справочник
    без конца: после того как бюджет кончился, каждый вызов — отказ −3) или
    «отказы фиксации» (фиксация без конца: при пустом бюджете каждая —
    отказ −3 с записью в журнал)."""
    texts = p5b._Texts()
    body = "\n    ".join(p5b._code(m, texts) for m in moves) or "(nop)"
    if tail == "счёт":
        tail_code = TAIL_COUNT
    elif tail == "отказы":
        tail_code = f"(loop $l {p5b._code(('имя', 'цена'), texts)} (br $l))"
    elif tail == "отказы фиксации":
        tail_code = f"(loop $l {p5b._code(('сообщение', 'А'), texts)} (br $l))"
    elif tail == "отказы без права":
        # Вызов ворот через ручку, которой у контура нет: отказ при любом
        # остатке бюджета.
        tail_code = f"(loop $l {p5b._code(('ворота', 99, 0, 0, 0), texts)} (br $l))"
    else:
        raise ValueError(tail)
    hbody = "\n    ".join(p5b._code(m, texts) for m in helper or []) or "(nop)"
    return p5b.HEAD + texts.data() + f'''
  (func (export "main") (local $x i32)
    {body}
    {tail_code})
  (func (export "helper")
    {hbody}))'''


def node_path(count: int) -> list[tuple]:
    """Начало пути жадного агента с count вычислениями (выбросы — по пути)."""
    path = [m for m in p5b.greedy(World("M3т", CFG["levels"], CFG["memory"])) if m[0] in ("узел", "выброс")]
    out, done = [], 0
    for m in path:
        if m[0] == "узел":
            if done == count:
                break
            done += 1
        out.append(m)
    return out


# Объявленные стратегии. В каждой хода больше, чем позволяет бюджет:
# лишние получают отказ, и тратить дальше можно только топливо.
STRATEGIES = {
    "топливо": ([], "счёт", None),
    "фиксации": ([("сообщение", "А")] * 70, "счёт", None),
    "ворота": ([("имя", "цена")] * 130, "счёт", None),
    "отказы": ([("имя", "цена")] * 125, "отказы", None),
    "узлы": (node_path(70), "счёт", None),
    "помощники": ([("помощник",)] * 3, "счёт", [("имя", "цена")] * 50),
    "смесь": ([("сообщение", "А")] * 30 + [("имя", "цена")] * 40 + node_path(10), "отказы", None),
    # Не была в прогоне на телефоне: найдена при разборе его итогов.
    "отказы фиксации": ([("сообщение", "А")] * 63, "отказы фиксации", None),
    # Бюджет не тронут: отказ по праву, а не по деньгам.
    "отказы без права": ([], "отказы без права", None),
}
FILES = {"топливо": "pr_toplivo", "фиксации": "pr_fiks", "ворота": "pr_vorota", "отказы": "pr_otkazy",
         "узлы": "pr_uzly", "помощники": "pr_pomoshch", "смесь": "pr_smes",
         "отказы фиксации": "pr_otkazy_fiks", "отказы без права": "pr_otkazy_prava"}


def build() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("pr_*.wasm"):
        f.unlink()
    work = {}
    for name, (moves, tail, helper) in STRATEGIES.items():
        fname = f"{FILES[name]}.wasm"
        (OUT / fname).write_bytes(wasm(build_module(moves, tail, helper)))
        work[name] = [{"file": fname, "n": 1}]
    plan = {"cfg": CFG, "kinds": list(STRATEGIES), "work": work, "channels": [],
            "bound": {"time_ms": 93.0, "energy_mJ": 91.2, "time_ms_no_margin": 62.0, "energy_mJ_no_margin": 60.8,
                      "B": CFG["budget"], "fuel": FUEL}}
    (OUT / "plan8b.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan


def check(exe: str, plan: dict, n: int = 1, only: str | None = None) -> None:
    """Каждая нагрузка на сервере n раз: исход, расход бюджета и топлива,
    время run — наименьшее, медиана, наибольшее; размер журнала после
    последнего запуска. Сервер не телефон: это проверка нагрузок и порядок
    величин, а не предел."""
    import base64
    import statistics
    import subprocess
    import tempfile
    import time
    with tempfile.TemporaryDirectory() as root:
        p = subprocess.Popen([exe, "--root", root, "--kind", "5б"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, encoding="utf-8")

        def call(cmd, *args):
            p.stdin.write(json.dumps({"cmd": cmd, "args": list(args)}, ensure_ascii=False) + "\n")
            p.stdin.flush()
            r = json.loads(p.stdout.readline())
            if r["status"] != "ok":
                raise RuntimeError(f"{cmd}: {r}")
            return r["value"]

        for name, ws in plan["work"].items():
            if only and name != only:
                continue
            b64 = base64.b64encode((OUT / ws[0]["file"]).read_bytes()).decode()
            ts = []
            for _ in range(n):
                call("new", plan["cfg"], None)
                lo = call("load", 1, {"b64": b64})
                t0 = time.perf_counter_ns()
                ro = call("run", 1)
                ts.append((time.perf_counter_ns() - t0) / 1e6)
                c = call("dump")["contours"]["1"]
            ts.sort()
            journal = max(Path(root).rglob("journal5.jsonl"), key=lambda f: f.stat().st_mtime)
            jkb = journal.stat().st_size / 1024
            print(f"{name:10s} загрузка {lo[0]:9s} исход {ro[0]}: {ro[1]:22s} бюджет {c['spent']:4d} из {CFG['budget']}, "
                  f"топлива осталось {c['fuel']:7d}, run мс: {ts[0]:6.1f} / {statistics.median(ts):6.1f} / "
                  f"{ts[-1]:6.1f}, журнал {jkb:,.0f} КБ, отказы {c['refusals']}".replace(",", " "))
        p.stdin.close()
        p.wait()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe")
    ap.add_argument("--repeat", type=int, default=1, help="сколько раз запускать каждую нагрузку на сервере")
    ap.add_argument("--only", help="только эта нагрузка")
    a = ap.parse_args()
    plan = build()
    print(f"собрано нагрузок: {len(plan['work'])}")
    if a.exe:
        check(a.exe, plan, a.repeat, a.only)


if __name__ == "__main__":
    main()
