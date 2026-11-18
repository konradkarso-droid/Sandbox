"""Нагрузки шага 8: модули WebAssembly и план для стенда (assets/w8/).

Пять видов работы (спецификация шага 8, §3) по 8 размеров и программы
каналов (§6). Размеры циклов подобраны так, чтобы самый большой укладывался
в топливо контура (3 000 000): проверка — check_fuel() через исполнитель.

Программы «фиксация», «ворота» и «работа узла» собираются генератором
противников шага 5б (experiments/programs5b.py): там уже есть ходы
«сообщение», «имя» и путь жадного агента по графу.

python3 phone8/workloads8.py [--exe ИСПОЛНИТЕЛЬ]  — собрать; с исполнителем —
ещё и проверить топливо и исходы.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core.mir3 import World  # noqa: E402
from experiments import programs5b as p5b  # noqa: E402

OUT = HERE / "assets" / "w8"
FUEL = 3_000_000
LEVELS, MEMORY = 6, 4
CFG = {"budget": 1_000_000, "graph": "M3т", "levels": LEVELS, "memory": MEMORY}
KINDS = ["счёт", "память", "ворота", "фиксация", "работа узла"]
SEED = 8
# Объявленные числа, не подобранные: верх циклов — с запасом под топливо
# контура; ворот и фиксаций — чтобы один замер не длился секундами.
ARITH_MAX, MEM_MAX = 160_000, 112_000
GATES_MAX, FIX_MAX = 256, 256

ARITH = '''(module
  (func (export "main")
    (local $i i32) (local $x i32)
    (local.set $i (i32.const {n}))
    (block $d (loop $l
      (br_if $d (i32.eqz (local.get $i)))
      (local.set $x (i32.add (i32.mul (local.get $x) (i32.const 1103515245)) (i32.const 12345)))
      (local.set $i (i32.sub (local.get $i) (i32.const 1)))
      (br $l)))))'''

MEMORY_LOOP = '''(module
  (memory (export "memory") 1)
  (func (export "main")
    (local $i i32) (local $a i32)
    (local.set $i (i32.const {n}))
    (block $d (loop $l
      (br_if $d (i32.eqz (local.get $i)))
      (local.set $a (i32.add (i32.const 64) (i32.shl (i32.and (local.get $i) (i32.const 8191)) (i32.const 2))))
      (i32.store (local.get $a) (i32.add (i32.load (local.get $a)) (local.get $i)))
      (local.set $i (i32.sub (local.get $i) (i32.const 1)))
      (br $l)))))'''

OUTCOME = {
    0: '(module (func (export "main")))',
    1: '(module (func (export "main") unreachable))',
    2: '(module (memory (export "memory") 1) (func (export "main") (drop (i32.load (i32.const 200000)))))',
    3: '(module (func (export "main") (loop $l (br $l))))',
}


def wasm(wat: str) -> bytes:
    import wasmtime  # только как переводчик текста WebAssembly в байты
    return bytes(wasmtime.wat2wasm(wat))


def sizes(top: int) -> list[int]:
    """8 размеров: top, top/2, …, top/128 — по возрастанию."""
    return [max(1, top >> k) for k in range(7, -1, -1)]


def node_moves(count: int) -> list[tuple]:
    """Начало пути жадного агента, в котором ровно count вычислений узла.
    Доставки из пути убраны: нагрузка — только работа узлов и выбросы."""
    path = [m for m in p5b.greedy(World("M3т", LEVELS, MEMORY)) if m[0] in ("узел", "выброс")]
    out, done = [], 0
    for m in path:
        if m[0] == "узел":
            if done == count:
                break
            done += 1
        out.append(m)
    if done < count:
        raise ValueError(f"в пути жадного агента только {done} вычислений, нужно {count}")
    return out


def build() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    files: dict[str, bytes] = {}
    work: dict[str, list] = {k: [] for k in KINDS}
    for n in sizes(ARITH_MAX):
        files[f"schet_{n}.wasm"] = wasm(ARITH.format(n=n))
        work["счёт"].append({"file": f"schet_{n}.wasm", "n": n})
    for n in sizes(MEM_MAX):
        files[f"pamyat_{n}.wasm"] = wasm(MEMORY_LOOP.format(n=n))
        work["память"].append({"file": f"pamyat_{n}.wasm", "n": n})
    for n in sizes(GATES_MAX):
        files[f"vorota_{n}.wasm"] = wasm(p5b.module([("имя", "цена")] * n))
        work["ворота"].append({"file": f"vorota_{n}.wasm", "n": n})
    for n in sizes(FIX_MAX):
        files[f"fiks_{n}.wasm"] = wasm(p5b.module([("сообщение", "А")] * n))
        work["фиксация"].append({"file": f"fiks_{n}.wasm", "n": n})
    total = sum(1 for m in p5b.greedy(World("M3т", LEVELS, MEMORY)) if m[0] == "узел")
    for n in sorted({max(1, round(total * k / 8)) for k in range(1, 9)}):
        files[f"uzly_{n}.wasm"] = wasm(p5b.module(node_moves(n)))
        work["работа узла"].append({"file": f"uzly_{n}.wasm", "n": n})

    # Каналы: тайна — искусственные символы из генератора с заданным зерном.
    rng = random.Random(SEED)
    t2 = {0: ARITH_MAX // 16, 1: ARITH_MAX // 2}
    t4 = {0: ARITH_MAX // 16, 1: ARITH_MAX // 4, 2: ARITH_MAX // 2, 3: ARITH_MAX * 9 // 10}
    for s, n in t2.items():
        files[f"k_vremya2_{s}.wasm"] = wasm(ARITH.format(n=n))
    for s, n in t4.items():
        files[f"k_vremya4_{s}.wasm"] = wasm(ARITH.format(n=n))
    for s, w in OUTCOME.items():
        files[f"k_ishod_{s}.wasm"] = wasm(w)
    for s in (0, 1):
        files[f"k_razmer_{s}.wasm"] = wasm(p5b.module([("сообщение", "А")] * (s + 1)))
    channels = []
    for name, levels, count, prefix in (("время, 2 уровня", 2, 128, "k_vremya2"), ("время, 4 уровня", 4, 128, "k_vremya4"),
                                        ("исход", 4, 64, "k_ishod"), ("размер вывода", 2, 64, "k_razmer")):
        for _ in range(count):
            s = rng.randrange(levels)
            channels.append({"channel": name, "sent": s, "file": f"{prefix}_{s}.wasm"})
    plan = {"cfg": CFG, "kinds": KINDS, "work": work, "channels": channels, "seed": SEED,
            "channel_levels": {"время, 2 уровня": t2, "время, 4 уровня": t4}}
    for name, b in files.items():
        (OUT / name).write_bytes(b)
    (OUT / "plan8.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return plan


def check_fuel(exe: str, plan: dict) -> list[str]:
    """Каждая нагрузка: загружается, завершается, укладывается в топливо.
    Возвращает список нарушений; пустой — всё в порядке."""
    import base64
    bad = []
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

        for kind, ws in plan["work"].items():
            for w in ws:
                call("new", plan["cfg"], None)
                lo = call("load", 1, {"b64": base64.b64encode((OUT / w["file"]).read_bytes()).decode()})
                f0 = call("dump")["contours"]["1"]["fuel"]
                ro = call("run", 1)
                d = call("dump")["contours"]["1"]
                used = f0 - d["fuel"]
                w["fuel"], w["spent"] = used, d["spent"]
                if lo[0] != "загружен" or ro[0] != "завершён" or d["fuel"] < FUEL // 10:
                    bad.append(f"{kind} {w['file']}: загрузка {lo}, запуск {ro}, топливо {used}, осталось {d['fuel']}")
        p.stdin.close()
        p.wait()
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe")
    a = ap.parse_args()
    plan = build()
    print(f"собрано: {len(list(OUT.glob('*.wasm')))} модулей, каналов {len(plan['channels'])}")
    if a.exe:
        bad = check_fuel(a.exe, plan)
        for kind, ws in plan["work"].items():
            print(kind, [(w["n"], w["fuel"], w["spent"]) for w in ws])
        print("нарушений:", len(bad))
        for b in bad:
            print("  ", b)


if __name__ == "__main__":
    main()
