"""Шаг 8в: попытка платит (спецификация шага 8в).

1. Окно бюджета тем же правилом, что в шагах 6 и 8б, — с правилом.
2. Основная серия шага 6 по ценам шага 8б и с правилом, цели Ц-1 … Ц-10.
3. Предел для владельца — по самому дорогому из прогонов телефона
   (спецификация шага 8в, §7): калибровка шага 8 и проверка предела шага 8б.
4. --bound-file ФАЙЛ — проверка предела по файлу стенда с планом «предел 8в»:
   вердикт против предела, объявленного до прогона, и предел, пересчитанный
   с этим прогоном.

python3 run_shag8v.py [--real] [--seeds 50] | --bound-file ФАЙЛ
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

import run_shag6 as S6
import run_shag8 as A8
import run_shag8b as R8
from phone8 import bound8b as B8
from sandbox.process5 import Driver, LocalDriver

RESULTS = Path("experiments/results")
PRICES8V = {**R8.PRICES, "paid_attempts": 1}
FUEL, Z = R8.FUEL, R8.Z
OUTCOME_STOP = "ловушка: бюджет исчерпан"
OUTCOME_FUEL = "ловушка: топливо кончилось"


# ---- цена единицы бюджета и топлива по прогону стенда

def session_costs(path: Path) -> dict:
    """Цена единицы бюджета и единицы топлива в одном прогоне стенда с
    планом «предел» (шаг 8б) или «предел 8в».

    - Стратегия без расхода бюджета, кончившаяся топливом, — цена топлива:
      время / 3 млн.
    - Кончившаяся остановом (правило шага 8в) — вся работа оплачена:
      время / расход.
    - Кончившаяся топливом при расходе > 0 (план шага 8б) — работа плюс
      хвост. Берётся только стратегия с хвостом-счётом: из её времени
      вычитается время стратегии «топливо» того же прогона. Хвост из
      бесплатных отказов — это дыра, а не цена работы, и в цену не идёт.

    Энергия — ΔP × среднее время там, где ΔP надёжна (§ run_shag8b.bound_check)."""
    r = R8.bound_check(path, 1e9, 1e9)
    st = r["strategies"]
    count_tail = {f"8б: {n}" for n, (m, tail, h) in B8.STRATEGIES.items() if tail == "счёт"} | \
                 {n for n, (m, tail, h) in B8.STRATEGIES.items() if tail == "счёт"}
    fuel = next((v for k, v in st.items() if v["spent"] == 0 and OUTCOME_FUEL in v["outcomes"]), None)
    t_fuel = fuel["mean_ms"] * 1e6 / FUEL if fuel else None                   # нс на единицу топлива
    e_fuel = fuel["energy_mJ"] * 1e6 / FUEL if fuel and fuel["energy_mJ"] is not None else None   # нДж
    t_unit, e_unit = {}, {}
    for k, v in st.items():
        if v["spent"] <= 0:
            continue
        if OUTCOME_STOP in v["outcomes"]:
            t, e = v["mean_ms"], v["energy_mJ"]
        elif k in count_tail and fuel:
            t = v["mean_ms"] - fuel["mean_ms"]
            e = None if v["energy_mJ"] is None or fuel["energy_mJ"] is None else v["energy_mJ"] - fuel["energy_mJ"]
        else:
            continue
        t_unit[k] = t / v["spent"] * 1e3                                       # мкс на единицу бюджета
        if e is not None:
            e_unit[k] = e / v["spent"] * 1e3                                   # мкДж на единицу бюджета
    return {"file": str(path), "t_unit_us": t_unit, "e_unit_uJ": e_unit, "t_fuel_ns": t_fuel, "e_fuel_nJ": e_fuel}


def calibration_costs(b_units: int) -> dict:
    """То же из калибровки шага 8 (наклоны по размерам нагрузок)."""
    b = R8.owner_bound(RESULTS / "shag8_phone.txt", b_units, R8.PRICES)
    return {"file": str(RESULTS / "shag8_phone.txt"), "t_unit_us": b["t_unit_us"], "e_unit_uJ": b["e_unit_uJ"],
            "t_fuel_ns": b["t_fuel_ns"], "e_fuel_nJ": b["e_fuel_nJ"]}


def owner_bound(b_units: int, sessions: list[dict]) -> dict:
    """Предел за жизнь контура = (B × p_b + F × p_f) × z. p_b и p_f — самые
    дорогие по всем видам работы и всем прогонам (спецификация шага 8в, §7)."""
    def worst(field: str) -> tuple[float, str]:
        best = (0.0, "")
        for s in sessions:
            vals = s[field].items() if isinstance(s[field], dict) else [("", s[field])]
            for k, v in vals:
                if v is not None and v > best[0]:
                    best = (v, f"{Path(s['file']).name}: {k}".rstrip(": "))
        return best
    pb_t, pb_t_from = worst("t_unit_us")
    pb_e, pb_e_from = worst("e_unit_uJ")
    pf_t, pf_t_from = worst("t_fuel_ns")
    pf_e, pf_e_from = worst("e_fuel_nJ")
    return {"B": b_units, "Z": Z, "p_b_us": pb_t, "p_b_us_from": pb_t_from, "p_b_uJ": pb_e, "p_b_uJ_from": pb_e_from,
            "p_f_ns": pf_t, "p_f_ns_from": pf_t_from, "p_f_nJ": pf_e, "p_f_nJ_from": pf_e_from,
            "time_ms": (b_units * pb_t / 1e3 + FUEL * pf_t / 1e6) * Z,
            "energy_mJ": (b_units * pb_e / 1e3 + FUEL * pf_e / 1e6) * Z,
            "sessions": sessions}


def sessions_before(b_units: int) -> list[dict]:
    return [calibration_costs(b_units), session_costs(RESULTS / "shag8b_bound_phone.txt")]


# ---- отчёт

def write(res: dict) -> Path:
    w, w8 = res["window"], res["window_8b"]
    b = res["bound"]
    L = ["# Шаг 8в: попытка платит — сводка", "",
         "## Окно бюджета (то же правило, правило шага 8в включено)", "",
         "| | U' | L' | честный | B' | контроль M2 |", "|---|---|---|---|---|---|",
         f"| шаг 8б | {w8['U']} | {w8['L']} | {w8['honest']} | {w8['B']} | {w8['B_M2']} |",
         f"| шаг 8в | {w['U']} | {w['L']} | {w['honest']} | {w['B']} | {w['B_M2']} |", ""]
    if "series" in res:
        L += [f"Серия шага 6 с правилом: `{res['series']}`.", ""]
    L += ["## Предел для владельца", "",
          f"(B × p_b + F × p_f) × z = ({b['B']} × {b['p_b_us']:.1f} мкс + {FUEL:,} × {b['p_f_ns']:.2f} нс) × {Z}"
          .replace(",", " ") + f" = **{b['time_ms']:.0f} мс**;", "",
          f"энергия: ({b['B']} × {b['p_b_uJ']:.1f} мкДж + {FUEL:,} × {b['p_f_nJ']:.2f} нДж) × {Z}".replace(",", " ")
          + f" = **{b['energy_mJ']:.0f} мДж**.", "",
          f"Самое дорогое: единица бюджета по времени — {b['p_b_us_from']}; по энергии — {b['p_b_uJ_from']}; "
          f"топливо по времени — {b['p_f_ns_from']}; по энергии — {b['p_f_nJ_from']}.", ""]
    out = RESULTS / "shag8v_summary.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    (RESULTS / "shag8v.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bound-file", help="файл стенда с планом «предел 8в»")
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--seeds", type=int, default=50)
    ap.add_argument("--gran-seeds", type=int, default=20)
    ap.add_argument("--defect-seeds", type=int, default=10)
    ap.add_argument("--no-series", action="store_true")
    a = ap.parse_args()
    if a.bound_file:
        before = json.loads((RESULTS / "shag8v.json").read_text(encoding="utf-8"))["bound"]
        r = R8.bound_check(Path(a.bound_file), before["time_ms"], before["energy_mJ"])
        where = "phone" if r["device"] and r["device"].get("where") == "телефон" else "server"
        plan = (A8.read(Path(a.bound_file))["заголовок"] or [{}])[0].get("plan", "")
        what = "energy" if plan.startswith("энергия") else "bound"
        print(R8.write_bound(r, f"shag8v_{what}_{where}", step="8в").read_text(encoding="utf-8"))
        if where != "phone":
            return                      # предел — телефонный: сервер в его цены не идёт
        after = owner_bound(before["B"], sessions_before(before["B"]) + [session_costs(Path(a.bound_file))])
        (RESULTS / f"shag8v_{what}_after_{where}.json").write_text(json.dumps(after, ensure_ascii=False, indent=1),
                                                                  encoding="utf-8")
        print(f"предел до прогона: {before['time_ms']:.0f} мс, {before['energy_mJ']:.0f} мДж; "
              f"с этим прогоном: {after['time_ms']:.0f} мс, {after['energy_mJ']:.0f} мДж")
        return
    drv = Driver if a.real else LocalDriver
    root = Path(tempfile.mkdtemp(prefix="shag8v_"))
    res = {"prices": PRICES8V, "window": R8.window(drv, root, PRICES8V),
           "window_8b": json.loads((RESULTS / "shag8b.json").read_text(encoding="utf-8"))["window_new"]}
    print("окно:", res["window"], flush=True)
    nb = res["window"]
    if nb["B"] is not None and not a.no_series:
        budgets = {"grid": nb["B"], "m2": nb["B_M2"], "gran": 400 * R8.SCALE_PLENTY, "defects": 60 * R8.SCALE_PLENTY}
        series = S6.main_series(drv, root, a.seeds, a.gran_seeds, a.defect_seeds, over=PRICES8V, budgets=budgets)
        res["series"] = str(S6.write_main(series, name="shag8v_series",
                                          title="Шаг 8в: основная серия шага 6 с правилом «попытка платит»"))
    res["bound"] = owner_bound(nb["B"] or 0, sessions_before(nb["B"] or 0))
    print(write(res).read_text(encoding="utf-8"))
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
