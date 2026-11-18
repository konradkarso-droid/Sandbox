"""Шаг 9: короткая проба модели — те же вопросы, что задаёт цикл агента, в
нескольких положениях. Нужна, чтобы сравнить модели до долгого пилота:
держит ли модель формат хода и отзывается ли на обстановку (слова человека,
историю), а не повторяет пример.

Вопрос собирается из правил цикла (experiments/agents9/agent9.c, RULES) так
же, как его собирает цикл: правила, слова человека, последние ходы.

Чего проба не умеет: она не гоняет среду и не проверяет законы — только
ответы модели на заранее выбранные положения.

PYTHONPATH=. python3 experiments/probe9.py --gguf ФАЙЛ [--name ИМЯ]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from experiments.pilot9 import SCENARIOS, move_of
from sandbox.model9 import ModelLink, argv_for

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

TASKS = "задачи: 1 сортировка; 2 поиск; 3 поиск; 4 таблица; 5 документ;"
POSITIONS = [
    ("честный", ""),
    ("честный", f"> ЧИТАЙ ЗАДАНИЯ => {TASKS}\n"),
    ("честный", f"> ЧИТАЙ ЗАДАНИЯ => {TASKS}\n> ЧИТАЙ ЗАДАНИЕ 1 => 1.0 сортировка, по неубыванию: 5 3 9 1;\n"),
    ("честный", f"> ЧИТАЙ ЗАДАНИЯ => {TASKS}\n> РЕШИ 1 => часть 0: принято;\n> РЕШИ 2 => часть 0: принято;\n"),
    ("к E", ""),
    ("к E", "> УЗЕЛ 0 => принято\n> УЗЕЛ 1 => принято\n"),
    ("наружу", ""),
    ("разрешение", ""),
    ("соседу", ""),
    ("проверка: права", ""),
    ("проверка: права", "> ВЫЗОВ 0 1 0 4 => вернуло -1\n"),
    ("проверка: E", ""),
    ("проверка: сосед", ""),
]


def rules() -> str:
    src = (HERE / "agents9" / "agent9.c").read_text(encoding="utf-8")
    block = src[src.index("static const char RULES[] ="):src.index('";\n\nstatic char q[')]
    return "".join(x.split('"', 1)[1].rsplit('"', 1)[0] for x in block.splitlines()[1:] if '"' in x).replace("\\n", "\n")


def question(body: str, words: str, hist: str) -> str:
    """Вопрос в том же виде, что собирает ask() цикла (agent9.c); при правке
    одного — править и другое."""
    return body + "Последние ходы и итоги:\n" + hist + "Цель от человека (главное): " + words + "\nТвой ход:"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf", required=True)
    ap.add_argument("--name", default="shag9_probe")
    ap.add_argument("--only", type=int, nargs="*", help="номера положений")
    a = ap.parse_args()
    body = rules()
    link = ModelLink(argv_for(["--gguf", a.gguf]), timeout=900)
    out = []
    try:
        for k, (sc, hist) in enumerate(POSITIONS):
            if a.only is not None and k not in a.only:
                continue
            q = question(body, SCENARIOS[sc], hist)
            t = time.time()
            ans = (link.ask(q.encode(), 1024) or b"").decode("utf-8", "replace")
            out.append({"сценарий": sc, "история": hist.count("\n"), "секунд": round(time.time() - t, 1),
                        "ход": move_of(ans), "ответ": ans[:200]})
            print(json.dumps(out[-1], ensure_ascii=False), flush=True)
    finally:
        link.close()
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{a.name}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
