"""Шаг 8б: цена одной записи журнала и доля байтов в цене вызова ворот.

Меряется на сервере исполнителем второй реализации (тем же, что стоит на
телефоне): время шага run для программ из N одинаковых вызовов; наклон по
N — цена одного вызова (спецификация шага 8б, §2–3).

Виды:
- справочник — 1 запись на вызов;
- чтение 1 байта и 4096 байт — 1 запись, разница — доля байтов;
- запись 1 и 16 байт в снимок — 1 запись, разница — доля байтов;
- вычисление узла без выброса и пара «вычисление + выброс» — разница —
  цена выброса (2 записи).

python3 experiments/measure8b.py --exe ИСПОЛНИТЕЛЬ
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments import programs5b as p5b  # noqa: E402

REPEAT = 7
SIZES = (16, 32, 64, 128, 240)  # 240 ≤ 255 узлов графа при K = 8 и ≤ памяти 255
CFG = {"budget": 10_000_000, "graph": "M3т", "levels": 8, "memory": 255}


def programs() -> dict[str, dict[int, str]]:
    """Вид → {N: текст модуля}. Снимок сначала наполняется 4096 байтами
    (одна запись), чтобы было что читать."""
    fill = [("ворота", p5b.CAP_SNAP_W, 1, 0, 4096)]
    out = {"справочник": {}, "чтение 1 Б": {}, "чтение 4096 Б": {}, "запись 1 Б": {}, "запись 16 Б": {},
           "вычисление": {}, "вычисление + выброс": {}}
    for n in SIZES:
        out["справочник"][n] = p5b.module([("имя", "цена")] * n)
        out["чтение 1 Б"][n] = p5b.module(fill + [("ворота", p5b.CAP_SNAP_R, 0, 8192, 1)] * n)
        out["чтение 4096 Б"][n] = p5b.module(fill + [("ворота", p5b.CAP_SNAP_R, 0, 8192, 4096)] * n)
        out["запись 1 Б"][n] = p5b.module([("ворота", p5b.CAP_SNAP_W, 1, 0, 1)] * n)
        out["запись 16 Б"][n] = p5b.module([("ворота", p5b.CAP_SNAP_W, 1, 0, 16)] * n)
        # Узлы 0…n−1 по возрастанию: предки всегда раньше, памяти 255 хватает без выбросов.
        out["вычисление"][n] = p5b.module([("узел", j) for j in range(n)])
        # Узел 0 не имеет предков: «вычислить — выбросить» можно повторять.
        out["вычисление + выброс"][n] = p5b.module([("узел", 0), ("выброс", 0)] * n)
    return out


class Exe:
    def __init__(self, exe: str, root: str):
        self.p = subprocess.Popen([exe, "--root", root, "--kind", "5б"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, encoding="utf-8")

    def call(self, cmd, *args):
        self.p.stdin.write(json.dumps({"cmd": cmd, "args": list(args)}, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        r = json.loads(self.p.stdout.readline())
        if r["status"] != "ok":
            raise RuntimeError(f"{cmd}: {r}")
        return r["value"]

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def measure(exe: str) -> dict:
    import shutil

    import wasmtime
    progs = programs()
    res = {}
    with tempfile.TemporaryDirectory() as root:
        e = Exe(exe, root)
        for kind, by_n in progs.items():
            xs, ys = [], []
            for n, wat in by_n.items():
                mod = base64.b64encode(bytes(wasmtime.wat2wasm(wat))).decode()
                for _ in range(REPEAT):
                    wd = e.call("new", CFG, None)
                    lo = e.call("load", 1, {"b64": mod})
                    if lo[0] != "загружен":
                        raise RuntimeError(f"{kind} {n}: {lo}")
                    t0 = time.perf_counter_ns()
                    ro = e.call("run", 1)
                    t1 = time.perf_counter_ns()
                    if ro[0] != "завершён":
                        raise RuntimeError(f"{kind} {n}: {ro}")
                    d = e.call("dump")["contours"]["1"]
                    if d.get("refusals"):
                        raise RuntimeError(f"{kind} {n}: отказы ворот {d['refusals']}")
                    xs.append(n)
                    ys.append((t1 - t0) / 1000)
                    shutil.rmtree(wd, ignore_errors=True)
            b, a = np.polyfit(xs, ys, 1)
            res[kind] = {"us_per_call": float(b), "intercept_us": float(a)}
        e.close()
    return res


def single_write(exe: str, reps: int = 200) -> dict:
    """Одна запись 1 байта против одной записи 4096 байт, много повторов.
    Разница средних и её стандартная ошибка — доля байтов записи прямо, а не
    продолжением наклона с 1 до 16 байт (там разница в 15 байт тонет в шуме,
    и продолжение до 4096 байт умножает шум в 273 раза)."""
    import shutil

    import wasmtime
    mods = {b: base64.b64encode(bytes(wasmtime.wat2wasm(p5b.module([("ворота", p5b.CAP_SNAP_W, 1, 0, b)])))).decode()
            for b in (1, 4096)}
    times: dict[int, list[float]] = {1: [], 4096: []}
    with tempfile.TemporaryDirectory() as root:
        e = Exe(exe, root)
        for i in range(reps):
            for b in ((1, 4096) if i % 2 == 0 else (4096, 1)):   # чередование — против дрейфа
                wd = e.call("new", CFG, None)
                e.call("load", 1, {"b64": mods[b]})
                t0 = time.perf_counter_ns()
                ro = e.call("run", 1)
                t1 = time.perf_counter_ns()
                if ro[0] != "завершён":
                    raise RuntimeError(f"запись {b}: {ro}")
                times[b].append((t1 - t0) / 1000)
                shutil.rmtree(wd, ignore_errors=True)
        e.close()
    m1, m4 = np.mean(times[1]), np.mean(times[4096])
    se = float(np.sqrt(np.var(times[1], ddof=1) / reps + np.var(times[4096], ddof=1) / reps))
    return {"1 Б, мкс": float(m1), "4096 Б, мкс": float(m4), "разница, мкс": float(m4 - m1), "ошибка разницы, мкс": se}


def decide(res: dict) -> dict:
    """Решения по объявленным правилам спецификации шага 8б (§2)."""
    base = res["справочник"]["us_per_call"]
    read_bytes = res["чтение 4096 Б"]["us_per_call"] - res["чтение 1 Б"]["us_per_call"]
    write_bytes = res["одна запись"]["разница, мкс"]
    discard = res["вычисление + выброс"]["us_per_call"] - res["вычисление"]["us_per_call"]
    return {
        "запись журнала, мкс": base,
        "доля байтов чтения при 4096 Б": read_bytes / base,
        "доля байтов записи при 4096 Б (одна запись 4096 Б против 1 Б)": write_bytes / base,
        "плата за байты нужна": max(read_bytes, write_bytes) / base > 0.10,
        "выброс, мкс": discard,
        "выброс / справочник (правило «2 записи» даёт 2)": discard / base,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--out", default="experiments/results/shag8b_measure.json")
    a = ap.parse_args()
    res = measure(a.exe)
    res["одна запись"] = single_write(a.exe)
    dec = decide(res)
    Path(a.out).write_text(json.dumps({"kinds": res, "decision": dec}, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in res.items():
        print(f"{k}: {v['us_per_call']:.1f} мкс на вызов" if "us_per_call" in v else f"{k}: {v}")
    for k, v in dec.items():
        print(f"{k}: {v:.3f}" if isinstance(v, float) else f"{k}: {v}")


if __name__ == "__main__":
    main()
