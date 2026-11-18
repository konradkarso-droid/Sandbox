"""Шаг 9: ход «спросить модель» во второй реализации — сверка с первой.

Вторая реализация (rust7/executor_b) получает ход §17 контракта под замком;
здесь — проверка того, что получилось:

1. законы на наборе шага 9 обвязки судьи (`harness.py --suite 9`);
2. перебор попыток хода модели (experiments/enum8v.py, `--model`) на обеих
   реализациях: закон перебора и совпадение попытка в попытку — код,
   расход, виды записей, остановка контура;
3. различающее сравнение на случаях шага 9 (experiments/diff7.py,
   run_both_9) — как есть и без двух известных мест двоякого прочтения
   (§14 контракта, neutral).

Прежние наборы (шаги 5–8в) сверяет run_shag7.py с `--prices`; здесь их нет.

python3 run_shag9_vtoraya.py [--rust ИСПОЛНИТЕЛЬ] [--quick] [--name ИМЯ]
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

from experiments import diff7 as D
from experiments import enum8v

H = D.H
RESULTS = Path(__file__).resolve().parent / "experiments" / "results"
ATTEMPT_KEYS = ("code", "spent", "records", "stopped", "run", "load")


def attempts(exe: list[str]) -> list[dict]:
    return enum8v.enumerate_all(lambda kind: enum8v.JsonExe(exe, kind, True), True)


def compare_attempts(a: list[dict], b: list[dict]) -> list[str]:
    out = []
    if len(a) != len(b):
        return [f"число попыток: {len(a)} ≠ {len(b)}"]
    for x, y in zip(a, b):
        for k in ATTEMPT_KEYS:
            if x.get(k) != y.get(k):
                out.append(f"{x['kind']} {x['rule']} {x['budget']} «{x['probe']}»: {k} {x.get(k)} ≠ {y.get(k)}")
                break
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", default=str(D.JUDGE.parent / "executor_b" / "target" / "release" / "executor7"))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--name", default="shag9_vtoraya", help="начало имён файлов итогов")
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="shag9v_"))
    t0 = time.time()
    res: dict = {"rust": a.rust, "quick": a.quick}

    runs = H.suite_9(a.rust, root / "законы", a.quick)
    bad = [r for r in runs if H.broken(r)]
    res["laws"] = {"runs": len(runs), "broken": [{"program": r["program"], "laws": H.broken(r),
                                                  "detail": {k: str(r["laws"][k][1])[:200] for k in H.broken(r)}}
                                                 for r in bad]}
    print(f"законы, набор шага 9: {len(runs)} запусков, с нарушениями {len(bad)}; {time.time() - t0:.0f} с", flush=True)

    rows_b = attempts(shlex.split(a.rust))
    rows_a = attempts(enum8v.PY_EXE)
    sb = enum8v.summary(rows_b)
    res["attempts"] = {"second": sb, "first": enum8v.summary(rows_a), "differ": compare_attempts(rows_a, rows_b)}
    print(f"перебор хода модели: вторая — попыток {sb[1]['попыток']}, без цены с правилом {sb[1]['без цены']}; "
          f"расхождений с первой {len(res['attempts']['differ'])}; {time.time() - t0:.0f} с", flush=True)

    for neutral in (False, True):
        rows = D.run_both_9(a.rust, root / f"сравнение{int(neutral)}", a.quick, neutral=neutral)
        key = "diff_neutral" if neutral else "diff"
        res[key] = [{k: v for k, v in r.items() if k != "workdirs"} for r in rows]
        c = Counter(r["verdict"] for r in rows)
        print(f"сравнение{' (neutral)' if neutral else ''}: {len(rows)} случаев, {dict(c)}; "
              f"{time.time() - t0:.0f} с", flush=True)
        for r in rows:
            if r["verdict"] == "расхождение":
                print(f"   «{r['name']}»: {str(r['diff'])[:240]}")

    res["seconds"] = time.time() - t0
    (RESULTS / f"{a.name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                                            encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
