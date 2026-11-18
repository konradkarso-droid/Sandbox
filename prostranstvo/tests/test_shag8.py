"""Разбор шага 8 на синтетических файлах с известным ответом.

Проверяется и «находит, когда должно» (скачок, связь с температурой,
неучтённая работа, известные наклоны и мощность), и «молчит, когда не
должно» (ровные данные — без скачков; независимая температура — без
связи).
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import run_shag8 as A

KINDS = {  # вид: (наклон, нс на единицу размера; топливо на единицу; бюджет на единицу)
    "счёт": (5.0, 1, 0),
    "память": (7.0, 1, 0),
    "ворота": (2_000_000.0, 12, 0),   # 2 мс на вызов при 12 единицах топлива — неучтённое
    "фиксация": (4_000_000.0, 20, 1),
    "работа узла": (3_000_000.0, 15, 1),
}
SIZES = {"счёт": [1000 * 2 ** k for k in range(8)], "память": [1000 * 2 ** k for k in range(8)],
         "ворота": [2 ** k for k in range(1, 9)], "фиксация": [2 ** k for k in range(1, 9)],
         "работа узла": [10 * k for k in range(1, 9)]}


def synth(path: Path, jump_round=None, temp_follows=False, seed=1, phone=True) -> None:
    rng = random.Random(seed)
    lines = [{"type": "заголовок", "t": 0, "device": {"where": "синтетика"}, "plan": "телефон"}]
    t = 0
    step = 0
    I_REST, I_WORK = 300_000, 900_000  # мкА; ΔP = 0,6 А × 4 В = 2,4 Вт

    def samples(t0, t1, part, kind, rnd, current, temp):
        for tt in range(t0, t1, 500):
            lines.append({"type": "батарея", "t": tt, "part": part, "kind": kind, "round": rnd,
                          "b": {"i_raw": -current + rng.randint(-5000, 5000), "q_uah": 4_000_000 - tt, "v_mv": 4000,
                                "temp_c": temp, "level": 80, "plugged": False}})

    lines.append({"type": "шаг", "t": t, "i": step, "part": "фон", "kind": None, "round": 0, "ms": 60000})
    samples(t, t + 60000, "фон", None, 0, I_REST, 30.0)
    t += 60000
    step += 1
    for rnd in range(1, 5):
        for k, (b, fuel, spent) in KINDS.items():
            temp = 30.0 + (rnd * 2 if temp_follows else rng.choice([0, 3, 1, 4, 2]))
            slope = b * (1.3 if (jump_round and rnd >= jump_round) else 1.0)
            if temp_follows:
                slope = b * (1 + 0.05 * rnd)
            lines.append({"type": "шаг", "t": t, "i": step, "part": "работа", "kind": k, "round": rnd, "ms": 30000})
            samples(t, t + 30000, "работа", k, rnd, I_WORK, temp)
            for n in SIZES[k]:
                x = n * fuel if k in A.FUEL_KINDS else n
                m = 50_000 + slope * x * (1 + rng.gauss(0, 0.002))
                lines.append({"type": "замер", "t": t, "step": step, "kind": k, "round": rnd, "file": f"{k}_{n}",
                              "n": n, "count": 20, "mean_ns": m, "sd_ns": m * 0.01, "min_ns": m, "max_ns": m,
                              "fuel_min": n * fuel, "fuel_max": n * fuel, "spent_min": n * spent,
                              "spent_max": n * spent, "outcomes": {"завершён": 20}})
            lines.append({"type": "блок", "t": t + 30000, "step": step, "kind": k, "round": rnd, "t0": t,
                          "t1": t + 30000, "paused_ms": 0, "trials": 160, "peak_kb": 3000})
            t += 30000
            step += 1
            lines.append({"type": "шаг", "t": t, "i": step, "part": "покой", "kind": None, "round": rnd, "ms": 20000})
            samples(t, t + 20000, "покой", None, rnd, I_REST, temp)
            t += 20000
            step += 1
    lines.append({"type": "шаг", "t": t, "i": step, "part": "каналы", "kind": None, "round": 0, "ms": 0})
    r2 = random.Random(8)
    for i in range(128):
        s = r2.randrange(2)
        lines.append({"type": "канал", "t": t, "channel": "время, 2 уровня", "i": i, "sent": s,
                      "t_ns": (1_000_000 if s == 0 else 8_000_000) * (1 + rng.gauss(0, 0.01)), "fuel": 0, "spent": 0,
                      "outcome": "завершён", "deliveries": 0})
    names = ["завершён", "ловушка: unreachable", "ловушка: обращение за границу памяти", "ловушка: топливо кончилось"]
    for i in range(64):
        s = r2.randrange(4)
        lines.append({"type": "канал", "t": t, "channel": "исход", "i": i, "sent": s, "t_ns": 100_000, "fuel": 0,
                      "spent": 0, "outcome": names[s], "deliveries": 0})
    for i in range(64):
        s = r2.randrange(2)
        lines.append({"type": "канал", "t": t, "channel": "размер вывода", "i": i, "sent": s, "t_ns": 5_000_000,
                      "fuel": 0, "spent": s + 1, "outcome": "завершён", "deliveries": s + 1})
    lines.append({"type": "конец", "t": t, "why": "Готово", "complete": True})
    if not phone:
        lines = [x for x in lines if x["type"] != "батарея"]
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")


def test_known_slopes_and_silence(tmp_path):
    f = tmp_path / "ph.txt"
    synth(f)
    r = A.analyse(f)
    for k, (b, _, _) in KINDS.items():
        st = r["slopes"][k]["steady"]
        assert abs(st["b"] / b - 1) < 0.02, (k, st["b"])
        assert st["r2"] > 0.99
    assert r["jumps"] == []                      # молчит: ровные данные
    assert r["h2"]["verdict"] == "supported"     # молчит: температура не связана с наклоном


def test_unaccounted_gates_only(tmp_path):
    f = tmp_path / "ph.txt"
    synth(f)
    f1 = A.analyse(f)["f1"]
    assert f1["verdict"] == "refuted"
    assert len(f1["unaccounted"]) == 1 and "ворота" in f1["unaccounted"][0]


def test_jump_found(tmp_path):
    f = tmp_path / "ph.txt"
    synth(f, jump_round=3)
    j = A.analyse(f)["jumps"]
    assert len(j) == len(KINDS) and all("круг 2 → 3" in x for x in j)


def test_temperature_link_found(tmp_path):
    f = tmp_path / "ph.txt"
    synth(f, temp_follows=True)
    h = A.analyse(f)["h2"]
    assert h["verdict"] == "refuted" and h["r"] > 0.9


def test_energy(tmp_path):
    f = tmp_path / "ph.txt"
    synth(f)
    e = A.analyse(f)["energy"]
    assert "мкА" in e["note"]
    for k in KINDS:
        assert abs(e["kinds"][k]["dP_w"] - 2.4) < 0.05
    # энергия единицы = ΔP × время единицы: счёт — 2,4 Вт × 5 нс = 12 нДж
    assert abs(e["kinds"]["счёт"]["nJ_per_unit"] - 12.0) < 0.5


def test_channels(tmp_path):
    f = tmp_path / "ph.txt"
    synth(f)
    c = A.analyse(f)["channels"]
    assert c["исход"]["errors"] == 0 and abs(c["исход"]["bits_per_symbol"] - c["исход"]["max_bits"]) < 0.3
    assert c["время, 2 уровня"]["errors"] == 0 and c["время, 2 уровня"]["bits_per_symbol"] > 0.9
    assert c["размер вывода"]["budget_per_symbol"] > 1


def test_mutual_information_edges():
    assert A.mutual_information([(0, 0), (1, 1)] * 10) == 1.0
    assert abs(A.mutual_information([(0, 0), (0, 1), (1, 0), (1, 1)] * 10)) < 1e-12   # шум — 0 бит


def test_server_file_without_battery(tmp_path):
    f = tmp_path / "sv.txt"
    synth(f, phone=False)
    r = A.analyse(f)
    assert r["energy"]["note"] == "нет показаний батареи"
    assert r["h2"]["verdict"] == "нельзя сказать"
    txt = A.report(A.analyse(tmp_path / "sv.txt"), r)
    assert "телефон / сервер" in txt and math.isfinite(r["slopes"]["счёт"]["steady"]["b"])
