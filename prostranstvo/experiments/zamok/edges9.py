"""Края контракта §17, которых нет в наборе обвязки: прямая сверка первой
и второй реализаций (поля модели при model = 0, адрес вне памяти при
длине 0, граница памяти, предел ответа 4096, восстановление без модели).

python3 experiments/zamok/edges9.py — из папки prostranstvo."""

import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, "/home/claude/prostranstvo")
from experiments import diff7 as D
from experiments import programs5b as p5b
H = D.H
A = D.PY_EXE
B = "/home/claude/rust7/executor_b/target/release/executor7"
root = Path(tempfile.mkdtemp(prefix="edges9_"))
cm = p5b.CAP_MODEL
cases = {
    "model=0, нестандартные A и U": ([("сообщение", "А")], {**H.P9, "model": 0, "model_answer": 64, "model_byte_unit": 906}),
    "адрес вне памяти, длина 0": ([("ворота", cm, 2, 70000, 0), ("ворота", cm, 2, -1, 0), ("ворота", cm, 2, 65536, 0)], H.P9),
    "адрес = длина памяти, длина 4": ([("ворота", cm, 2, 65536, 4)], H.P9),
    "оп 2 на модель, предел ответа 4096 у края": ([("ворота", cm, 2, 65536 - 4096, 4)], {**H.P9, "model_answer": 4096}),
}
for name, (moves, cfg) in cases.items():
    snaps = {}
    for impl, exe in (("А", A), ("Б", B)):
        d = H.ExeDriver(exe, root / impl / name.replace(" ", "_"), "5б", model="--stub")
        src = p5b.module(moves)
        r = H.one(d, name, {1: src, 2: src}, {**cfg, **D.FIX}, rounds=1, extra={"ext_world": H.W5})
        d.close()
        snaps[impl] = D.snapshot(r)
        recs = [json.loads(x) for x in (Path(r["workdir"]) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]
        u = next(x for x in recs if x["kind"] == "учёт")
        codes = [x["code"] for x in recs if x["kind"] == "отказ ворот" and x.get("contour") == 1]
        print(f"{impl} {name}: учёт {{{', '.join(k for k in u if k.startswith('model'))}}}, отказы {codes}, нарушения {H.broken(r)}")
    print("   =>", D.compare(snaps["А"], snaps["Б"]))

# восстановление при model = 1 исполнителем без модели
for impl, exe in (("А", A), ("Б", B)):
    d = H.ExeDriver(exe, root / impl / "rec", "5б", model="--stub")
    st, wd = d.call("new", {**H.P9, **D.FIX}, None)
    d.call("load", 1, p5b.module([("модель", "ЭХО а")])); d.call("run", 1)
    d.close()
    n = int(Path(wd).name[3:])
    d2 = H.ExeDriver(exe, root / impl / "rec", "5б")
    st2, v2 = d2.call("recover", n, None)
    st3, v3 = d2.call("load", 1, p5b.module([("модель", "ЭХО б")]))
    st4, v4 = d2.call("run", 1)
    d2.close()
    recs = [json.loads(x) for x in (Path(wd) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]
    tail = [(x["kind"], x.get("code"), (x.get("signature") or [None])[0]) for x in recs[-3:]]
    print(f"{impl} восстановление без модели: recover {st2} {str(v2)[:80]}; load {st3}; run {st4} {v4}; хвост {tail}")
