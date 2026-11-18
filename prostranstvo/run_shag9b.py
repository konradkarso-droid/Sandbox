"""Шаг 9б, серверная часть: режим «модель на устройстве» (model = 2,
контракт §18) на первой реализации или на исполнителе из --exe.

Четыре проверки:

1. законы — набор случаев шага 9 при model = 2 (обвязка судьи, suite_9);
2. режим 2 против режима 1 на одном исполнителе: снимки обязаны различаться
   только значением model в записи «учёт» и тем, что агент сам сделал с
   ответом справочника (experiments/diff7.py, run_modes_9);
3. перебор попыток хода модели при model = 2: попыток без цены — 0, и
   каждая попытка даёт то же, что при model = 1, кроме ответа справочника
   на имя «модель» (experiments/enum8v.py);
4. модель замкнутости: мир без порчи чист, порча «модель с выходом»
   даёт путь сведений агента наружу (И21), порча «общая модель» — путь к
   соседу (И22);
5. способ «без сети» (контракт §18.1, `--model-net`): набор судьи
   `--suite net` (закон «К18»), законы шага 9 при model = 2 способом probe
   с подменной socket() и сверка журналов netns против probe на одном
   исполнителе — обязаны совпасть байт в байт.

Чего прогон не проверяет: процесс модели здесь — заглушка судьи, и время
вопроса не меряется; предел устройства с моделью — дело стенда на
телефоне (спецификация шага 9б, §7.4).

python3 run_shag9b.py [--exe "КОМАНДА ИСПОЛНИТЕЛЯ"] [--quick] [--name ИМЯ]
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

import experiments.diff7 as D
from core.mir9 import W5, W6
from experiments import enum8v
from verification import closure

H = D.H
HERE = Path(__file__).resolve().parent


def closure_rows() -> list[dict]:
    rows = []
    for world in (W5, W6):
        for variant in (None, "общая модель", "модель с выходом"):
            c = closure.fixpoint(variant, world)
            v = closure.check(c)
            viol = sorted(k for k, x in v.violations.items() if x and k != "И23")
            rows.append({"мир": world.name, "порча": variant or "нет", "нарушены": viol,
                         "вне описи": sorted({s[0] for s in c.fired if s not in world.INVENTORY_EXT}),
                         "путь": list(v.paths.get(viol[0], [])) if viol else []})
    return rows


def attempts(exe: list[str], mode: int) -> list[dict]:
    return enum8v.enumerate_all(lambda kind: enum8v.JsonExe(exe, kind, mode), mode)


def same_attempts(a: list[dict], b: list[dict]) -> list[str]:
    """Попытка в попытку. Разрешено одно отличие: справочник на имя «модель»
    возвращает номер режима — 1 и 2."""
    out = []
    if len(a) != len(b):
        return [f"число попыток {len(a)} ≠ {len(b)}"]
    for x, y in zip(a, b):
        if x == y:
            continue
        if x["probe"] == "справочник «модель»" and {**x, "code": 0} == {**y, "code": 0} and (x["code"], y["code"]) == (1, 2):
            continue
        out.append(f"{x['kind']} {x['probe']}: {D.first_difference(x, y)}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=None, help="команда внешнего исполнителя; без неё — первая реализация")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--name", default="shag9b_server")
    a = ap.parse_args()
    exe = a.exe or D.PY_EXE
    root = Path(tempfile.mkdtemp(prefix="shag9b_"))
    t0 = time.time()
    res: dict = {"исполнитель": exe}

    runs = H.suite_9(exe, root / "законы", a.quick, 2)
    bad = [r for r in runs if H.broken(r)]
    res["законы"] = {"запусков": len(runs), "с нарушениями": len(bad),
                     "какие": dict(Counter(k for r in bad for k in H.broken(r))),
                     "первые": [(r["program"], H.broken(r)) for r in bad[:5]]}
    print("законы при model = 2:", res["законы"])

    rows = D.run_modes_9(root / "режимы", a.quick, exe)
    extra = [r for r in rows if r["лишние"]]
    res["режимы"] = {"случаев": len(rows), "с различиями": sum(1 for r in rows if r["различий"]),
                     "с лишними различиями": len(extra),
                     "случаев, где отличается память": sum(1 for r in rows if r["байт памяти"]),
                     "байт памяти всего": sum(r["байт памяти"] for r in rows),
                     "первые лишние": [(r["name"], r["лишние"]) for r in extra[:5]]}
    print("режим 2 против режима 1:", res["режимы"])

    cmd = __import__("shlex").split(exe)
    r1, r2 = attempts(cmd, 1), attempts(cmd, 2)
    s2 = enum8v.summary(r2)
    res["перебор"] = {"model = 2": s2, "против model = 1": same_attempts(r1, r2)[:5],
                      "закон": "держится" if s2[1]["без цены"] == 0 else "НЕ держится"}
    print("перебор при model = 2:", {k: (v["попыток"], v["без цены"]) for k, v in s2.items()},
          "| отличий от model = 1:", len(res["перебор"]["против model = 1"]))

    net = H.suite_net(exe, root / "сеть")
    runs = H.suite_9(exe, root / "законы-probe", a.quick, 2, "probe")
    bad = [r for r in runs if H.broken(r)]
    rows = D.run_both_9(exe, root / "netns-probe", a.quick, first_exe=exe, mode=2, nets=(None, "probe"))
    res["способ без сети"] = {
        "К18": {"проверок": len(net), "нарушены": [(r["program"], r["laws"]["К18"][1]) for r in net if H.broken(r)]},
        "законы при probe": {"запусков": len(runs), "с нарушениями": len(bad),
                             "первые": [(r["program"], H.broken(r)) for r in bad[:5]]},
        "netns против probe": {"случаев": len(rows), "совпали": sum(1 for r in rows if r["verdict"] == "совпали"),
                               "первые расхождения": [(r["name"], r["diff"]) for r in rows
                                                      if r["verdict"] != "совпали"][:5]}}
    print("способ без сети:", {k: {kk: vv for kk, vv in v.items() if not kk.startswith("перв")}
                               for k, v in res["способ без сети"].items()})

    res["замкнутость"] = closure_rows()
    for r in res["замкнутость"]:
        print("замкнутость:", r["мир"], "| порча:", r["порча"], "| нарушены:", r["нарушены"] or "нет")

    res["секунд"] = round(time.time() - t0)
    out = HERE / "experiments" / "results" / f"{a.name}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)
    print(f"время: {res['секунд']} с; итог: {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
