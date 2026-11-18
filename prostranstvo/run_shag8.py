"""Шаг 8: разбор файла замеров стенда (телефон или сервер).

python3 run_shag8.py ФАЙЛ_ТЕЛЕФОНА [--server ФАЙЛ_СЕРВЕРА] [--out ПАПКА]

Что считает (спецификация шага 8, §9):
- наклон и свободный член прямой «время — размер» для каждого вида работы,
  по каждому кругу и по устойчивому режиму (круги 2–4);
- холодный круг против устойчивого, скачки между кругами;
- цену единицы топлива, вызова ворот, единицы бюджета во времени;
- энергию: разность мощности в работе и в соседнем покое, умноженная на
  время единицы;
- пиковую память, температуру;
- связь наклона с температурой (Н2);
- ёмкость каналов (О10) по таблице «послано — принято».

Пороги вердиктов объявлены здесь до прогона, не подобраны (ниже, ПОРОГИ).
Чего не умеет: ток в телефоне — грубый прибор; энергия — оценка с
разбросом по кругам, а не измерение энергометром.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

# Объявленные числа, не подобранные.
ПОРОГИ = {
    "линейность_R2": 0.98,       # прямая объясняет не меньше этой доли разброса
    "скачок_се": 3.0,            # скачок — изменение наклона больше 3 стандартных ошибок…
    "скачок_доля": 0.10,         # …и больше 10 %
    "неучтённое_раз": 10.0,      # работа дороже своей цены в топливе больше чем в 10 раз
    "н2_r": 0.5,                 # связь с температурой: |r| от 0,5…
    "н2_p": 0.05,                # …при p < 0,05
    "мА_если_меньше": 20_000,    # медиана |тока| в фоне меньше — значит, система отдаёт мА, а не мкА
}
FUEL_KINDS = ("счёт", "память")          # размер — топливо
UNIT = {"ворота": "вызов ворот", "фиксация": "фиксация", "работа узла": "вычисление узла"}


def read(path: Path) -> dict:
    out = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["type"]].append(r)
    return out


# ---- прямая по средним с весами

def fit(xs, ys, ws) -> dict | None:
    """Взвешенная прямая y = a + b x. Возвращает a, b, стандартную ошибку b
    и долю разброса, объяснённую прямой (R²). None — данных мало."""
    x, y, w = (np.asarray(v, dtype=float) for v in (xs, ys, ws))
    if len(x) < 3 or len(set(x)) < 3 or w.sum() <= 0:
        return None
    W = w / w.sum()
    mx, my = (W * x).sum(), (W * y).sum()
    sxx = (W * (x - mx) ** 2).sum()
    b = (W * (x - mx) * (y - my)).sum() / sxx
    a = my - b * mx
    res = y - (a + b * x)
    ss_res, ss_tot = (W * res ** 2).sum(), (W * (y - my) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 1.0
    n = len(x)
    se = math.sqrt(max(ss_res, 0) / max(n - 2, 1) / sxx) if sxx > 0 else float("inf")
    return {"a": a, "b": b, "se": se, "r2": r2, "points": n}


def size_of(row: dict, kind: str) -> float:
    """Размер замера: топливо для счёта и памяти, число действий — для остального."""
    if kind in FUEL_KINDS:
        return (row["fuel_min"] + row["fuel_max"]) / 2
    return row["n"]


def good_rows(d: dict, kind: str, rounds=None) -> list[dict]:
    return [r for r in d["замер"] if r["kind"] == kind and r["count"] > 0
            and set(r["outcomes"]) == {"завершён"} and (rounds is None or r["round"] in rounds)]


def slopes(d: dict) -> dict:
    """Наклоны по видам: по каждому кругу и по устойчивому режиму."""
    out = {}
    kinds = list(dict.fromkeys(r["kind"] for r in d["замер"]))
    rounds = sorted({r["round"] for r in d["замер"]})
    for k in kinds:
        per = {}
        for rnd in rounds:
            rows = good_rows(d, k, {rnd})
            f = fit([size_of(r, k) for r in rows], [r["mean_ns"] for r in rows], [r["count"] for r in rows])
            if f:
                per[rnd] = f
        steady_r = [r for r in rounds if r >= 2] or rounds
        rows = good_rows(d, k, set(steady_r))
        steady = fit([size_of(r, k) for r in rows], [r["mean_ns"] for r in rows], [r["count"] for r in rows])
        # топливо и бюджет на единицу размера — для «неучтённого»
        rows_all = good_rows(d, k)
        fuel_fit = fit([r["n"] for r in rows_all], [(r["fuel_min"] + r["fuel_max"]) / 2 for r in rows_all],
                       [1] * len(rows_all))
        spent_fit = fit([r["n"] for r in rows_all], [(r["spent_min"] + r["spent_max"]) / 2 for r in rows_all],
                        [1] * len(rows_all))
        anomalies = sum(1 for r in d["замер"] if r["kind"] == k and r["count"] > 0 and set(r["outcomes"]) != {"завершён"})
        unstable = sum(1 for r in rows_all if r["fuel_min"] != r["fuel_max"] or r["spent_min"] != r["spent_max"])
        out[k] = {"rounds": per, "steady": steady, "fuel_per_unit": fuel_fit["b"] if fuel_fit else None,
                  "spent_per_unit": spent_fit["b"] if spent_fit else None, "anomalies": anomalies,
                  "unstable_cost": unstable}
    return out


def jumps(sl: dict) -> list[str]:
    """Скачки наклона между соседними кругами — сверх объявленных порогов."""
    found = []
    for k, s in sl.items():
        rs = sorted(s["rounds"])
        for r1, r2 in zip(rs, rs[1:]):
            f1, f2 = s["rounds"][r1], s["rounds"][r2]
            diff = abs(f2["b"] - f1["b"])
            se = math.hypot(f1["se"], f2["se"])
            if diff > ПОРОГИ["скачок_се"] * se and diff > ПОРОГИ["скачок_доля"] * abs(f1["b"]):
                found.append(f"{k}: круг {r1} → {r2}, наклон {f1['b']:.4g} → {f2['b']:.4g}")
    return found


# ---- энергия

def current_scale(d: dict) -> tuple[float, str]:
    """Множитель к сырому току, чтобы получить амперы, и пояснение."""
    vals = [abs(s["b"]["i_raw"]) for s in d["батарея"] if s["b"].get("i_raw") is not None and s["part"] == "фон"]
    vals = vals or [abs(s["b"]["i_raw"]) for s in d["батарея"] if s["b"].get("i_raw") is not None]
    if not vals:
        return float("nan"), "тока нет"
    med = float(np.median(vals))
    if med < ПОРОГИ["мА_если_меньше"]:
        return 1e-3, f"ток в мА (медиана |тока| {med:.0f})"
    return 1e-6, f"ток в мкА (медиана |тока| {med:.0f})"


def power(samples: list[dict], scale: float) -> float | None:
    p = [abs(s["b"]["i_raw"]) * scale * s["b"]["v_mv"] / 1000 for s in samples
         if s["b"].get("i_raw") is not None and s["b"].get("v_mv")]
    return float(np.mean(p)) if p else None


def energy(d: dict, sl: dict) -> dict:
    """ΔP — мощность в блоке работы минус средняя в соседних покоях; энергия
    единицы = ΔP × время единицы (наклон). Допущение: мощность во время
    run такая же, как в среднем по блоку (там же new, load, выгрузки)."""
    scale, note = current_scale(d)
    if math.isnan(scale):
        return {"note": note}
    steps = {s["i"]: s for s in d["шаг"]}
    samples = d["батарея"]
    by_kind = defaultdict(list)
    for blk in d["блок"]:
        i = blk["step"]
        work = [s for s in samples if blk["t0"] <= s["t"] <= blk["t1"] and s["part"] == "работа"]
        rest = []
        for j in (i - 1, i + 1):
            st = steps.get(j)
            if st and st["part"] in ("покой", "фон"):
                t_end = steps[j + 1]["t"] if (j + 1) in steps else float("inf")
                rest += [s for s in samples if st["t"] <= s["t"] < t_end and s["part"] in ("покой", "фон")]
        pw, pr = power(work, scale), power(rest, scale)
        if pw is None or pr is None:
            continue
        by_kind[blk["kind"]].append({"round": blk["round"], "dP_w": pw - pr, "P_work": pw, "P_rest": pr})
    out = {"note": note, "kinds": {}}
    for k, bl in by_kind.items():
        steady = [b["dP_w"] for b in bl if b["round"] >= 2] or [b["dP_w"] for b in bl]
        dp = float(np.mean(steady))
        sdp = float(np.std(steady, ddof=1)) if len(steady) > 1 else float("nan")
        b_ns = sl.get(k, {}).get("steady", {}) or {}
        per = dp * b_ns.get("b", float("nan"))  # Вт × нс = нДж
        out["kinds"][k] = {"dP_w": dp, "dP_sd": sdp, "blocks": bl, "nJ_per_unit": per,
                           "P_rest_w": float(np.mean([b["P_rest"] for b in bl]))}
    q = [s["b"]["q_uah"] for s in samples if s["b"].get("q_uah") is not None]
    if len(q) > 1:
        out["charge_drop_uah"] = q[0] - q[-1]
    return out


# ---- температура и Н2

def temperature(d: dict) -> dict:
    t = [(s["t"], s["b"]["temp_c"]) for s in d["батарея"] if s["b"].get("temp_c") is not None]
    if not t:
        return {}
    return {"start": t[0][1], "max": max(x for _, x in t), "end": t[-1][1]}


def h2(d: dict) -> dict:
    """Связь наклона блока (относительно среднего по виду) с температурой
    батареи в этом блоке. Проверка перестановками — без допущений о
    распределении."""
    temps = [(s["t"], s["b"]["temp_c"]) for s in d["батарея"] if s["b"].get("temp_c") is not None]
    if not temps:
        return {"verdict": "нельзя сказать", "why": "нет показаний температуры (сервер)"}
    xs, ys = [], []
    kinds = {r["kind"] for r in d["замер"]}
    for k in kinds:
        per_block = []
        for blk in [b for b in d["блок"] if b["kind"] == k]:
            rows = [r for r in good_rows(d, k) if r["step"] == blk["step"]]
            f = fit([size_of(r, k) for r in rows], [r["mean_ns"] for r in rows], [r["count"] for r in rows])
            tt = [x for t, x in temps if blk["t0"] <= t <= blk["t1"]]
            if f and tt:
                per_block.append((f["b"], float(np.mean(tt))))
        if len(per_block) >= 2:
            m = np.mean([b for b, _ in per_block])
            for b, t in per_block:
                xs.append(t)
                ys.append(b / m)
    if len(xs) < 6 or np.std(xs) == 0 or np.std(ys) == 0:
        return {"verdict": "нельзя сказать", "why": f"мало блоков или температура не менялась ({len(xs)} блоков)"}
    r = float(np.corrcoef(xs, ys)[0, 1])
    rng = np.random.default_rng(8)
    y = np.asarray(ys)
    perm = sum(abs(np.corrcoef(xs, rng.permutation(y))[0, 1]) >= abs(r) for _ in range(5000))
    p = (perm + 1) / 5001
    shared = abs(r) >= ПОРОГИ["н2_r"] and p < ПОРОГИ["н2_p"]
    return {"r": r, "p": p, "blocks": len(xs), "temp_range": (min(xs), max(xs)),
            "verdict": "refuted" if shared else "supported",
            "why": ("наклон следует за температурой — у повторных замеров общий скрытый источник" if shared
                    else "заметной связи наклона с температурой нет")}


# ---- каналы

def mutual_information(pairs: list[tuple]) -> float:
    n = len(pairs)
    if n == 0:
        return float("nan")
    joint = Counter(pairs)
    px = Counter(a for a, _ in pairs)
    py = Counter(b for _, b in pairs)
    return sum(c / n * math.log2((c / n) / (px[a] / n * py[b] / n)) for (a, b), c in joint.items())


def channels(d: dict) -> dict:
    by = defaultdict(list)
    for r in d["канал"]:
        by[r["channel"]].append(r)
    out = {}
    for ch, rows in by.items():
        rows.sort(key=lambda r: r["i"])
        half = len(rows) // 2
        train, test = rows[:half], rows[half:]
        levels = len({r["sent"] for r in rows})
        if ch.startswith("время"):
            means = {}
            for s in {r["sent"] for r in train}:
                means[s] = float(np.mean([math.log(r["t_ns"]) for r in train if r["sent"] == s]))

            def decode(r):
                return min(means, key=lambda s: abs(math.log(r["t_ns"]) - means[s]))
            seen = "время run"
        else:
            key = "outcome" if ch == "исход" else "deliveries"
            table = defaultdict(Counter)
            for r in train:
                table[r[key]][r["sent"]] += 1

            def decode(r, key=key, table=table):
                return table[r[key]].most_common(1)[0][0] if r[key] in table else None
            seen = "исход run" if key == "outcome" else "число доставок в выход"
        pairs = [(r["sent"], decode(r)) for r in test]
        mi = mutual_information(pairs)
        err = sum(1 for a, b in pairs if a != b) / len(pairs) if pairs else float("nan")
        t_mean = float(np.mean([r["t_ns"] for r in rows])) / 1e9
        out[ch] = {"seen": seen, "levels": levels, "symbols": len(rows), "test": len(pairs), "errors": err,
                   "bits_per_symbol": mi, "max_bits": math.log2(levels) if levels > 1 else 0.0,
                   "bits_per_s_upper": mi / t_mean if t_mean > 0 else float("nan"),
                   "budget_per_symbol": float(np.mean([r["spent"] for r in rows]))}
    return out


# ---- вердикты

def f1_verdict(sl: dict, jm: list[str]) -> dict:
    reasons = []
    for k, s in sl.items():
        st = s["steady"]
        if st is None:
            return {"verdict": "нельзя сказать", "why": f"мало точек у «{k}»"}
        if st["r2"] < ПОРОГИ["линейность_R2"]:
            reasons.append(f"«{k}»: прямая объясняет {st['r2']:.3f} разброса, меньше {ПОРОГИ['линейность_R2']}")
        if s["anomalies"]:
            reasons.append(f"«{k}»: {s['anomalies']} замеров с исходом не «завершён»")
        if s["unstable_cost"]:
            reasons.append(f"«{k}»: топливо или бюджет менялись от запуска к запуску")
    reasons += [f"скачок: {j}" for j in jm]
    # неучтённое: время единицы против времени её топлива по цене «счёта»
    base = (sl.get("счёт") or {}).get("steady")
    unacc = []
    if base:
        for k in UNIT:
            s = sl.get(k)
            if not s or not s["steady"]:
                continue
            fuel_ns = (s["fuel_per_unit"] or 0) * base["b"]
            spent = s["spent_per_unit"] or 0
            ratio = s["steady"]["b"] / fuel_ns if fuel_ns > 0 else float("inf")
            if spent < 0.5 and ratio > ПОРОГИ["неучтённое_раз"]:
                unacc.append(f"«{k}»: {UNIT[k]} стоит {s['steady']['b'] / 1000:.1f} мкс, а по топливу — "
                             f"{fuel_ns / 1000:.3f} мкс (в {ratio:.0f} раз меньше), бюджета 0")
    reasons += [f"неучтённое: {u}" for u in unacc]
    return {"verdict": "supported" if not reasons else "refuted", "reasons": reasons, "unaccounted": unacc}


def analyse(path: Path) -> dict:
    d = read(path)
    sl = slopes(d)
    jm = jumps(sl)
    head = d["заголовок"][0] if d["заголовок"] else {}
    end = d["конец"][0] if d["конец"] else {}
    peak = defaultdict(int)
    for b in d["блок"]:
        peak[b["kind"]] = max(peak[b["kind"]], b.get("peak_kb") or -1)
    return {"device": head.get("device"), "plan": head.get("plan"), "end": end, "slopes": sl, "jumps": jm,
            "energy": energy(d, sl) if d["батарея"] else {"note": "нет показаний батареи"},
            "temperature": temperature(d), "h2": h2(d), "channels": channels(d), "peak_kb": dict(peak),
            "events": [e["text"] for e in d["событие"]], "f1": f1_verdict(sl, jm)}


# ---- отчёт

def unit_text(k: str) -> str:
    return "нс на единицу топлива" if k in FUEL_KINDS else f"мкс на {UNIT[k]}"


def b_text(k: str, b: float) -> str:
    return f"{b:.3f}" if k in FUEL_KINDS else f"{b / 1000:.1f}"


def report(ph: dict, sv: dict | None) -> str:
    L = ["# Шаг 8: итоги замеров", ""]
    dev = ph.get("device") or {}
    L += [f"Устройство: {', '.join(f'{k} {v}' for k, v in dev.items())}.",
          f"Конец прогона: {ph['end'].get('why')} (полный: {ph['end'].get('complete')}).", ""]
    L += ["## Цена во времени (устойчивый режим, круги 2–4)", "",
          "| Вид | наклон | единица | свободный член, мкс | R² | круг 1 / устойчивый | топлива на единицу | бюджета на единицу |"
          + (" телефон / сервер |" if sv else ""),
          "|---|---|---|---|---|---|---|---|" + ("---|" if sv else "")]
    for k, s in ph["slopes"].items():
        st, r1 = s["steady"], s["rounds"].get(1)
        if not st:
            L.append(f"| {k} | мало точек |")
            continue
        cold = f"{r1['b'] / st['b']:.2f}" if r1 else "—"
        row = (f"| {k} | {b_text(k, st['b'])} | {unit_text(k)} | {st['a'] / 1000:.0f} | {st['r2']:.4f} | {cold} | "
               f"{s['fuel_per_unit']:.1f} | {s['spent_per_unit']:.2f} |")
        if sv:
            ss = (sv["slopes"].get(k) or {}).get("steady")
            row += f" {st['b'] / ss['b']:.1f} |" if ss else " — |"
        L.append(row)
    L += ["", "**Скачки наклона между кругами:** " + ("; ".join(ph["jumps"]) if ph["jumps"] else "нет.")]
    if ph["jumps"] and all("круг 1 → 2" in j for j in ph["jumps"]):
        L.append("Все — между холодным кругом 1 и устойчивым режимом; между кругами 2–4 скачков нет.")
    L.append("")
    e = ph["energy"]
    L += ["## Энергия", "", f"Прибор: {e.get('note')}. Оценка, не энергометр.", ""]
    if e.get("kinds"):
        L += ["| Вид | ΔP в работе, мВт | разброс по кругам, мВт | энергия единицы | фон (покой), мВт |", "|---|---|---|---|---|"]
        for k, x in e["kinds"].items():
            per = x["nJ_per_unit"]
            per_t = f"{per:.2f} нДж на единицу топлива" if k in FUEL_KINDS else f"{per / 1e6:.3f} мДж на {UNIT[k]}"
            if not (abs(x["dP_w"]) > x["dP_sd"]):
                per_t = "нельзя сказать: разброс по кругам больше самой разности"
            L.append(f"| {k} | {x['dP_w'] * 1000:.0f} | {x['dP_sd'] * 1000:.0f} | {per_t} | {x['P_rest_w'] * 1000:.0f} |")
    if "charge_drop_uah" in e:
        L.append(f"\nСчётчик заряда за прогон: {e['charge_drop_uah'] / 1000:.0f} мА·ч.")
    t = ph["temperature"]
    if t:
        L += ["", f"Температура батареи: в начале {t['start']:.1f} °C, наибольшая {t['max']:.1f} °C, в конце {t['end']:.1f} °C."]
    L += ["", "## Память", "", "Пиковая память процесса исполнителя: "
          + ", ".join(f"{k} — {v // 1024} МиБ" if v >= 1024 else f"{k} — {v} КиБ" for k, v in ph["peak_kb"].items()) + "."]
    L += ["", "## Каналы (О10)", "", "| Канал | что видно | символов (проверка) | ошибок | бит на символ (из возможных) | бит/с, верхняя граница | бюджета на символ |",
          "|---|---|---|---|---|---|---|"]
    for ch, c in ph["channels"].items():
        L.append(f"| {ch} | {c['seen']} | {c['symbols']} ({c['test']}) | {c['errors']:.1%} | "
                 f"{c['bits_per_symbol']:.2f} ({c['max_bits']:.0f}) | {c['bits_per_s_upper']:.0f} | {c['budget_per_symbol']:.1f} |")
    L += ["", "Бит на символ — взаимная информация на проверочной половине. При нуле ошибок она ниже "
          "возможного только потому, что символы в этой половине встречаются неравно часто."]
    f1 = ph["f1"]
    L += ["", "## Вердикты", "", f"- **Ф1:** {f1['verdict']}."] + [f"  - {r}" for r in f1["reasons"]]
    h = ph["h2"]
    L += [f"- **Н2:** {h['verdict']} — {h['why']}" + (f" (r = {h['r']:.2f}, p = {h['p']:.3f}, блоков {h['blocks']})." if "r" in h else ".")]
    if ph["events"]:
        L += ["", "## События прогона", ""] + [f"- {x}" for x in ph["events"][:40]]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phone")
    ap.add_argument("--server")
    ap.add_argument("--out", default="experiments/results")
    a = ap.parse_args()
    ph = analyse(Path(a.phone))
    sv = analyse(Path(a.server)) if a.server else None
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "shag8_summary.md").write_text(report(ph, sv), encoding="utf-8")
    (out / "shag8.json").write_text(json.dumps({"phone": ph, "server": sv}, ensure_ascii=False, indent=1, default=str),
                                    encoding="utf-8")
    print((out / "shag8_summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
