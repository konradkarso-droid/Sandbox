"""Шаг 9б: во что вопрос обходится модели.

Предел устройства с моделью строится на двух вещах, и обе здесь меряются,
а не предполагаются:

1. сколько токенов (кусков текста, которыми считает модель) может дать
   вопрос в b байт. Исполнитель считает байты, модель работает токенами;
   предел по байтам ограничивает работу модели, только если токенов не
   больше, чем байт;
2. помещается ли наибольший вопрос (4096 байт) с наибольшим ответом в окно
   процесса модели (model9/serve.py). Вопрос, который не помещается, роняет
   процесс: контур до конца жизни получает −7.

Третье — время чтения вопроса и письма ответа порознь и вместе: от них
зависит, какой вопрос самый дорогой, и по нему ставится срок ответа.

Числа скоростей верны только для этого сервера и этой модели; для телефона
их даёт стенд. Отношение токенов к байтам — свойство словаря модели, оно от
устройства не зависит.

python3 experiments/measure9b.py --gguf ФАЙЛ [--out ФАЙЛ.json]
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SERVE = HERE / "model9" / "serve.py"
Q_MAX = 4096


def texts() -> dict[str, bytes]:
    """Вопросы по 4096 байт разного рода — от обычной речи до нарочно
    «плотных», где на токен приходится меньше всего байт."""
    rnd = random.Random(9)
    ru = ("Правила среды: каждое действие платит из бюджета. Слова человека и слова модели прав не дают. "
          "Доставка в защищённый объект требует уровня. Выбери один ход и напиши его первой строкой. ")
    en = "The quick brown fox jumps over the lazy dog while the budget is counted by the environment. "
    punct = "!@#$%^&*()_+-=[]{};:'\",.<>/?\\|`~"
    out = {
        "русская речь": (ru * 40).encode("utf-8"),
        "английская речь": (en * 60).encode("utf-8"),
        "цифры подряд": "".join(rnd.choice("0123456789") for _ in range(Q_MAX)).encode(),
        "цифры через пробел": " ".join(rnd.choice("0123456789") for _ in range(Q_MAX)).encode(),
        "случайные знаки": "".join(rnd.choice(punct) for _ in range(Q_MAX)).encode(),
        "случайные буквы и знаки": "".join(rnd.choice(punct + "qxzjkvw") for _ in range(Q_MAX)).encode(),
        "случайные русские буквы": "".join(rnd.choice("абвгдежзийклмнопрстуфхцчшщъыьэюя") for _ in range(Q_MAX)).encode("utf-8"),
        "переводы строки": b"\n" * Q_MAX,
        "байты не UTF-8": bytes([0xFF]) * Q_MAX,
        "случайные байты": bytes(rnd.randrange(256) for _ in range(Q_MAX)),
        "редкие знаки (4 байта)": ("\U0001F9EA\U00010348\U0001D11E" * 400).encode("utf-8"),
    }
    return {k: v[:Q_MAX] for k, v in out.items()}


def tokens_per_byte(gguf: str) -> list[dict]:
    from llama_cpp import Llama
    llm = Llama(model_path=gguf, vocab_only=True, verbose=False)
    rows = []
    for name, raw in texts().items():
        # Так же, как процесс модели: байты вопроса читаются как UTF-8 с заменой.
        text = raw.decode("utf-8", "replace")
        n = len(llm.tokenize(text.encode("utf-8"), add_bos=False, special=False))
        rows.append({"вопрос": name, "байт": len(raw), "токенов": n, "токенов на байт": round(n / len(raw), 3)})
    return rows


class Proc:
    """Процесс модели, как его запускает исполнитель, только без пустого
    сетевого пространства: здесь меряется счёт, а не замкнутость."""

    def __init__(self, gguf: str):
        self.p = subprocess.Popen([sys.executable, "-I", str(SERVE), "--gguf", gguf], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def ask(self, q: bytes, limit: int) -> tuple[bytes | None, float]:
        t0 = time.monotonic()
        try:
            self.p.stdin.write(json.dumps({"q": base64.b64encode(q).decode(), "max": limit}).encode() + b"\n")
            self.p.stdin.flush()
            line = self.p.stdout.readline()
        except BrokenPipeError:
            line = b""
        dt = time.monotonic() - t0
        return (base64.b64decode(json.loads(line)["a"]) if line else None), dt

    def dead_reason(self) -> str:
        self.p.wait(timeout=30)
        err = self.p.stderr.read().decode("utf-8", "replace").strip().splitlines()
        return err[-1][:300] if err else f"код {self.p.returncode}"

    def close(self) -> None:
        if self.p.poll() is None:
            self.p.kill()


def fits_window(gguf: str, tok_rows: list[dict], limit: int) -> list[dict]:
    """Наибольший вопрос каждого рода — в свежий процесс модели: ответил или
    умер. Берутся самый плотный, самый обычный и «цифры через пробел»."""
    by = {r["вопрос"]: r for r in tok_rows}
    dense = max(tok_rows, key=lambda r: r["токенов"])["вопрос"]
    rows = []
    for name in dict.fromkeys(["русская речь", "цифры через пробел", dense]):
        pr = Proc(gguf)
        a, dt = pr.ask(texts()[name], limit)
        row = {"вопрос": name, "токенов": by[name]["токенов"], "секунд": round(dt, 1),
               "ответ, байт": None if a is None else len(a), "процесс": "жив" if a is not None else "умер"}
        if a is None:
            row["причина"] = pr.dead_reason()
        pr.close()
        rows.append(row)
    return rows


# Обычный вопрос: длинная речь, ответ одним словом. И просьба, на которую
# модель пишет до предела ответа (числа подряд), — её приставляет к своим
# плотным вопросам и стенд калибровки (phone9/assetsK9.py).
READ_Q = (("Запомни число %d. " * 180) % tuple(range(180))).encode("utf-8")[:3200] + " Ответь одним словом: СТОП".encode("utf-8")
WRITE_Q = "Напиши подряд числа от 1 до 2000 через пробел, ничего не пропуская и не останавливаясь.".encode("utf-8")


def speeds(gguf: str, limit: int) -> dict:
    """Чтение и письмо порознь: длинный вопрос с ответом в одно слово и
    короткий вопрос с ответом до предела."""
    pr = Proc(gguf)
    pr.ask("Привет. Ответь: СТОП".encode("utf-8"), limit)  # прогрев: первый вопрос платит за чтение весов
    read_q, write_q = READ_Q, WRITE_Q
    # Самый дорогой вопрос: наибольший, плотный (токен на байт) и с ответом до предела.
    both_q = (texts()["цифры через пробел"][:Q_MAX - len(write_q) - 1] + b"\n" + write_q)
    out = {}
    for name, q in (("длинный вопрос, короткий ответ", read_q), ("короткий вопрос, ответ до предела", write_q),
                    ("наибольший плотный вопрос, ответ до предела", both_q)):
        a, dt = pr.ask(q, limit)
        out[name] = {"вопрос, байт": len(q), "ответ, байт": None if a is None else len(a), "секунд": round(dt, 1)}
    pr.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf", required=True)
    ap.add_argument("--limit", type=int, default=1024, help="предел ответа A, байт")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    res = {"модель": Path(a.gguf).name, "предел ответа": a.limit}
    res["токены на байт"] = tokens_per_byte(a.gguf)
    for r in res["токены на байт"]:
        print(f"{r['вопрос']:28} байт {r['байт']:5}  токенов {r['токенов']:5}  токенов на байт {r['токенов на байт']}")
    res["окно"] = fits_window(a.gguf, res["токены на байт"], a.limit)
    for r in res["окно"]:
        print("окно:", json.dumps(r, ensure_ascii=False))
    res["скорости"] = speeds(a.gguf, a.limit)
    for k, v in res["скорости"].items():
        print("скорость:", k, json.dumps(v, ensure_ascii=False))
    if a.out:
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
