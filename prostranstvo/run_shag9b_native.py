"""Шаг 9б, пункт 4: родная программа модели (model9/native/serve9) на месте
serve.py — в наборах судьи и у обеих реализаций.

Что проверяется (программа модели — в режимах заглушки, как во всех наборах
шага 9; живую модель сверяет experiments/native9.py):

0. сверка видит программу модели: с испорченной копией serve.py (заглушка
   отвечает «СТОП1» вместо «СТОП») журналы обязаны разойтись у обеих
   реализаций — иначе совпадение в п. 1 ничего не значит;
1. журнал не зависит от программы модели: у каждой реализации на случаях
   набора шага 9 журнал с serve9 совпадает с журналом с serve.py — режим 2
   способами netns и probe и режим 1 (diff7.run_both_9: одна реализация на
   обеих сторонах, MODEL_SERVE у сторон разная);
2. законы обеих реализаций при serve9: «К18» (`--suite net`) и набор шага 9
   при model = 2 (netns, probe) и model = 1;
3. прямая сверка двух реализаций при serve9 — те же три режима;
4. перебор попыток хода модели при model = 2 с serve9 у обеих: закон
   перебора и совпадение попытка в попытку.

python3 run_shag9b_native.py --serve9 ПРОГРАММА [--rust ИСПОЛНИТЕЛЬ] [--quick]
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
from run_shag9b_vtoraya import laws

H = D.H
RESULTS = Path(__file__).resolve().parent / "experiments" / "results"
SERVE_PY = Path(__file__).resolve().parent / "model9" / "serve.py"
MODES = (("model = 2, netns", 2, None), ("model = 2, probe", 2, "probe"), ("model = 1, netns", 1, None))


def verdicts(rows: list[dict]) -> dict:
    return {"случаев": len(rows), "итоги": dict(Counter(r["verdict"] for r in rows)),
            "расхождения": [{"name": r["name"], "diff": r["diff"]} for r in rows if r["verdict"] != "совпали"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve9", required=True)
    ap.add_argument("--rust", default=str(D.JUDGE.parent / "executor_b" / "target" / "release" / "executor7"))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--name", default="shag9b_native")
    a = ap.parse_args()
    serve9 = str(Path(a.serve9).resolve())
    root = Path(tempfile.mkdtemp(prefix="shag9bn_"))
    t0 = time.time()
    res: dict = {"serve9": serve9, "rust": a.rust, "quick": a.quick}

    # 0. Порча: та же сверка обязана увидеть другую программу модели.
    spoil = root / "serve_spoil.py"
    text = SERVE_PY.read_text(encoding="utf-8")
    assert text.count('    return "СТОП"\n') == 1
    spoil.write_text(text.replace('    return "СТОП"\n', '    return "СТОП1"\n'), encoding="utf-8")
    spoil.chmod(0o755)
    for impl, exe in (("первая", D.PY_EXE), ("вторая", a.rust)):
        rows = D.run_both_9(exe, root / f"porcha-{impl}", True, first_exe=exe, mode=2, nets=(None, None),
                            envs=({"MODEL_SERVE": str(SERVE_PY)}, {"MODEL_SERVE": str(spoil)}))
        v = verdicts(rows)
        v["поймана"] = v["итоги"].get("совпали", 0) < v["случаев"]
        res[f"порча программы модели, {impl}"] = {k: v[k] for k in ("случаев", "итоги", "поймана")}
        print(f"порча программы модели, {impl}: {v['случаев']} случаев, {v['итоги']}; {time.time() - t0:.0f} с",
              flush=True)

    # 1. serve.py против serve9 у одной реализации. Папки без пробелов: путь
    # к сценарию модели идёт в режиме `--script ФАЙЛ` (контракт §17).
    envs = ({"MODEL_SERVE": str(SERVE_PY)}, {"MODEL_SERVE": serve9})
    n = 0
    for impl, exe in (("первая", D.PY_EXE), ("вторая", a.rust)):
        for name, mode, net in MODES:
            rows = D.run_both_9(exe, root / f"programmy{n}", a.quick, first_exe=exe, mode=mode, nets=(net, net), envs=envs)
            n += 1
            res[f"serve.py против serve9, {impl}, {name}"] = v = verdicts(rows)
            print(f"serve.py против serve9, {impl}, {name}: {v['случаев']} случаев, {v['итоги']}; "
                  f"{time.time() - t0:.0f} с", flush=True)
            for r in v["расхождения"][:5]:
                print(f"   «{r['name']}»: {str(r['diff'])[:240]}")

    # Дальше все исполнители получают serve9.
    H.use_serve(serve9)

    # 2. законы
    for impl, exe in (("первая", D.PY_EXE), ("вторая", a.rust)):
        res[f"законы, {impl}"] = {
            "К18": laws(H.suite_net(exe, root / f"set-{impl}")),
            **{f"9, {name}": laws(H.suite_9(exe, root / f"zakony{i}-{impl}", a.quick, mode, net))
               for i, (name, mode, net) in enumerate(MODES)}}
        for k, v in res[f"законы, {impl}"].items():
            print(f"законы, {impl}, {k}: {v['запусков']} запусков, с нарушениями {v['с нарушениями']}; "
                  f"{time.time() - t0:.0f} с", flush=True)

    # 3. прямая сверка двух реализаций
    for i, (name, mode, net) in enumerate(MODES):
        rows = D.run_both_9(a.rust, root / f"sverka{i}", a.quick, mode=mode, nets=(net, net))
        res[f"сверка реализаций, {name}"] = v = verdicts(rows)
        print(f"сверка реализаций, {name}: {v['случаев']} случаев, {v['итоги']}; {time.time() - t0:.0f} с", flush=True)

    # 4. перебор попыток при model = 2
    rows_b = enum8v.enumerate_all(lambda kind: enum8v.JsonExe(shlex.split(a.rust), kind, 2), 2)
    rows_a = enum8v.enumerate_all(lambda kind: enum8v.JsonExe(enum8v.PY_EXE, kind, 2), 2)
    sa, sb = enum8v.summary(rows_a), enum8v.summary(rows_b)
    res["перебор, model = 2"] = {"первая": sa, "вторая": sb, "расхождения": compare_attempts(rows_a, rows_b)}
    print(f"перебор при model = 2: первая — попыток {sa.get(1, {}).get('попыток')}, без цены "
          f"{sa.get(1, {}).get('без цены')}; вторая — попыток {sb.get(1, {}).get('попыток')}, без цены "
          f"{sb.get(1, {}).get('без цены')}; расхождений {len(res['перебор, model = 2']['расхождения'])}; "
          f"{time.time() - t0:.0f} с", flush=True)

    res["секунд"] = round(time.time() - t0)
    (RESULTS / f"{a.name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                                            encoding="utf-8")
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
