"""Шаг 9: доля байтов в цене вопроса модели — по правилу шага 8б.

Правило объявлено в спецификации шага 8б (§2) и не меняется: плата за байты
нужна, если при наибольшем размере доля байтов больше 10 % цены вызова.
Здесь «вызов» — вопрос модели: две записи журнала, в которых лежат тексты
вопроса и ответа, и передача их процессу модели. Вычисления модели в цену
устройства не входят (базовый сценарий), поэтому модель — заглушка-эхо: её
работа ничтожна, и разница времени — работа среды над байтами.

Сравниваются прямо, а не продолжением наклона (урок промаха шага 8):
- малый вопрос: «ЭХО а» — 8 байт вопроса, 2 байта ответа;
- наибольший: вопрос 4096 байт, ответ — предел A = 1024 байта.
Каждый запуск — новая песочница и N вопросов подряд; время шага run.

С `--exe` замер идёт через внешний исполнитель по протоколу контракта
(обвязка судьи, harness.ExeDriver): так меряется вторая реализация, а для
сравнения тем же путём — и первая (sandbox.jsonfront). Время шага run тогда
включает обмен строкой JSON с процессом исполнителя — одна и та же добавка
к малому и наибольшему вопросу, она чуть занижает долю.

Чего замер не умеет: у Python постоянная часть вызова больше, чем у Rust,
поэтому доля байтов на первой реализации, скорее, занижена.

PYTHONPATH=. python3 experiments/measure9.py [--exe "КОМАНДА ИСПОЛНИТЕЛЯ" --name ИМЯ]
"""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from experiments import programs5b as p5b
from sandbox.process5 import LocalDriver

N = 12
REPEAT = 25
CFG = {"graph": "M3т", "levels": 4, "memory": 4, "budget": 10_000_000, "price": 10, "fail_price": 10,
       "work_price": 10, "gate_price": 5, "paid_attempts": 1, "model": 1, "model_answer": 1024}
RESULTS = Path(__file__).resolve().parent / "results"


def texts(big: bool) -> list[str]:
    """N разных вопросов: ответ эха короче вопроса и не залезает на соседний."""
    if not big:
        return [f"ЭХО а{k:02d}" for k in range(N)]
    out = []
    for k in range(N):
        head = f"ЭХО {k:02d}"
        out.append(head + "ж" * ((4096 - len(head.encode())) // 2))
    return out


def one(d, big: bool) -> float:
    st, wd = d.call("new", CFG, None)
    assert st == "ok", wd
    d.call("load", 1, p5b.module([("модель", t) for t in texts(big)]))
    t0 = time.perf_counter()
    st, v = d.call("run", 1)
    dt = time.perf_counter() - t0
    assert st == "ok" and v[0] == "завершён", v
    return dt / N


def driver(exe: str | None):
    root = Path(tempfile.mkdtemp(prefix="measure9_"))
    if not exe:
        return LocalDriver(root, frozenset(), kind="5б", model={"mode": ["--stub"], "timeout": 60.0, "allow_net": True})
    from experiments.diff7 import H              # обвязка судьи: ExeDriver
    return H.ExeDriver(exe, root, "5б", model="--stub", model_timeout=60.0)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="внешний исполнитель; без него — первая реализация в этом процессе")
    ap.add_argument("--name", default="shag9_bytes")
    a = ap.parse_args()
    d = driver(a.exe)
    try:
        for big in (False, True):                   # прогрев
            one(d, big)
        small, large = [], []
        for _ in range(REPEAT):
            small.append(one(d, False))
            large.append(one(d, True))
    finally:
        d.close()
    q_big = len(texts(True)[0].encode())
    base, top = statistics.mean(small), statistics.mean(large)
    diff = [b - s for s, b in zip(small, large)]
    se = statistics.stdev(diff) / len(diff) ** 0.5
    res = {
        "вопрос малый / наибольший, байт": [len(texts(False)[0].encode()), q_big],
        "ответ малый / наибольший, байт": [len(texts(False)[0].encode()) - len("ЭХО ".encode()), CFG["model_answer"]],
        "мкс на вопрос: малый": base * 1e6, "мкс на вопрос: наибольший": top * 1e6,
        "разница, мкс": (top - base) * 1e6, "её стандартная ошибка, мкс": se * 1e6,
        "доля байтов при наибольшем размере": (top - base) / base,
        "плата за байты нужна (доля > 10 %)": (top - base) / base > 0.10,
        "повторов": REPEAT, "вопросов в запуске": N, "исполнитель": a.exe or "первая реализация, в процессе",
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{a.name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
