"""Разбор итогов калибровки шага 9б (спецификация шага 9б, §17) — на
сервере, по папке итогов стенда (kalibr9.json, battery.csv, k5_journal5.jsonl,
k6_*):

  - энергия: единицы тока по величине фона, мощность P = U·I, мощность
    покоя P₀, e(b) = ∫(P − P₀) dt по вопросу, предел₂ по энергии; проверка
    единиц до выводов — ∫U·I dt против убыли заряда в процентах × 27,1 Вт·ч;
  - К5 по энергии: энергия каждого контура против предела₂;
  - К6: законы — наблюдатели шага 9 (observer6 с миром W6) по снимкам
    журнала, которые стенд кладёт после каждого шага.

  PYTHONPATH=. python3 phone9/razborK9.py ПАПКА_ИТОГОВ [--out ФАЙЛ.json]

Чего не умеет: прибора аудита на телефоне нет — закон о вызовах системы в
К6 не проверяется (аудит подаётся пустым и так и помечается); энергия — по
датчикам всего телефона.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from core.mir9 import W6
from sandbox.jsonfront import from_json
from verification import observer6
from verification.observer5 import BROKEN

Z = 1.5
WH_NOMINAL = 7.0 * 3.87          # 7000 мА·ч × 3,87 В — ёмкость по описанию телефона, Вт·ч
UNIT_TOLERANCE = 0.25
# Стенд пишет батарею раз в 0,5 с, пока работает. Пауза в журнале длиннее
# минуты — стенд не работал (остывание, зарядка между запусками): заряд за
# паузу мог и вырасти, поэтому отрезок проверки единиц на ней кончается.
GAP_MS = 60_000
LIMIT1_J = 0.127 * 10            # предел₁ по энергии: 127 мДж старого телефона × 10 (§17)
HERE = Path(__file__).resolve().parent


def battery(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({k: (float(v) if v not in ("", None) else None) for k, v in r.items()})
    return rows


def power(rows: list[dict], banki: int = 1) -> tuple[list[tuple[float, float]], dict]:
    """[(мс, Вт)] без зарядки. Ток — в мкА или мА (как отдаёт система):
    единица — по величине медианы; знак разряда — преобладающий.

    banki — во сколько раз настоящая мощность больше U·I по датчикам. У
    многобаночной батареи система может отдавать напряжение одной банки
    (банки последовательно) или ток одной ветви (параллельно); в обоих
    случаях U·I — доля мощности. Число задаётся по описанию батареи и
    сверяется проверкой единиц по ватт-часам."""
    cur = sorted(abs(r["i_raw"]) for r in rows if r["i_raw"] is not None and not r["plugged"])
    if not cur:
        return [], {"ток": "нет показаний без зарядки"}
    med = cur[len(cur) // 2]
    scale = 1e-6 if med > 20000 else 1e-3
    neg = sum(1 for r in rows if r["i_raw"] is not None and r["i_raw"] < 0 and not r["plugged"])
    sign = -1 if neg > len(cur) / 2 else 1
    pts = [(r["ms"], banki * r["v_mv"] / 1000 * sign * r["i_raw"] * scale) for r in rows
           if r["i_raw"] is not None and r["v_mv"] is not None and not r["plugged"]]
    return pts, {"единица тока": "мкА" if scale == 1e-6 else "мА", "знак разряда": sign, "медиана |I|": med,
                 "множитель мощности (банок)": banki}


def integral(pts: list[tuple[float, float]], a: float, b: float, base: float = 0.0) -> float | None:
    """∫(P − base) dt на [a, b], Дж, трапециями по точкам внутри отрезка."""
    xs = [(t, p) for t, p in pts if a <= t <= b]
    if len(xs) < 2:
        return None
    return sum((t2 - t1) / 1000 * ((p1 + p2) / 2 - base) for (t1, p1), (t2, p2) in zip(xs, xs[1:]))


def mean_power(pts, a, b) -> float | None:
    e = integral(pts, a, b)
    return None if e is None or b <= a else e / ((b - a) / 1000)


def unit_check(rows: list[dict], pts) -> dict:
    """∫U·I dt против убыли заряда × ёмкость — по каждому отрезку без зарядки
    и без пауз журнала (GAP_MS)."""
    segs, cur = [], []
    for r in rows:
        if r["plugged"] or r["level"] is None:
            if cur:
                segs.append(cur)
            cur = []
            continue
        if cur and r["ms"] - cur[-1]["ms"] > GAP_MS:
            segs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        segs.append(cur)
    out, e_sum, wh_sum = [], 0.0, 0.0
    for s in segs:
        a, b = s[0]["ms"], s[-1]["ms"]
        e = integral(pts, a, b) or 0.0
        drop = s[0]["level"] - s[-1]["level"]
        out.append({"минут": round((b - a) / 60000, 1), "∫U·I, Вт·ч": round(e / 3600, 3), "убыль, %": drop})
        e_sum += e / 3600
        wh_sum += drop / 100 * WH_NOMINAL
    total_drop = sum(x["убыль, %"] for x in out)
    if total_drop < 10:
        verdict = "мало данных: убыль заряда меньше 10 %"
    elif abs(e_sum - wh_sum) / wh_sum <= UNIT_TOLERANCE:
        verdict = "да"
    else:
        verdict = f"нет: ∫U·I = {e_sum:.2f} Вт·ч против {wh_sum:.2f} по убыли заряда"
    return {"отрезки": out, "∫U·I, Вт·ч": round(e_sum, 3), "по убыли заряда, Вт·ч": round(wh_sum, 3),
            "отношение": round(e_sum / wh_sum, 3) if wh_sum else None, "единицы верны": verdict}


def energy(res: dict, rows: list[dict], banki: int = 1) -> dict:
    pts, units = power(rows, banki)
    if not pts:
        return {"итог": "не меряется", **units}
    st = res["этапы"]
    idle = [st["К2"].get("покой до"), st["К2"].get("покой после"), st["К3"].get("покой после")]
    p0s = [mean_power(pts, w["с"], w["по"]) for w in idle if w]
    p0s = [x for x in p0s if x is not None]
    p0 = sum(p0s) / len(p0s) if p0s else None
    per: dict[str, float] = {}
    for name in ("К2", "К3"):
        for q in st[name]["вопросы"]:
            e = integral(pts, q["с"], q["по"], p0 or 0.0)
            if e is not None:
                per[q["вопрос"]] = max(per.get(q["вопрос"], float("-inf")), e)
    budget = res["настройки"]["бюджет"]
    rows_b, worst = [], 0.0
    for b in (256, 1024, 2048, 4096):
        p = 10 + -(-(b + 256) // 906)
        n = budget // p
        e = per.get(f"Р{b}")
        rows_b.append({"b": b, "вопросов": n, "e(b), Дж": None if e is None else round(e, 1),
                       "вопросов × e, Дж": None if e is None else round(n * e, 1)})
        if e is not None:
            worst = max(worst, n * e)
    limit2 = LIMIT1_J + Z * worst
    out = {**units, "P₀, Вт": None if p0 is None else round(p0, 3), "по размерам": rows_b,
           "предел₂ по энергии, Дж": round(limit2, 1), "обычный вопрос, Дж": None if "О" not in per else round(per["О"], 1),
           "проверка единиц": unit_check(rows, pts)}
    k5 = st.get("К5")
    if k5 and k5.get("закончен"):
        out["К5 по энергии"] = [{"контур": r["контур"], "Дж": None if (e := integral(pts, r["с"], r["по"], p0 or 0.0)) is None
                                 else round(e, 1)} for r in k5["запуски"]]
        es = [x["Дж"] for x in out["К5 по энергии"] if x["Дж"] is not None]
        out["К5 по энергии, вердикт"] = ("нельзя сказать: " + out["проверка единиц"]["единицы верны"]
                                        if out["проверка единиц"]["единицы верны"] != "да"
                                        else "да" if es and max(es) <= limit2 else "нет")
    return out


def laws_k6(folder: Path) -> dict:
    agent = (HERE / "assets" / "k9" / "agent9.wasm").read_bytes()
    out = {}
    for d in sorted(folder.glob("k6_*")):
        if not (d / "run.json").exists():
            # Сценарий прерван посреди К6: снимки есть, итога запуска нет —
            # законы по нему не проверяются (этап начнётся заново).
            out[d.name] = {"законов проверено": 0, "нарушено": {}, "итог": "сценарий не закончен — не проверялся"}
            continue
        # Байты в выгрузке исполнителя — {"b64": …}, как в обмене судьи (jsonfront).
        run_info = from_json(json.loads((d / "run.json").read_text(encoding="utf-8")))
        dump = from_json(json.loads((d / "dump.json").read_text(encoding="utf-8")))
        snaps = []
        for s in sorted(d.glob("snap*")):
            j = s / "journal5.jsonl"
            snaps.append({"journal": j.read_bytes() if j.exists() else b"",
                          "receivers": {f"{k}{i}": (s / f"{k}{i}.jsonl").read_bytes() if (s / f"{k}{i}.jsonl").exists() else b""
                                        for i in (1, 2) for k in ("выход", "E")}})
        last = sorted(d.glob("snap*"))[-1]
        run = {"workdir": str(last), "defect": None, "program": d.name, "sources": {1: agent, 2: agent},
               "cfg": run_info["cfg"], "loads": {int(k): tuple(v) for k, v in run_info["loads"].items()},
               "runs": [(r["контур"], tuple(r["итог run"]) if isinstance(r["итог run"], list) else ("сбой", str(r["итог run"])))
                        for r in run_info["runs"]],
               "failures": [], "crashes": [], "audit": [], "snapshots": snaps, "dump": dump,
               "budget_enough": True, "ext_world": W6, "expect_all": False}
        laws = observer6.check(run)
        broken = {k: v[1][:300] for k, (s, v) in ((k, (x[0], x)) for k, x in laws.items()) if s == BROKEN}
        out[d.name] = {"законов проверено": len(laws), "нарушено": broken,
                       "аудит вызовов системы": "не проверен: на телефоне нет прибора"}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--out")
    ap.add_argument("--banki", type=int, default=1,
                    help="множитель мощности по числу банок батареи (iQOO Neo 10 — 2, см. power)")
    a = ap.parse_args()
    res = json.loads((a.folder / "kalibr9.json").read_text(encoding="utf-8"))
    out = {"вердикты стенда": res.get("вердикты"), "числа К4": {k: v for k, v in res["этапы"].get("К4", {}).items()
                                                               if k not in ("начат", "закончен")},
           "прерывания": [{"этап": x.get("этап"), "почему": x.get("почему"), "когда": x.get("когда"),
                           "журнал до прерывания": x.get("собрано", {}).get("журнал до прерывания")}
                          for x in res.get("прерывания", [])],
           "отказы в начале": res.get("отказы в начале", [])}
    bat = a.folder / "battery.csv"
    out["энергия"] = energy(res, battery(bat), a.banki) if bat.exists() and "К3" in res.get("этапы", {}) else "нет данных"
    out["К6 законы"] = laws_k6(a.folder)
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
