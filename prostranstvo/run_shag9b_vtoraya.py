"""Шаг 9б: режим 2 и способ «без сети» во второй реализации — сверка с первой.

Вторая реализация (rust7/executor_b) получает §18 контракта под замком;
здесь — проверка того, что получилось:

1. законы второй: набор шага 9 при model = 2 способами netns и probe
   (`harness.py --suite 9 --model-mode 2 [--model-net probe]`) и набор
   `--suite net` (закон «К18»);
2. перебор попыток хода модели при model = 2 на обеих реализациях: закон
   перебора и совпадение попытка в попытку;
3. прямая сверка без исключений (experiments/diff7.py, run_both_9): режим 2
   способом netns, режим 2 способом probe у обеих, режим 1 — что прежнее
   не сдвинулось.

Прежние наборы (шаги 5–8в) сверяет run_shag7.py с `--prices`; здесь их нет.

python3 run_shag9b_vtoraya.py [--rust ИСПОЛНИТЕЛЬ] [--quick] [--name ИМЯ]
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
from run_shag9_vtoraya import compare_attempts

H = D.H
RESULTS = Path(__file__).resolve().parent / "experiments" / "results"


def laws(runs: list[dict]) -> dict:
    bad = [r for r in runs if H.broken(r)]
    return {"запусков": len(runs), "с нарушениями": len(bad),
            "какие": [{"program": r["program"], "laws": H.broken(r),
                       "detail": {k: str(r["laws"][k][1])[:200] for k in H.broken(r)}} for r in bad]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust", default=str(D.JUDGE.parent / "executor_b" / "target" / "release" / "executor7"))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--name", default="shag9b_vtoraya", help="имя файла итогов")
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="shag9bv_"))
    t0 = time.time()
    res: dict = {"rust": a.rust, "quick": a.quick}

    res["законы"] = {
        "К18": laws(H.suite_net(a.rust, root / "сеть")),
        "9, model = 2, netns": laws(H.suite_9(a.rust, root / "законы2", a.quick, 2)),
        "9, model = 2, probe": laws(H.suite_9(a.rust, root / "законы2p", a.quick, 2, "probe")),
    }
    for k, v in res["законы"].items():
        print(f"законы второй, {k}: {v['запусков']} запусков, с нарушениями {v['с нарушениями']}; "
              f"{time.time() - t0:.0f} с", flush=True)

    rows_b = enum8v.enumerate_all(lambda kind: enum8v.JsonExe(shlex.split(a.rust), kind, 2), 2)
    rows_a = enum8v.enumerate_all(lambda kind: enum8v.JsonExe(enum8v.PY_EXE, kind, 2), 2)
    # Попытка без итога — исполнитель отказал ещё на `new`; сводка её не считает.
    lost = sum(1 for r in rows_b if "spent" not in r)
    sb = enum8v.summary([r for r in rows_b if "spent" in r])
    res["перебор, model = 2"] = {"вторая": sb, "первая": enum8v.summary(rows_a), "без итога у второй": lost,
                                 "расхождения": compare_attempts(rows_a, rows_b)}
    print(f"перебор при model = 2: вторая — попыток {sb.get(1, {}).get('попыток')}, без цены с правилом "
          f"{sb.get(1, {}).get('без цены')}, без итога {lost}; "
          f"расхождений с первой {len(res['перебор, model = 2']['расхождения'])}; {time.time() - t0:.0f} с",
          flush=True)

    for n, (name, mode, nets) in enumerate((("model = 2, netns", 2, (None, None)),
                                            ("model = 2, probe", 2, ("probe", "probe")),
                                            ("model = 1, netns", 1, (None, None)))):
        # Папка без пробелов: путь к сценарию модели идёт в режиме `--script
        # ФАЙЛ`, а режим делится по пробелам (контракт §17).
        rows = D.run_both_9(a.rust, root / f"сравнение{n}", a.quick, mode=mode, nets=nets)
        c = Counter(r["verdict"] for r in rows)
        res[f"сверка, {name}"] = {"случаев": len(rows), "итоги": dict(c),
                                  "расхождения": [{"name": r["name"], "diff": r["diff"]} for r in rows
                                                  if r["verdict"] != "совпали"]}
        print(f"прямая сверка, {name}: {len(rows)} случаев, {dict(c)}; {time.time() - t0:.0f} с", flush=True)
        for r in res[f"сверка, {name}"]["расхождения"][:5]:
            print(f"   «{r['name']}»: {str(r['diff'])[:240]}")

    res["секунд"] = round(time.time() - t0)
    (RESULTS / f"{a.name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                                            encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
