"""Шаг 7: вторая реализация против судьи и против первой реализации.

1. законы — все наборы обвязки чистой комнаты против второй реализации;
2. сверка с моделями шагов 1 и 3 через вторую реализацию;
3. различающее сравнение двух реализаций (experiments/diff7.py);
4. порча готовых файлов второй реализации — чувствительность наблюдателей;
5. системные вызовы её процесса (strace).

Итоги — experiments/results/shag7_*.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import experiments.diff7 as D
import harness as H
from core.mir3 import World
from experiments import programs5b as p5b
from experiments.agents6.build import build
from verification import observer5b, observer6

RESULTS = Path("experiments/results")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", default=str(D.JUDGE.parent / "executor_b" / "target" / "release" / "executor7"))
    ap.add_argument("--first", default=None, help="сравнивать не с первой реализацией, а с этой программой; "
                                                   "тогда только сравнение")
    ap.add_argument("--name", default="shag7", help="начало имён файлов итогов")
    ap.add_argument("--neutral", action="store_true", help="не видеть двух известных мест двоякого прочтения контракта")
    ap.add_argument("--prices", choices=["old", "new", "paid"], default="old",
                    help="new — цены шага 8б (контракт §15); paid — они же и «попытка платит» (§16)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--random", type=int, default=None, help="случайных программ шагов 5 и 5б в сравнении")
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="shag7_"))
    t0 = time.time()
    res: dict = {}

    if a.first:
        rows = D.run_both(a.rust, root / "сравнение", a.quick, trace=False, n_rand=a.random, first_exe=a.first,
                          neutral=a.neutral, new_prices={"old": False, "new": True, "paid": "paid"}[a.prices])
        res = {"first": a.first, "second": a.rust, "seconds": time.time() - t0,
               "diff": [{k: v for k, v in r.items() if k != "workdirs"} for r in rows]}
        (RESULTS / f"{a.name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                                                encoding="utf-8")
        print(f"сравнение: {len(rows)} случаев, {dict(Counter(r['verdict'] for r in rows))}, {res['seconds']:.0f} с")
        for r in rows:
            if r["verdict"] == "расхождение":
                print(f"  {r['suite']}, «{r['name']}»: {r['diff']}")
        shutil.rmtree(root, ignore_errors=True)
        return

    # 1. Законы на всех наборах обвязки
    laws_runs = []
    for name, fn in (("шаг 5", H.suite_5), ("шаг 5б", H.suite_5b), ("шаг 6", H.suite_6)):
        for r in fn(a.rust, root / "законы", a.quick):
            laws_runs.append({"suite": name, "program": r["program"], "broken": H.broken(r),
                              "detail": {k: r["laws"][k][1][:200] for k in H.broken(r)}})
    res["laws"] = laws_runs
    print(f"законы: {len(laws_runs)} запусков, с нарушениями {sum(1 for r in laws_runs if r['broken'])}, "
          f"{time.time() - t0:.0f} с", flush=True)

    # 2. Сверка с моделями
    res["conform1"] = H.conform1(a.rust, root / "м1", 5 if a.quick else 100)
    res["conform3"] = H.conform3(a.rust, root / "м3", 2 if a.quick else 25)
    print(f"модели: 1 — {res['conform1']['compared']} состояний, расхождений {len(res['conform1']['diverged'])}; "
          f"3 — {res['conform3']['steps']} ходов, расхождений {len(res['conform3']['diverged'])}", flush=True)

    # 3. Различающее сравнение, вторая реализация — под strace
    rows = D.run_both(a.rust, root / "сравнение", a.quick, trace=True, n_rand=a.random, neutral=a.neutral)
    res["diff"] = [{k: v for k, v in r.items() if k != "workdirs"} for r in rows]
    print(f"сравнение: {len(rows)} случаев, {dict(Counter(r['verdict'] for r in rows))}, {time.time() - t0:.0f} с",
          flush=True)

    # 5. Системные вызовы второй реализации
    events = []
    seen: Counter = Counter()
    for kind in ("5б", "6"):
        folder = root / "сравнение" / "вторая" / kind
        events += D.strace_events(folder, folder, a.rust)
        seen.update(D.strace_seen(folder))
    res["strace_seen"] = dict(seen.most_common())
    res["strace"] = events
    res["strace_files"] = sum(1 for _ in (root / "сравнение" / "вторая").rglob("strace*.txt"))

    # 4. Порча файлов второй реализации
    sens = []
    w = World("M3т", 4, 4)
    d5 = H.ExeDriver(a.rust, root / "порча" / "5б", "5б")
    for name, mods, fault in (
            ("жадный", p5b.module(p5b.greedy(w)), None),
            ("свидетель", p5b.module(p5b.witness(w)), None),
            ("сбой после доставки", p5b.module([("сообщение", "А"), ("сообщение", "Б")]), ("после доставки", 1))):
        r = H.one(d5, name, {1: mods, 2: mods}, D.BASE, fault)
        sens += [{"run": name, **x} for x in D.sensitivity(r, observer5b.check)]
    d5.close()
    d6 = H.ExeDriver(a.rust, root / "порча" / "6", "6")
    for aname in ("с ошибками", "честный"):
        b = build(H.AGENTS6[aname], H.e_moves(D.BASE6))
        r = H.one(d6, aname, {1: b, 2: b}, D.BASE6, rounds=1, checker=observer6.check, extra={"expect_all": True})
        sens += [{"run": aname, **x} for x in D.sensitivity(r, observer6.check)]
    d6.close()
    res["sensitivity"] = sens
    res["seconds"] = time.time() - t0
    write(res, a.name)
    shutil.rmtree(root, ignore_errors=True)
    print((RESULTS / f"{a.name}_summary.md").read_text(encoding="utf-8"))


def write(res: dict, name: str = "shag7") -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    L = ["# Шаг 7: итоги", "", f"Время: {res['seconds']:.0f} с.", "", "## 1. Законы на второй реализации", "",
         "| Набор | запусков | с нарушениями |", "|---|---|---|"]
    by = defaultdict(list)
    for r in res["laws"]:
        by[r["suite"]].append(r)
    for s, rs in by.items():
        L.append(f"| {s} | {len(rs)} | {sum(1 for r in rs if r['broken'])} |")
    for r in [r for r in res["laws"] if r["broken"]][:20]:
        L.append(f"- {r['suite']}, «{r['program']}»: " + "; ".join(f"{k}: {v}" for k, v in r["detail"].items()))
    c1, c3 = res["conform1"], res["conform3"]
    L += ["", "## 2. Сверка с моделями через вторую реализацию", "",
          f"- модель шага 1: сравнено состояний {c1['compared']}, сбоев по инъекции {c1['crashes']}, "
          f"расхождений {len(c1['diverged'])};",
          f"- модель шага 3: ходов {c3['steps']}, расхождений {len(c3['diverged'])}; свидетель: {c3['witness']}."]
    for x in (c1["diverged"] + c3["diverged"])[:10]:
        L.append(f"  - {x}")
    L += ["", "## 3. Различающее сравнение", "", "| Набор | совпали | сравнено по исходу | расхождение |",
          "|---|---|---|---|"]
    bys = defaultdict(Counter)
    for r in res["diff"]:
        bys[r["suite"]][r["verdict"]] += 1
    for s, c in bys.items():
        L.append(f"| {s} | {c['совпали']} | {c['сравнено по исходу']} | {c['расхождение']} |")
    L += [""]
    for r in [r for r in res["diff"] if r["verdict"] == "расхождение"][:40]:
        L.append(f"- **{r['suite']}, «{r['name']}»:** {r['diff']}")
    L += ["", "## 4. Порча файлов второй реализации", "", "| Запуск | порча | закон | итог |", "|---|---|---|---|"]
    for x in res["sensitivity"]:
        L.append(f"| {x['run']} | {x['порча']} | {x['закон']} | {x['итог']} |")
    L += ["", "## 5. Системные вызовы второй реализации", "",
          f"Записей strace (по одной на жизнь процесса): {res['strace_files']}; недопустимых событий: "
          f"{len(res['strace'])}.", "",
          "Записанные вызовы: " + ", ".join(f"{k} — {v}" for k, v in res["strace_seen"].items()) + "."]
    for e in res["strace"][:10]:
        L.append(f"- {e}")
    (RESULTS / f"{name}_summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
