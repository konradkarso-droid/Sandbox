"""Сверка родной программы модели (model9/native/serve9.cpp) с serve.py —
пункт 4 порядка работ спецификации шага 9б.

Мерило — serve.py: на одних и тех же строках входа выход обязан совпасть
байт в байт, а где serve.py падает — родная программа тоже обязана умереть
(код выхода не 0), выдав до того то же самое.

Части:
  stub    — заглушки --stub и --script: случайные и нарочно трудные вопросы
            (неверный UTF-8, base64 с мусором, разные записи предела, строки
            «> » сценария), плюс строки, на которых обе обязаны упасть;
  prompt  — токены вопроса после шаблона беседы: llama-cpp-python против
            прибора render9 (тот же код оформления, что в программе);
  live    — живая модель: вопросы всех родов из спецификации (§4.4, §7.4) и
            трудные для повторения места llama-cpp-python (обрезка по пределу,
            недописанный многобайтный знак, кусок токена длиннее 32 байт,
            служебные строки в вопросе);
  threads — то же на живой модели при другом числе потоков: ответ от числа
            потоков зависеть не должен;
  fix     — выдача ответа против правила (oracle): не больше N токенов, текст
            каждого токена целиком; ответ правила строится прямо из примитивов
            llama-cpp-python, мимо цикла выдачи обеих программ. Порча —
            прежние программы с двумя дефектами (--old-serve, --old-serve9);
  strace  — родная программа под strace: ни сети, ни записи файлов.

Чего сверка не умеет: она видит только поданные вопросы; живую модель —
только ту, что дана (--gguf), и только на этом процессоре.

Запуск: PYTHONPATH=. python3 experiments/native9.py ЧАСТЬ --serve9 ПРОГРАММА
[--render9 ПРИБОР] [--gguf МОДЕЛЬ] [--old-serve ФАЙЛ] [--old-serve9 ПРОГРАММА]
[--out ФАЙЛ.json]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SERVE = HERE / "model9" / "serve.py"
RESULTS = HERE / "experiments" / "results"


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def req(q: bytes, limit) -> bytes:
    return json.dumps({"q": b64(q), "max": limit}).encode() + b"\n"


def run(argv: list[str], data: bytes, timeout: float = 3600) -> tuple[int, bytes, float]:
    t0 = time.monotonic()
    r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout, time.monotonic() - t0


def py_argv(mode: list[str]) -> list[str]:
    return [sys.executable, "-I", str(SERVE), *mode]


# ---------------------------------------------------------------- заглушки

PREFIXES = ["ЭХО ", "ДЛИННО", "РАЗРЕШАЮ", "СП", "ЭХО", "ЭХ", "СТОП", "", "> ", "ДЛИН"]


def rnd_bytes(r: random.Random, n: int) -> bytes:
    """Байты, в которых часто встречаются начала и куски знаков UTF-8."""
    pool = [b"\xd0", b"\xd1", b"\x80", b"\xbf", b"\xe0", b"\xed", b"\xa0", b"\xf0", b"\xf4", b"\x90",
            b"\x8f", b"\xc0", b"\xc1", b"\xf5", b"\xff", "я".encode(), "€".encode(), "😀".encode(),
            b"\n", b"> ", b"a", b" ", b"\x00", b"\xe2\x82", b"\xf0\x9f\x98"]
    out = b""
    while len(out) < n:
        out += r.choice(pool) if r.random() < 0.7 else bytes([r.randrange(256)])
    return out


def rnd_limit(r: random.Random):
    k = r.random()
    if k < 0.55:
        return r.randrange(0, 5000)
    if k < 0.65:
        return r.randrange(-300, 0)
    if k < 0.75:
        return round(r.uniform(-50, 3000), 2)
    if k < 0.8:
        return r.choice([True, False])
    if k < 0.9:
        return r.choice(["42", " 17 ", "-5", "+8", "0"])
    return r.choice([10 ** 25, -10 ** 25, 1e30])


def fuzz_lines(r: random.Random, n: int, script: bool) -> list[bytes]:
    lines = []
    for _ in range(n):
        if script:
            k = r.randrange(0, 6)
            parts = [r.choice(["> ход", ">  x", ">", "> ", " > нет", "x> ", "\r> r", "> \r"]) for _ in range(k)]
            parts += [rnd_bytes(r, r.randrange(0, 12)).decode("utf-8", "replace") for _ in range(r.randrange(0, 3))]
            r.shuffle(parts)
            q = "\n".join(parts).encode("utf-8", "surrogatepass")
            if r.random() < 0.2:
                q += rnd_bytes(r, 5)
        else:
            q = r.choice(PREFIXES).encode() + rnd_bytes(r, r.randrange(0, 40))
            if r.random() < 0.1:
                q = rnd_bytes(r, r.randrange(1, 8)) + q
        limit = rnd_limit(r)
        # огромный предел у «ДЛИННО» роняет обе (проверяется отдельно, в part_stub)
        if q.startswith("ДЛИННО".encode()) and isinstance(limit, (int, float)) and abs(limit) > 10 ** 6:
            limit = 77
        enc = b64(q)
        form = r.random()
        if form < 0.15:  # мусор внутри base64 — Python его пропускает
            pos = r.randrange(0, len(enc) + 1)
            enc = enc[:pos] + r.choice([" ", "\\n", "*", "!", "\\t", "="]) + enc[pos:]
        elif form < 0.25 and enc.endswith("="):  # лишние «=» и мусор после дополнения
            enc = enc + r.choice(["=", "==", "*", "=*="])
        elif form < 0.3:
            enc = enc.replace("A", "\\u0041") if "A" in enc else enc
        if form >= 0.3 and form < 0.45:
            line = '{ "max" : %s , "q":"%s", "x": [1, {"y": null}] }' % (json.dumps(limit), enc)
        elif form >= 0.45 and form < 0.5:
            line = '{"q": "AAAA", "max": 1, "q": "%s", "max": %s}' % (enc, json.dumps(limit))
        else:
            line = '{"q": "%s", "max": %s}' % (enc, json.dumps(limit))
        lines.append(line.encode() + (b"\n" if r.random() < 0.97 else b"\r\n"))
    return lines


# Строки, на которых serve.py падает. Каждая — в своём процессе, после одной
# верной строки: верный ответ обязан выйти, затем — смерть.
BAD = [
    b"\n", b"   \n", b"[]\n", b'"q"\n', b"{}\n", b'{"q": "0J/RgA=="}\n', b'{"max": 5}\n',
    b'{"q": "0J/RgA==", "max": null}\n', b'{"q": "0J/RgA==", "max": "abc"}\n',
    b'{"q": "0J/RgA==", "max": NaN}\n', b'{"q": "0J/RgA==", "max": Infinity}\n',
    b'{"q": "0J/Rg", "max": 5}\n', b'{"q": "0", "max": 5}\n', b'{"q": "\xd0\xaf", "max": 5}\n',
    b'{"q": "\\u042f", "max": 5}\n', b'{"q": 5, "max": 5}\n', b'{"q": "0J/RgA==", "max": 5} x\n',
    b'{"q": "0J/RgA==", "max": 5,}\n', b'{"q": "0J/R\\u00e9gA==", "max": 5}\n', b'{"q": "0J/RgA==A", "max": 5}\n',
    b'{"q": "QUI=QQQ", "max": 5}\n', b'{"q": "QQ==QQQ", "max": 5}\n', b'{"q": "0J/RgA==", "max": 05}\n', b'{"q": "0J/RgA==", "max": 1.}\n',
    b'{"q": "0J/R\x01gA==", "max": 5}\n', b'{"q": "0J/RgA==", "max": [5]}\n', b"\xff\n",
]


def part_stub(serve9: str) -> dict:
    r = random.Random(9)
    res: dict = {"stub": {}, "script": {}, "падения": {}}
    # --stub: 20 процессов по 200 строк
    same = total = 0
    for batch in range(20):
        data = b"".join(fuzz_lines(r, 200, script=False))
        a, b = run(py_argv(["--stub"]), data, 120), run([serve9, "--stub"], data, 120)
        total += 1
        if a[0] == b[0] == 0 and a[1] == b[1]:
            same += 1
        else:
            res["stub"].setdefault("расхождения", []).append({"пачка": batch, "коды": [a[0], b[0]],
                                                             "первые байты разницы": first_diff(a[1], b[1])})
    res["stub"].update({"пачек": total, "строк": total * 200, "совпало пачек": same})
    # --script: 10 сценариев по 200 вопросов
    same = total = 0
    with tempfile.TemporaryDirectory() as tmp:
        for k in range(10):
            moves = [r.choice(["СТОП", "РЕШИ 1", "", "ход \"в кавычках\"", "😀é", "\\n", "ЧИТАЙ\nЗАДАНИЕ"])
                     for _ in range(r.randrange(0, 6))]
            f = Path(tmp) / f"s{k}.json"
            text = json.dumps(moves, ensure_ascii=r.random() < 0.5)
            f.write_text(text, encoding="utf-8")
            data = b"".join(fuzz_lines(r, 200, script=True))
            a = run(py_argv(["--script", str(f)]), data, 120)
            b = run([serve9, "--script", str(f)], data, 120)
            total += 1
            if a[0] == b[0] == 0 and a[1] == b[1]:
                same += 1
            else:
                res["script"].setdefault("расхождения", []).append({"сценарий": k, "коды": [a[0], b[0]],
                                                                   "первые байты разницы": first_diff(a[1], b[1])})
        # сценарий не список и нет файла — обе обязаны умереть при старте
        bad = Path(tmp) / "bad.json"
        bad.write_text('{"a": 1}', encoding="utf-8")
        for f in (bad, Path(tmp) / "нет.json"):
            a = run(py_argv(["--script", str(f)]), req(b"x", 5), 60)
            b = run([serve9, "--script", str(f)], req(b"x", 5), 60)
            ok = a[0] != 0 and b[0] != 0 and a[1] == b[1] == b""
            res["падения"][f"сценарий {f.name}"] = "оба умерли" if ok else f"коды {a[0]} {b[0]}"
    res["script"].update({"сценариев": total, "строк": total * 200, "совпало сценариев": same})
    # падения на строках входа
    good = req("ЭХО жив".encode(), 10)
    fails = 0
    for i, line in enumerate(BAD):
        a = run(py_argv(["--stub"]), good + line, 60)
        b = run([serve9, "--stub"], good + line, 60)
        ok = a[0] != 0 and b[0] != 0 and a[1] == b[1] and a[1].count(b"\n") == 1
        if not ok:
            fails += 1
            res["падения"][f"строка {i}"] = {"строка": line.decode("utf-8", "replace"), "коды": [a[0], b[0]],
                                             "выход": [a[1].decode(errors="replace"), b[1].decode(errors="replace")]}
    res["падения"]["строк, где обе умерли после верного ответа"] = f"{len(BAD) - fails} из {len(BAD)}"
    # ДЛИННО с огромным пределом: Python — MemoryError, программа — отказ
    a = run(py_argv(["--stub"]), req("ДЛИННО".encode(), 10 ** 25), 120)
    b = run([serve9, "--stub"], req("ДЛИННО".encode(), 10 ** 25), 120)
    res["падения"]["ДЛИННО при пределе 10**25"] = "оба умерли" if a[0] != 0 and b[0] != 0 else f"коды {a[0]} {b[0]}"
    # без перевода строки в конце и пустой вход
    for name, data in (("без перевода строки", req("ЭХО конец".encode(), 50)[:-1]), ("пустой вход", b"")):
        a, b = run(py_argv(["--stub"]), data, 60), run([serve9, "--stub"], data, 60)
        res["падения"][name] = "совпало" if (a[0], a[1]) == (b[0], b[1]) else f"{a[:2]} {b[:2]}"
    # «СПИ» — молчит; оба обязаны не ответить за 3 секунды
    quiet = []
    for argv in (py_argv(["--stub"]), [serve9, "--stub"]):
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        p.stdin.write(req("СПИ".encode(), 5))
        p.stdin.flush()
        time.sleep(3)
        quiet.append(p.poll() is None)
        p.kill()
        p.wait()
    res["падения"]["СПИ: оба молчат 3 с"] = "да" if all(quiet) else f"нет: {quiet}"
    res["итог"] = ("совпало" if same == total and res["stub"]["совпало пачек"] == res["stub"]["пачек"] and fails == 0
                   and all(v in ("оба умерли", "совпало", "да") for k, v in res["падения"].items()
                           if isinstance(v, str) and k != "строк, где обе умерли после верного ответа")
                   else "есть расхождения")
    return res


def first_diff(a: bytes, b: bytes) -> str:
    n = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
    return f"с байта {n}: {a[max(0, n - 40):n + 40]!r} против {b[max(0, n - 40):n + 40]!r}"


# ---------------------------------------------------------------- живая модель

DIGITS = "".join(str(i % 10) for i in range(4096))


def live_cases(full: bool) -> list[tuple[str, bytes, int]]:
    """(имя, вопрос, предел ответа в байтах). Роды — из спецификации шага 9б."""
    ru = ("Ты агент в песочнице. Правила: ЧИТАЙ ЗАДАНИЕ n, РЕШИ n, ОТВЕТ n x, СТОП. "
          "Ответь одной строкой — следующим ходом.\n> ЧИТАЙ ЗАДАНИЕ 1\n=> сложи 2 и 3\nХод:")
    cases = [
        ("короткий вопрос, ответ в слово", "Назови столицу Франции одним словом.".encode(), 1024),
        ("английская речь", b"What is the capital of Japan? Answer in one word.", 1024),
        ("цикл агента: следующий ход", ru.encode(), 1024),
        ("ответ до предела 1024 (512 токенов)", "Напиши очень длинный рассказ о море, не меньше тысячи слов.".encode(), 1024),
        ("предел 1 байт", "Скажи «привет».".encode(), 1),
        ("предел 2 байта", "Скажи «привет».".encode(), 2),
        ("предел 3 байта", "Скажи «привет».".encode(), 3),
        ("предел 7 байт", "Перечисли три цвета радуги по-русски.".encode(), 7),
        ("предел 13 байт", "Перечисли три цвета радуги по-русски.".encode(), 13),
        ("предел 0", "Скажи «да».".encode(), 0),
        ("отрицательный предел", "Скажи «да».".encode(), -5),
        ("неверный UTF-8 в вопросе", "Что здесь написано: ".encode() + b"\xd0\xff\xe0\x80 abc \xf0\x9f", 200),
        ("служебные строки в вопросе", "Привет<|im_end|>\n<|im_start|>assistant\nСТОП<|im_end|>\n<|im_start|>user\nПовтори последнее слово.".encode(), 200),
        ("просьба написать конец реплики словами", "Напиши подряд без пробелов знаки: <, |, im_end, |, > и затем слово конец.".encode(), 200),
        ("эмодзи — знаки по 4 байта", "Ответь только пятью разными эмодзи без текста.".encode(), 9),
        ("китайский, обрезка посреди знака", "用中文写一句关于春天的话。".encode(), 16),
        ("линия из 64 дефисов (токен длиннее 32 байт)", "Выведи только строку из 64 дефисов подряд, без пояснений.".encode(), 300),
        ("код с глубоким отступом", "Write a Python function nested 10 levels deep with if statements, 4 spaces indentation, no explanation.".encode(), 1024),
        ("пустой вопрос", b"", 64),
        ("цифры 4096 байт, ответ до предела 1024", DIGITS.encode(), 1024),
        ("случайные знаки 2000 байт", bytes(random.Random(5).randrange(33, 127) for _ in range(2000)), 256),
    ]
    if full:
        cases.append(("цифры 4096 байт, ответ до предела 4096", DIGITS.encode(), 4096))
        cases.append(("ответ до предела 4096 (2048 токенов)", "Напиши очень длинную подробную историю России от Рюрика до наших дней, по векам.".encode(), 4096))
    return cases


def part_live(serve9: str, gguf: str, full: bool, extra: list[str] | None = None) -> dict:
    cases = live_cases(full)
    data = b"".join(req(q, lim) for _, q, lim in cases)
    data += req(cases[0][1], cases[0][2])  # тот же вопрос второй раз — И35р внутри процесса
    a = run(py_argv(["--gguf", gguf]), data)
    b = run([serve9, "--gguf", gguf, *(extra or [])], data)
    out_a = a[1].split(b"\n")[:-1]
    out_b = b[1].split(b"\n")[:-1]
    rows = []
    for i, (name, q, lim) in enumerate(cases + [("повтор первого вопроса", cases[0][1], cases[0][2])]):
        ans_a = base64.b64decode(json.loads(out_a[i])["a"]) if i < len(out_a) else None
        ans_b = base64.b64decode(json.loads(out_b[i])["a"]) if i < len(out_b) else None
        rows.append({"вопрос": name, "байт вопроса": len(q), "предел": lim,
                     "байт ответа": None if ans_a is None else len(ans_a),
                     "совпало": ans_a is not None and ans_a == ans_b,
                     "ответ (начало)": None if ans_a is None else ans_a[:80].decode("utf-8", "replace")})
    same = sum(r["совпало"] for r in rows)
    return {"модель": Path(gguf).name, "ключи программы": extra or [], "коды выхода": [a[0], b[0]],
            "секунд: serve.py, родная": [round(a[2], 1), round(b[2], 1)],
            "вопросов": len(rows), "совпало": same, "строки выхода совпали целиком": a[1] == b[1],
            "повтор дал те же байты": rows[-1]["совпало"] and out_b[0] == out_b[-1], "вопросы": rows}


def part_prompt(render9: str, gguf: str) -> dict:
    """Токены оформленного вопроса: llama-cpp-python против render9."""
    from llama_cpp import Llama
    from llama_cpp import llama_chat_format as F
    llm = Llama(model_path=gguf, n_ctx=512, verbose=False, vocab_only=True)
    tmpl = llm.metadata["tokenizer.chat_template"]
    eos, bos = llm.token_eos(), llm.token_bos()
    fmt = F.Jinja2ChatFormatter(template=tmpl, eos_token=llm._model.token_get_text(eos),
                                bos_token=llm._model.token_get_text(bos), stop_token_ids=[eos])
    qs = [q for _, q, _ in live_cases(False)]
    r = random.Random(11)
    qs += [rnd_bytes(r, r.randrange(0, 300)) for _ in range(200)]
    qs += ["{{ 7*7 }} {% if true %}x{% endif %}".encode(), "  \n\t ведущие и хвостовые пробелы \n\n".encode()]
    want = []
    for q in qs:
        p = fmt(messages=[{"role": "user", "content": q.decode("utf-8", "replace")}]).prompt
        want.append(" ".join(map(str, llm.tokenize(p.encode("utf-8"), add_bos=False, special=True))))
    rc, out, _ = run([render9, gguf], "".join(b64(q) + "\n" for q in qs).encode())
    got = out.decode().split("\n")[:-1]
    same = sum(1 for a, b in zip(want, got) if a == b)
    # Текст токенов: случайные цепочки, где часто встречаются токены с куском
    # длиннее 32 байт, служебные токены и начало текста (BOS) перед пробелом.
    import ctypes
    import llama_cpp as C
    buf = (ctypes.c_char * 1024)()
    n_vocab = llm.n_vocab()
    longs = [t for t in range(n_vocab) if C.llama_token_to_piece(llm._model.vocab, t, buf, 1024, 0, False) > 32]
    spaced = [t for t in range(0, n_vocab, 7) if C.llama_token_to_piece(llm._model.vocab, t, buf, 1024, 0, False) > 0
              and buf[0] == b" "][:200]
    special = [x for x in (bos, eos, llm.token_nl()) if x >= 0] + list(range(n_vocab - 30, n_vocab))
    seqs = []
    for k in range(400):
        pool = [lambda: r.choice(longs) if longs else 0, lambda: r.choice(spaced), lambda: r.choice(special),
                lambda: r.randrange(n_vocab)]
        s = [r.choice(pool)() for _ in range(r.randrange(0, 12))]
        if k % 4 == 0 and bos >= 0:
            s = [bos] + s
        seqs.append(s)
    # Ждём текст каждого токена целиком (буфер 1024 байта), а не
    # llm.detokenize: у той буфер 32 байта, и длинный кусок пропадает.
    def full(s: list[int]) -> bytes:
        b = b"".join(bytes(buf[:C.llama_token_to_piece(llm._model.vocab, t, buf, 1024, 0, False)]) for t in s)
        return b[1:] if s and s[0] == bos and b[:1] == b" " else b
    want_d = [b64(full(s)) for s in seqs]
    rc2, out2, _ = run([render9, gguf, "--detok"], "".join(" ".join(map(str, s)) + "\n" for s in seqs).encode())
    got_d = out2.decode().split("\n")[:-1]
    same_d = sum(1 for a, b in zip(want_d, got_d) if a == b)
    return {"модель": Path(gguf).name, "способ оформления у llama-cpp-python": llm.chat_format,
            "вопросов": len(qs), "токены совпали": same, "код прибора": rc,
            "текст токенов: цепочек": len(seqs), "совпало": same_d, "токенов с куском длиннее 32 байт в словаре": len(longs),
            "цепочек с таким токеном": sum(1 for s in seqs if set(s) & set(longs))}


# Вызовы системы, разрешённые программе модели, — список разрешённого, а не
# перечень запретов: вызов не из списка — нарушение, пока его не разобрали и
# не внесли сюда. Так новый путь наружу (местный сокет, sendfile, общая
# память) не проходит молча оттого, что его никто не вспомнил. Каждый вызов
# списка сам по себе ничего из процесса не выпускает: память, потоки, время,
# чтение. Четыре вызова списка могут быть и опасными, их аргументы
# проверяются отдельно (strace_verdict): open/openat — только на чтение;
# write — только в каналы 1 и 2; execve — один, запуск самой программы;
# clone/clone3 — только поток (CLONE_THREAD), а не новый процесс.
# Чего список не умеет: он верен для того, что программа сделала на поданных
# вопросах, а не для всего, что она могла бы сделать.
STRACE_ALLOWED = {
    "read", "pread64", "readv", "write", "writev", "close", "lseek",
    "open", "openat", "access", "faccessat", "faccessat2", "fstat", "newfstatat", "stat", "lstat", "statx",
    "fadvise64", "readlink", "readlinkat", "getcwd",
    "mmap", "munmap", "mprotect", "madvise", "brk", "mremap",
    "clone", "clone3", "futex", "set_robust_list", "rseq", "set_tid_address", "sched_getaffinity",
    "sched_yield", "exit", "exit_group",
    "rt_sigaction", "rt_sigprocmask", "rt_sigreturn",
    "execve", "arch_prctl", "prlimit64", "getrandom", "sysinfo", "uname",
    "getpid", "gettid", "nanosleep", "clock_nanosleep", "clock_gettime",
}
STRACE_NET = {"socket", "socketpair", "connect", "bind", "listen", "accept", "accept4", "sendto", "sendmsg",
              "sendmmsg", "recvfrom", "recvmsg", "recvmmsg", "getsockopt", "setsockopt", "shutdown"}
STRACE_FSMOD = {"unlink", "unlinkat", "rename", "renameat", "renameat2", "mkdir", "mkdirat", "rmdir", "truncate",
                "ftruncate", "link", "linkat", "symlink", "symlinkat", "chmod", "fchmod", "fchmodat", "chown",
                "fchown", "fchownat", "lchown", "utimensat", "mknod", "mknodat", "creat"}


def strace_verdict(lines: list[str]) -> dict:
    """Разбор вывода `strace -f -o`: что из сделанного программой нарушает
    «ни сети, ни записи файлов, ни других путей наружу»."""
    calls = []
    for x in lines:
        m = re.match(r"^(?:\d+\s+)?([a-z_][a-z0-9_]*)\(", x)   # «<... resumed>», сигналы, выходы — не вызовы
        if m:
            calls.append((m.group(1), x))
    names = {n for n, _ in calls}
    opens = [x for n, x in calls if n in ("open", "openat", "openat2")]
    writes_open = [x for x in opens if re.search(r"O_WRONLY|O_RDWR|O_CREAT|O_TRUNC", x)]
    wfd = sorted({m.group(1) for n, x in calls if n in ("write", "writev", "pwrite64", "pwritev", "pwritev2")
                  for m in [re.search(r"\((\d+)", x)] if m})
    v = {"системных вызовов": len(calls),
         "незнакомые вызовы": sorted(names - STRACE_ALLOWED),
         "вызовов сети": sum(1 for n, _ in calls if n in STRACE_NET),
         "открытий на запись": len(writes_open),
         "изменений файловой системы": sum(1 for n, _ in calls if n in STRACE_FSMOD),
         "запись в каналы": wfd,
         "лишний execve": max(0, sum(1 for n, _ in calls if n == "execve") - 1),
         "клон не поток": sum(1 for n, x in calls if n in ("clone", "clone3") and "CLONE_THREAD" not in x),
         "что открывалось на чтение": sorted({re.sub(r'.*?"([^"]+)".*', r"\1", x) for x in opens})[:40]}
    v["чисто"] = (not v["незнакомые вызовы"] and v["вызовов сети"] == 0 and v["открытий на запись"] == 0
                  and v["изменений файловой системы"] == 0 and set(wfd) <= {"1", "2"}
                  and v["лишний execve"] == 0 and v["клон не поток"] == 0)
    v["примеры нарушений"] = ([x for n, x in calls if n not in STRACE_ALLOWED or n in STRACE_NET or n in STRACE_FSMOD]
                              + writes_open)[:5]
    return v


def strace_run(argv: list[str], data: bytes) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "st.txt"
        subprocess.run(["strace", "-f", "-qq", "-s", "200", "-o", str(log), *argv], input=data,
                       capture_output=True, timeout=1800)
        return log.read_text(errors="replace").splitlines()


# Порча для самопроверки разбора: маленькая программа, которая читает вход,
# пишет ответ и — смотря по ключу — делает одно запрещённое. Разбор обязан
# поймать каждую порчу своей проверкой, а чистый вариант — пропустить.
SPOIL_C = r"""
#define _GNU_SOURCE
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/sendfile.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
int main(int argc, char **argv) {
    char buf[4096]; ssize_t n = read(0, buf, sizeof buf); (void) n;
    const char *k = argc > 1 ? argv[1] : "";
    if (!strcmp(k, "net")) { int s = socket(AF_INET, SOCK_STREAM, 0); struct sockaddr_in a = {0};
        a.sin_family = AF_INET; a.sin_port = htons(9); a.sin_addr.s_addr = htonl(0xC0000201);
        connect(s, (struct sockaddr *) &a, sizeof a); }
    if (!strcmp(k, "pair")) { int sv[2]; socketpair(AF_UNIX, SOCK_STREAM, 0, sv); }
    if (!strcmp(k, "file")) { int f = open(argv[2], O_WRONLY | O_CREAT, 0600); write(f, "x", 1); }
    if (!strcmp(k, "rename")) rename(argv[2], argv[3]);
    if (!strcmp(k, "sendfile")) sendfile(1, 0, NULL, 1);
    if (!strcmp(k, "memfd")) memfd_create("m", 0);
    if (!strcmp(k, "fork")) { pid_t p = fork(); if (p == 0) _exit(0); waitpid(p, NULL, 0); }
    if (!strcmp(k, "exec")) { char *a[] = {"/bin/true", NULL}; execv(a[0], a); }
    write(1, "{\"a\": \"\"}\n", 10);
    return 0;
}
"""
SPOILS = (("чисто", [], None), ("сеть", ["net"], "вызовов сети"), ("местный сокет", ["pair"], "вызовов сети"),
          ("запись файла", ["file", "{tmp}/w"], "открытий на запись"),
          ("переименование", ["rename", "{tmp}/a", "{tmp}/b"], "изменений файловой системы"),
          ("sendfile", ["sendfile"], "незнакомые вызовы"), ("общая память", ["memfd"], "незнакомые вызовы"),
          ("новый процесс", ["fork"], "клон не поток"), ("запуск программы", ["exec"], "лишний execve"))


def strace_selftest() -> dict:
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        return {"итог": "нет компилятора"}
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        src, exe = Path(tmp) / "spoil.c", Path(tmp) / "spoil"
        src.write_text(SPOIL_C)
        subprocess.run([cc, "-O0", "-o", str(exe), str(src)], check=True, capture_output=True)
        for name, args, check in SPOILS:
            v = strace_verdict(strace_run([str(exe), *[x.format(tmp=tmp) for x in args]], req(b"x", 1)))
            ok = v["чисто"] if check is None else bool(v[check]) and not v["чисто"]
            out[name] = {"ждали": check or "чисто", "поймано": ok, "незнакомые вызовы": v["незнакомые вызовы"]}
    out["итог"] = "каждая порча поймана своей проверкой" if all(x["поймано"] for x in out.values()) else "есть пропуски"
    return out


# ---------------------------------------------------------------- выдача ответа против правила

def fix_cases() -> list[tuple[str, bytes, int]]:
    """Вопросы живой сверки и ещё такие, где правило выдачи задевается чаще:
    предел токенов приходится посреди многобайтного знака (эмодзи и
    китайский при разных пределах) и токены с куском длиннее 32 байт."""
    cases = live_cases(False)
    emo = "Ответь только пятью разными эмодзи без текста.".encode()
    zh = "用中文写一句关于春天的话。".encode()
    cases += [(f"эмодзи, предел {n}", emo, n) for n in range(4, 17)]
    cases += [(f"китайский, предел {n}", zh, n) for n in range(4, 13)]
    cases += [("отступ в 40 пробелов", "Повтори строку как есть, без пояснений: «                                        x»".encode(), 300),
              ("черта из 80 знаков «=»", "Выведи только строку из 80 знаков = подряд, без пояснений.".encode(), 300)]
    return cases + znak_cases()


def znak_cases() -> list[tuple[str, bytes, int]]:
    """Предел токенов посреди знака так, чтобы это было видно в байтах:
    редкие иероглифы модель пишет двумя токенами, а цифры и пробелы — по
    одному, поэтому N токенов дают меньше 2N байт, и знак, дописанный сверх
    предела, ещё помещается в предел байт. Пределы подобраны прогоном на
    Qwen2.5-1.5B; у другой модели правило может не задеться — это видно по
    «правило задето»."""
    a = "Повтори дословно, без пояснений: 1龘2靐3齉4麤5爨6驫7鱻8龘9靐".encode()
    b = "Повтори дословно, без пояснений: 龘 靐 齉 麤 爨 驫 鱻 龘 靐".encode()
    return ([(f"цифры и иероглифы, предел {n}", a, n) for n in (4, 10, 20, 30)]
            + [(f"иероглифы через пробел, предел {n}", b, n) for n in (7, 11, 16, 22, 28)])


def oracle(gguf: str, cases: list[tuple[str, bytes, int]]) -> list[dict]:
    """Ответ по правилу выдачи, построенный прямо из примитивов библиотеки,
    без цикла выдачи serve.py и родной программы: вопрос оформлен шаблоном и
    прочитан той же llama-cpp-python, слова выбраны её generate; затем —
    первые N = max(1, предел // 2) токенов или меньше, если раньше конец
    реплики; текст каждого токена целиком (буфер 1024 байта); обрезка по
    тексту конца реплики; сборка — как в serve.py. Отдельно — по каждому
    вопросу, задето ли правило: сколько кусков длиннее 32 байт среди выданных
    токенов и кончается ли текст N токенов недописанным знаком."""
    import ctypes
    import llama_cpp.llama_cpp as C          # подмена — в том же модуле, что у serve.py
    from llama_cpp import Llama
    from llama_cpp import llama_chat_format as F
    sys.path.insert(0, str(SERVE.parent))
    import serve as S                        # только числа окна
    # Без перекладки весов, как в serve.py: с перекладкой числа модели другие,
    # и слова выбирались бы другие.
    default = C.llama_model_default_params

    def no_repack():
        p = default()
        p.use_extra_bufts = False
        return p
    C.llama_model_default_params = no_repack
    llm = Llama(model_path=gguf, n_ctx=S.N_CTX, n_threads=2, seed=0, verbose=False)
    eos, bos = llm.token_eos(), llm.token_bos()
    eos_text = llm._model.token_get_text(eos)
    fmt = F.Jinja2ChatFormatter(template=llm.metadata["tokenizer.chat_template"], eos_token=eos_text,
                                bos_token=llm._model.token_get_text(bos), stop_token_ids=[eos])
    buf = (ctypes.c_char * 1024)()

    def full(t: int) -> bytes:
        n = C.llama_token_to_piece(llm._model.vocab, t, buf, 1024, 0, False)
        assert 0 <= n <= 1024
        return bytes(buf[:n])

    def incomplete_tail(b: bytes) -> bool:
        i = len(b) - 1
        while i >= 0 and len(b) - i <= 4 and (b[i] & 0xC0) == 0x80:
            i -= 1
        if i < 0 or b[i] < 0xC0:
            return False
        need = 2 if b[i] < 0xE0 else 3 if b[i] < 0xF0 else 4
        return len(b) - i < need

    out = []
    for name, q, lim in cases:
        p = fmt(messages=[{"role": "user", "content": q.decode("utf-8", "replace")}]).prompt
        prompt = llm.tokenize(p.encode("utf-8"), add_bos=False, special=True)
        n_max = max(1, lim // 2)
        if n_max + len(prompt) >= S.N_CTX:
            n_max = S.N_CTX - len(prompt)
        llm.reset()
        toks, why = [], "предел"
        for t in llm.generate(prompt, top_k=1, top_p=0.95, min_p=0.05, typical_p=1.0, temp=0.0, repeat_penalty=1.0):
            if C.llama_vocab_is_eog(llm._model.vocab, t):
                why = "конец реплики"
                break
            toks.append(t)
            if len(toks) >= n_max:
                break
        pieces = [full(t) for t in toks]
        text = b"".join(pieces)
        if toks and toks[0] == bos and text[:1] == b" ":
            text = text[1:]
        cut = text.find(eos_text.encode())
        if cut >= 0:
            text, why = text[:cut], "текст конца реплики"
        ans = text.decode("utf-8", "ignore").encode("utf-8")[:lim].decode("utf-8", "ignore").encode("utf-8")
        out.append({"вопрос": name, "предел": lim, "ответ": ans, "токенов": len(toks), "остановка": why,
                    "кусков длиннее 32 байт": sum(1 for x in pieces if len(x) > 32),
                    "знак недописан на пределе": why == "предел" and incomplete_tail(text)})
    return out


def answers(argv: list[str], cases: list[tuple[str, bytes, int]]) -> tuple[int, list[bytes | None], float]:
    rc, out, sec = run(argv, b"".join(req(q, lim) for _, q, lim in cases))
    lines = out.split(b"\n")[:-1]
    got = [base64.b64decode(json.loads(x)["a"]) for x in lines]
    return rc, got + [None] * (len(cases) - len(got)), sec


def part_fix(serve9: str, gguf: str, old_serve: str | None, old_serve9: str | None, only_znak: bool = False) -> dict:
    """Исправленная выдача: serve.py и родная программа против правила
    (oracle) и друг против друга. Порча — прежние программы с двумя
    дефектами (--old-serve, --old-serve9): сверка обязана их отличить, и
    отличить только там, где правило задето."""
    cases = znak_cases() if only_znak else fix_cases()
    want = oracle(gguf, cases)
    progs = {"serve.py": py_argv(["--gguf", gguf]), "родная": [serve9, "--gguf", gguf]}
    if old_serve:
        progs["прежний serve.py (порча)"] = [sys.executable, "-I", old_serve, "--gguf", gguf]
    if old_serve9:
        progs["прежняя родная (порча)"] = [old_serve9, "--gguf", gguf]
    got = {k: answers(v, cases) for k, v in progs.items()}
    touched = [i for i, w in enumerate(want) if w["кусков длиннее 32 байт"] or w["знак недописан на пределе"]]
    res: dict = {"модель": Path(gguf).name, "вопросов": len(cases),
                 "правило задето": {"кусками длиннее 32 байт": sum(1 for w in want if w["кусков длиннее 32 байт"]),
                                    "недописанным знаком на пределе": sum(1 for w in want if w["знак недописан на пределе"])},
                 "программы": {}}
    for k, (rc, a, sec) in got.items():
        bad = [i for i in range(len(cases)) if a[i] != want[i]["ответ"]]
        res["программы"][k] = {"код выхода": rc, "секунд": round(sec, 1), "совпало с правилом": len(cases) - len(bad),
                               "расхождения": [cases[i][0] for i in bad],
                               "расхождения вне задетых правилом": [cases[i][0] for i in bad if i not in touched]}
    res["serve.py и родная — байт в байт"] = got["serve.py"][1] == got["родная"][1]
    res["вопросы"] = [{k: (v.decode("utf-8", "replace")[:80] if isinstance(v, bytes) else v) for k, v in w.items()}
                      | {"байт ответа": len(w["ответ"])} for w in want]
    clean = all(res["программы"][k]["совпало с правилом"] == len(cases) for k in ("serve.py", "родная"))
    spoiled = [k for k in progs if "порча" in k]
    caught = all(res["программы"][k]["расхождения"] and not res["программы"][k]["расхождения вне задетых правилом"]
                 for k in spoiled)
    res["итог"] = ("обе программы по правилу, байт в байт" if clean and res["serve.py и родная — байт в байт"]
                   else "есть расхождения")
    if spoiled:
        res["итог порчи"] = "каждая поймана, и только на задетых вопросах" if caught else "порча не поймана или поймана не там"
    return res


def part_strace(serve9: str, gguf: str | None) -> dict:
    """Ни сети, ни записи файлов, ни других путей наружу (strace_verdict).
    Сначала самопроверка разбора порчей, затем сама программа."""
    if not shutil.which("strace"):
        return {"итог": "нет strace"}
    res = {"самопроверка разбора": strace_selftest()}
    modes = [("--stub", ["--stub"])] + ([("--gguf", ["--gguf", gguf])] if gguf else [])
    data = req("ЭХО раз".encode(), 50) + req("Скажи «да».".encode(), 50) + req(DIGITS[:500].encode(), 100)
    for name, mode in modes:
        res[name] = strace_verdict(strace_run([serve9, *mode], data))
    # Проба сети — тоже вид программы, но её дело — нарушить: разбор обязан
    # увидеть сеть и у самой родной программы, а не только у порчи.
    probe = strace_verdict(strace_run([serve9, "--probe-net"], b""))
    res["--probe-net (обязан быть пойман)"] = {"вызовов сети": probe["вызовов сети"], "чисто": probe["чисто"]}
    clean = all(res[n]["чисто"] for n, _ in modes)
    res["итог"] = ("чисто" if clean and res["самопроверка разбора"]["итог"].startswith("каждая")
                   and probe["вызовов сети"] > 0 and not probe["чисто"] else "есть нарушения или пропуски")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("part", choices=["stub", "prompt", "live", "threads", "fix", "strace"])
    ap.add_argument("--serve9", required=True)
    ap.add_argument("--render9")
    ap.add_argument("--gguf")
    ap.add_argument("--full", action="store_true", help="live: и наибольший вопрос с ответом до 4096")
    ap.add_argument("--old-serve", help="fix: прежний serve.py — порча")
    ap.add_argument("--old-serve9", help="fix: прежняя родная программа — порча")
    ap.add_argument("--only-znak", action="store_true", help="fix: только вопросы с пределом посреди знака")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.part == "stub":
        res = part_stub(a.serve9)
    elif a.part == "prompt":
        res = part_prompt(a.render9, a.gguf)
    elif a.part == "live":
        res = part_live(a.serve9, a.gguf, a.full)
    elif a.part == "threads":
        res = {"1 и 1": part_live(a.serve9, a.gguf, False, ["--threads", "1", "--threads-batch", "1"]),
               "3 и 5": part_live(a.serve9, a.gguf, False, ["--threads", "3", "--threads-batch", "5"])}
    elif a.part == "fix":
        res = part_fix(a.serve9, a.gguf, a.old_serve, a.old_serve9, a.only_znak)
    else:
        res = part_strace(a.serve9, a.gguf)
    text = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out:
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / a.out).write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
