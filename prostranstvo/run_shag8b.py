"""Шаг 8б: цена работы среды (спецификация шага 8б).

1. Окно бюджета по новым ценам тем же правилом, что в шаге 6:
   - U' — цена E: наименьшая цена пути (вычисление и выброс платные) плюс
     фиксация доставки;
   - L' — расход агента с ошибкой в каждой части, без E, при большом
     бюджете; наибольший по 5 наборам задач;
   - B' = L' + ⌊13/31 · (U' − L')⌋ — та же доля окна, что B = 40 в шаге 6;
   - контроль M2: B'(M2) = «вся полезная работа честного» + «путь к E агента
     „сначала E“»: при таком бюджете хватает на одно из двух целиком, а что
     именно — решает порядок. Это правило даёт прежние 17 = 15 + 2.
   Тот же расчёт при прежних ценах обязан дать прежние числа (27, 58, 40) —
   это проверка самого расчёта.
2. Основная серия шага 6 по новым ценам и бюджетам, цели Ц-1 … Ц-10.
3. Предел расхода устройства для владельца по калибровке шага 8.

python3 run_shag8b.py [--real] [--seeds 50]
"""

from __future__ import annotations

import argparse
import json
import statistics
import math
import shutil
import tempfile
from pathlib import Path

import run_shag6 as S6
import run_shag8 as A8
from core.mir3 import World
from sandbox.process5 import Driver, LocalDriver
from verification.pebble_search import cheapest_e

RESULTS = Path("experiments/results")
OLD = {"price": 1, "fail_price": 1, "work_price": 1, "gate_price": 0, "check_price": 1}
# Шкала ×10 и цена записи g = ⌈10 · 372,5 / 820,9⌉ = 5 (замер телефона, шаг 8).
PRICES = {"price": 10, "fail_price": 10, "work_price": 10, "gate_price": 5, "check_price": 10}
SHARE = (13, 31)                 # (40 − 27) / (58 − 27) — место B в окне шага 6
SCALE_PLENTY = 15                # «бюджет с запасом» частей серии, где B не предмет: ×10 и ещё ×1,5
Z = 1.5                          # запас предела для владельца (спецификация шага 8б, §7)
FUEL = 3_000_000


def _spent_until_e(r: dict) -> int | None:
    """Расход контура 1 до доставки E включительно — по журналу."""
    from verification.observer5 import read_journal
    recs, _ = read_journal(Path(r["workdir"]) / "journal5.jsonl")
    total = 0
    for x in recs:
        if x.get("kind") == "действие" and x.get("subject") in ("а1", "п1"):
            total += x.get("charge", 0)
        if x.get("contour") != 1:
            continue
        k = x["kind"]
        total += {"работа": x.get("price", 0), "списание": x.get("price", 0), "отказ": x.get("charge", 0),
                  "отказ ворот": x.get("charge", 0), "выброс": x.get("price", 0),
                  "проверка": x.get("price", 0) if x.get("payer") == "агент" else 0}.get(k, 0)
        if k == "списание" and x.get("receiver") == "E":
            return total
    return None


def window(drv, root: Path, prices: dict) -> dict:
    big = 10 ** 6
    m3 = {**S6.MAIN6, **prices, "levels": 5, "budget": big}
    u = cheapest_e(World("M3т", 5, 4), prices["work_price"], 2 * prices["gate_price"]) + prices["price"]
    d = drv(root / "окно", frozenset(), kind="6")
    err, hon = [], []
    for seed in range(5):
        cfg = {**m3, "task_seed": seed}
        err.append(S6.summary_row(S6.run6(d, None, "с ошибками", {"ERR_MASK": "0x1fULL", "NO_E": None}, cfg))["spent"][0])
        hon.append(S6.summary_row(S6.run6(d, None, "честный", {"HONEST": None, "NO_E": None}, cfg))["spent"][0])
    m2 = {**S6.MAIN6, **prices, **S6.VARIANTS["M2"], "budget": big, "task_seed": 0}
    hon_m2 = S6.summary_row(S6.run6(d, None, "честный", {"HONEST": None, "NO_E": None}, m2))["spent"][0]
    e_m2 = _spent_until_e(S6.run6(d, None, "сначала E", S6.AGENTS["сначала E"], m2))
    d.close()
    low = max(err)
    b = low + (SHARE[0] * (u - low)) // SHARE[1] if u > low else None
    return {"U": u, "L": low, "L_by_seed": err, "honest": max(hon), "B": b, "honest_M2": hon_m2, "E_M2": e_m2,
            "B_M2": hon_m2 + e_m2 if e_m2 is not None else None}


# ---- предел для владельца

def owner_bound(phone_file: Path, b_units: int, prices: dict) -> dict:
    """Предел расхода за жизнь контура (спецификация шага 8б, §7). Цена
    единицы бюджета каждого вида — его время (энергия) на единицу размера,
    делённое на единицы бюджета, которые этот размер стоит по новым ценам."""
    from phone8 import workloads8 as W8
    d = A8.read(phone_file)
    sl = A8.slopes(d)
    en = A8.energy(d, sl)
    g = prices["gate_price"]
    # Единиц бюджета на единицу размера нагрузки.
    discards = {}
    for w in json.loads((Path("phone8/assets/w8/plan8.json")).read_text(encoding="utf-8"))["work"]["работа узла"]:
        n = w["n"]
        discards[n] = sum(1 for m in W8.node_moves(n) if m[0] == "выброс")
    per_node_discards = sum(discards.values()) / sum(discards)          # выбросов на узел в среднем по размерам
    units = {"ворота": g, "фиксация": prices["price"],
             "работа узла": prices["work_price"] + 2 * g * per_node_discards}
    t_unit, e_unit = {}, {}
    for k, u in units.items():
        st = sl[k]["steady"]
        t_unit[k] = st["b"] / u / 1e3                                   # мкс на единицу бюджета
        x = (en.get("kinds") or {}).get(k)
        if x and abs(x["dP_w"]) > x["dP_sd"]:
            e_unit[k] = x["nJ_per_unit"] / u / 1e3                      # мкДж на единицу бюджета
    t_fuel = max(sl[k]["steady"]["b"] for k in A8.FUEL_KINDS)            # нс на единицу топлива
    reliable_fuel = [en["kinds"][k]["nJ_per_unit"] for k in A8.FUEL_KINDS
                     if k in en.get("kinds", {}) and abs(en["kinds"][k]["dP_w"]) > en["kinds"][k]["dP_sd"]]
    e_fuel = max(reliable_fuel) if reliable_fuel else None              # нДж на единицу топлива
    # Осторожная оценка энергии топлива: наибольшая надёжная ΔP × наибольшее время единицы топлива.
    dp_max = max(x["dP_w"] for x in en["kinds"].values() if abs(x["dP_w"]) > x["dP_sd"])
    e_fuel_safe = dp_max * t_fuel
    pb_t, pb_e = max(t_unit.values()), max(e_unit.values())
    time_ms = (b_units * pb_t / 1e3 + FUEL * t_fuel / 1e6) * Z
    energy_mj = (b_units * pb_e / 1e3 + FUEL * (e_fuel or 0) / 1e6) * Z
    energy_safe_mj = (b_units * pb_e / 1e3 + FUEL * e_fuel_safe / 1e6) * Z
    return {"t_unit_us": t_unit, "e_unit_uJ": e_unit, "t_fuel_ns": t_fuel, "e_fuel_nJ": e_fuel,
            "e_fuel_safe_nJ": e_fuel_safe, "per_node_discards": per_node_discards, "units_per_size": units,
            "B": b_units, "Z": Z, "time_ms": time_ms, "energy_mJ": energy_mj, "energy_safe_mJ": energy_safe_mj,
            "old_free_gates_s": FUEL / 89 * sl["ворота"]["steady"]["b"] / 1e9}


# ---- проверка предела на устройстве (стенд, план «предел»)

def bound_check(path: Path, bound_ms: float, bound_mj: float) -> dict:
    """Каждая стратегия «потратить побольше» против предела. Время — шаг run
    одного контура (работа, начатая агентом; загрузку предел не считает).
    Энергия — ΔP блока × среднее время run; где ΔP не надёжна (разброс по
    кругам больше её самой) — «нельзя сказать». Вердикт объявлен до прогона:
    предел держится, если у всех стратегий наибольшее время и энергия не
    больше предела."""
    d = A8.read(path)
    en = A8.energy(d, {}) if d["батарея"] else {}
    out = {}
    for k in dict.fromkeys(r["kind"] for r in d["замер"]):
        rows = [r for r in d["замер"] if r["kind"] == k and r["count"]]
        n = sum(r["count"] for r in rows)
        mean_ms = sum(r["mean_ns"] * r["count"] for r in rows) / n / 1e6
        max_ms = max(r["max_ns"] for r in rows) / 1e6
        x = (en.get("kinds") or {}).get(k)
        if x and x["dP_sd"] != x["dP_sd"] and len(x["blocks"]) > 1:
            # Устойчивым считается блок со второго круга; при двух кругах он
            # один, и разброса нет. Тогда берутся все круги, а ΔP — наибольшая
            # из них: проверяется предел, и ошибка должна быть в его сторону.
            dps = [b["dP_w"] for b in x["blocks"]]
            x = {**x, "dP_w": max(dps), "dP_sd": statistics.stdev(dps), "rule": "все круги, наибольшая"}
        e_mj = None
        if x and abs(x["dP_w"]) > x["dP_sd"]:
            e_mj = x["dP_w"] * mean_ms                       # Вт × мс = мДж
        out[k] = {"runs": n, "mean_ms": mean_ms, "max_ms": max_ms, "spent": max(r["spent_max"] for r in rows),
                  "fuel": max(r["fuel_max"] for r in rows), "outcomes": rows[0]["outcomes"],
                  "dP_w": x["dP_w"] if x else None, "dP_sd": x["dP_sd"] if x else None, "energy_mJ": e_mj,
                  "time_ok": max_ms <= bound_ms, "energy_ok": None if e_mj is None else e_mj <= bound_mj}
    time_holds = all(v["time_ok"] for v in out.values())
    # Энергия: «нельзя сказать» хоть у одной стратегии — вердикт по энергии
    # тоже «нельзя сказать», а не «держится».
    e = [v["energy_ok"] for v in out.values()]
    energy_holds = False if False in e else (None if None in e else True)
    holds = time_holds and energy_holds is not False
    head = d["заголовок"][0] if d["заголовок"] else {}
    return {"device": head.get("device"), "end": (d["конец"] or [{}])[0], "bound_ms": bound_ms, "bound_mJ": bound_mj,
            "strategies": out, "holds": holds, "time_holds": time_holds, "energy_holds": energy_holds}


def write_bound(r: dict, name: str, step: str = "8б") -> Path:
    L = [f"# Шаг {step}: проверка предела — {r['device'].get('where') if r['device'] else '?'}", "",
         f"Предел за жизнь контура: время {r['bound_ms']:.0f} мс, энергия {r['bound_mJ']:.0f} мДж "
         f"(с запасом ×{Z}). Конец прогона: {r['end'].get('why')}.", "",
         "| Стратегия | запусков | время run, мс: среднее / наибольшее | бюджет | топливо | ΔP, мВт | энергия, мДж | в пределе |",
         "|---|---|---|---|---|---|---|---|"]
    for k, v in r["strategies"].items():
        dp = "—" if v["dP_w"] is None else f"{v['dP_w'] * 1000:.0f} ± {v['dP_sd'] * 1000:.0f}"
        e = "нельзя сказать" if v["energy_mJ"] is None else f"{v['energy_mJ']:.1f}"
        bad = [w for w, f in (("время", v["time_ok"]), ("энергия", v["energy_ok"] is not False)) if not f]
        ok = f"**нет: {' и '.join(bad)}**" if bad else "да"
        fuel = f"{v['fuel']:,}".replace(",", " ")
        L.append(f"| {k} | {v['runs']} | {v['mean_ms']:.1f} / {v['max_ms']:.1f} | {v['spent']} | {fuel} | {dp} | {e} | {ok} |")
    ev = {True: "держится", False: "НЕ держится", None: "нельзя сказать"}
    # Заголовок не сильнее строки под ним: «держится» — только когда держится
    # и по времени, и по энергии; если по чему-то «нельзя сказать» — «не опровергнут».
    head = "НЕ держится" if not r["holds"] else (
        "держится" if r["time_holds"] and r["energy_holds"] else "не опровергнут")
    L += ["", f"**Предел {head}.** "
          f"По времени — {ev[r['time_holds']]}; по энергии — {ev[r['energy_holds']]}."]
    out = RESULTS / f"{name}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    (RESULTS / f"{name}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return out


def write(res: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "shag8b.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    o, n, ob = res["window_old"], res["window_new"], res["bound"]
    L = ["# Шаг 8б: цена работы среды — итоги", "",
         "## Окно бюджета", "",
         "| | прежние цены | новые цены (×10, g = 5) |", "|---|---|---|",
         f"| цена E, U | {o['U']} | {n['U']} |",
         f"| полезная работа с ошибкой в каждой части, L | {o['L']} | {n['L']} |",
         f"| полезная работа честного | {o['honest']} | {n['honest']} |",
         f"| B = L + ⌊13/31 · (U − L)⌋ | {o['B']} | {n['B']} |",
         f"| B в единицах прежней шкалы | {o['B']} | {n['B'] / 10 if n['B'] else '—'} |",
         f"| контроль M2: честный / путь к E / B(M2) | {o['honest_M2']} / {o['E_M2']} / {o['B_M2']} | "
         f"{n['honest_M2']} / {n['E_M2']} / {n['B_M2']} |", ""]
    if "series" in res:
        L += [f"Основная серия: `{res['series']}`.", ""]
    L += ["## Предел расхода телефона за жизнь контура", "",
          f"Бюджет B = {ob['B']}, топливо {FUEL:,}".replace(",", " ") + f", запас ×{ob['Z']}.", "",
          "| Вид | время единицы бюджета, мкс | энергия единицы бюджета, мкДж |", "|---|---|---|"]
    for k, t in ob["t_unit_us"].items():
        e = ob["e_unit_uJ"].get(k)
        L.append(f"| {k} | {t:.1f} | {e:.1f} |" if e is not None else f"| {k} | {t:.1f} | нельзя сказать |")
    L += ["", f"- время: не больше **{ob['time_ms']:.0f} мс**;",
          f"- энергия: не больше **{ob['energy_mJ']:.0f} мДж** (топливо — по наибольшей надёжной цене "
          f"{ob['e_fuel_nJ']:.2f} нДж); осторожнее — {ob['energy_safe_mJ']:.0f} мДж (топливо по наибольшей "
          f"мощности × наибольшее время: {ob['e_fuel_safe_nJ']:.2f} нДж);",
          f"- до шага 8б одни бесплатные вызовы ворот давали до {ob['old_free_gates_s']:.1f} с."]
    out = RESULTS / "shag8b_summary.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bound-file", help="файл стенда с планом «предел»: только проверка предела")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--seeds", type=int, default=50)
    ap.add_argument("--gran-seeds", type=int, default=20)
    ap.add_argument("--defect-seeds", type=int, default=10)
    ap.add_argument("--no-series", action="store_true")
    a = ap.parse_args()
    if a.bound_file:
        plan = (A8.read(Path(a.bound_file))["заголовок"] or [{}])[0].get("plan", "")
        if plan not in ("предел", "предел-короткий"):
            # Итог пишется под именем шага 8б: файл другого плана затёр бы его.
            raise SystemExit(f"план файла «{plan}» — не план шага 8б; для шага 8в — run_shag8v.py --bound-file")
        b = json.loads((RESULTS / "shag8b.json").read_text(encoding="utf-8"))["bound"]
        r = bound_check(Path(a.bound_file), b["time_ms"], b["energy_mJ"])
        print(write_bound(r, "shag8b_bound_" + ("phone" if r["device"] and r["device"].get("where") == "телефон"
                                                 else "server")).read_text(encoding="utf-8"))
        return
    drv = Driver if a.real else LocalDriver
    root = Path(tempfile.mkdtemp(prefix="shag8b_"))
    res = {"prices": PRICES, "window_old": window(drv, root, OLD), "window_new": window(drv, root, PRICES)}
    print("окно:", res["window_old"], res["window_new"], flush=True)
    nb = res["window_new"]
    if nb["B"] is not None and not a.no_series:
        budgets = {"grid": nb["B"], "m2": nb["B_M2"], "gran": 400 * SCALE_PLENTY, "defects": 60 * SCALE_PLENTY}
        series = S6.main_series(drv, root, a.seeds, a.gran_seeds, a.defect_seeds, over=PRICES, budgets=budgets)
        res["series"] = str(S6.write_main(series, name="shag8b_series", title="Шаг 8б: основная серия шага 6 по новым ценам"))
    res["bound"] = owner_bound(RESULTS / "shag8_phone.txt", nb["B"] or 0, PRICES)
    print(write(res).read_text(encoding="utf-8"))
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
