"""Проверка стенда калибровки шага 9б на сервере — до телефона
(спецификация шага 9б, §17): тот же ведущий Kalibr9, чистый прогон и порчи.
Каждая порча обязана провалить свой вердикт, а чистый прогон — дать «да»
по всем; иначе вердикт стенда ничего не проверяет.

  потоки меняют ответ     — программа модели дописывает к ответу число
                            потоков: К1 «нет»;
  память между вопросами  — дописывает номер вопроса: К2 «нет» (повтор);
  ответ короче предела    — обрезает ответ до 50 байт: К2 и К5 «нет»;
  срок вдвое короче       — T = 0,5 × t(4096): К5 «нет» (−7);
  наибольшие короче       — «наибольшие» задают Р2048 вместо Р4096: К5 «нет»
                            (счёт вопросов);
  журнал К6 испорчен      — в снимке живого агента плата вопроса изменена:
                            разбор (razborK9.py) обязан найти нарушение.

Порчи, которым нужны только поздние этапы, берут ранние готовыми из
чистого прогона (стенд продолжает с первого незаконченного этапа).

  LC_ALL=C.UTF-8 PYTHONPATH=. python3 phone9/checkK9.py --exe ИСПОЛНИТЕЛЬ --serve SERVE9 --model GGUF \\
      --nosock БИБЛИОТЕКА [--out ФАЙЛ.json]

Профиль сервера меньше телефонного: бюджет 65, прогрев 20 с, два круга,
пары потоков 2×2 и 1×1, один сценарий живого агента. Чего не умеет:
телефона не заменяет — батареи, энергии и нагрева на сервере нет.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = [HERE.parent / "phone8" / "src" / "stend" / "core", HERE / "src" / "stend9" / "core", HERE / "src" / "stend9" / "server"]
# Бюджет 65, а не круглый: при нём «наибольшие» успевают 4 вопроса по 4096
# байт (цена 15) и 5 по 2048 (цена 13) — порча «наибольшие короче» видна и по
# счёту. При 60 оба счёта — 4, и счёт её не различал бы.
PROFILE = ["--budget", "65", "--warm-ms", "20000", "--repeats", "2", "--pairs", "2x2,1x1", "--live", "честный"]

WRAP = r'''#!/usr/bin/env -S python3 -I
# порча стенда калибровки: программа модели — обёртка над настоящей
import base64, json, subprocess, sys
REAL, HOW = {real!r}, {how!r}
args = sys.argv[1:]
child = subprocess.Popen([REAL, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
threads = args[args.index("--threads") + 1] if "--threads" in args else "?"
n = 0
for line in sys.stdin.buffer:
    child.stdin.write(line); child.stdin.flush()
    out = child.stdout.readline()
    if not out:
        break
    a = base64.b64decode(json.loads(out)["a"])
    n += 1
    if HOW == "threads" and "--gguf" in args:
        a += (" " + threads).encode()
    elif HOW == "memory" and "--gguf" in args:
        a += (" #%d" % n).encode()
    elif HOW == "short" and "--gguf" in args:
        a = a[:50]
    sys.stdout.write(json.dumps({{"a": base64.b64encode(a).decode()}}) + "\n"); sys.stdout.flush()
child.stdin.close(); child.wait()
'''


def compile_classes(out: Path) -> None:
    files = [str(p) for d in SRC for p in d.rglob("*.java")]
    subprocess.run(["javac", "-encoding", "UTF-8", "-d", str(out), *files], check=True, capture_output=True)


def stand(cls: Path, out: Path, exe: str, serve: str, model: str, nosock: str, assets: Path, extra: list[str]) -> dict:
    cmd = ["java", "-cp", str(cls), "stend9.server.ServerMainK9", "--exe", exe, "--serve", serve, "--model", model,
           "--assets", str(assets), "--nosock", nosock, "--out", str(out), *PROFILE, *extra]
    r = subprocess.run(cmd, capture_output=True, timeout=7200, env={**os.environ, "LC_ALL": "C.UTF-8"})
    (out / "stderr.txt").write_bytes(r.stderr)
    return json.loads((out / "kalibr9.json").read_text(encoding="utf-8"))


def keep_until(src: Path, dst: Path, last_kept: str) -> None:
    """Копия папки итогов, где готовы только этапы до last_kept включительно."""
    shutil.copytree(src, dst)
    res = json.loads((dst / "kalibr9.json").read_text(encoding="utf-8"))
    res["этапы"] = {k: v for k, v in res["этапы"].items() if k <= last_kept}
    res.pop("вердикты", None)
    (dst / "kalibr9.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    for d in dst.glob("k6_*"):
        shutil.rmtree(d)


def razbor(folder: Path) -> dict:
    r = subprocess.run(["python3", str(HERE / "razborK9.py"), str(folder)], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(HERE.parent)})
    return json.loads(r.stdout) if r.returncode == 0 else {"сбой": r.stderr[-1500:]}


def spoil_journal(folder: Path) -> None:
    """Плата первого «вопроса модели» в последнем снимке — на единицу больше."""
    d = sorted(folder.glob("k6_*"))[0]
    snap = sorted(d.glob("snap*"))[-1] / "journal5.jsonl"
    lines = snap.read_text(encoding="utf-8").splitlines()
    for k, line in enumerate(lines):
        e = json.loads(line)
        if e.get("kind") == "действие" and e["signature"][0] == "вопрос модели":
            e["charge"] = e.get("charge", 0) + 1
            lines[k] = json.dumps(e, ensure_ascii=False)
            break
    snap.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    for k in ("--exe", "--serve", "--model", "--nosock"):
        ap.add_argument(k, required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="checkK9_"))
    cls = tmp / "cls"
    compile_classes(cls)
    assets = HERE / "assets"

    def wrap(how: str) -> str:
        p = tmp / f"serve_{how}"
        p.write_text(WRAP.format(real=a.serve, how=how), encoding="utf-8")
        p.chmod(0o755)
        return str(p)

    out: dict = {}
    clean = tmp / "чистый"
    v = stand(cls, clean, a.exe, a.serve, a.model, a.nosock, assets, [])["вердикты"]
    out["чистый"] = {"вердикты": v, "разбор": razbor(clean)}

    v = stand(cls, tmp / "потоки", a.exe, wrap("threads"), a.model, a.nosock, assets, ["--last", "К1"])["вердикты"]
    out["потоки меняют ответ"] = {"ждали": "К1 «нет»", "вердикты": v}

    keep_until(clean, tmp / "память", "К1")
    v = stand(cls, tmp / "память", a.exe, wrap("memory"), a.model, a.nosock, assets, ["--last", "К2"])["вердикты"]
    out["память между вопросами"] = {"ждали": "К2 «нет»", "вердикты": v}

    keep_until(clean, tmp / "коротко", "К1")
    v = stand(cls, tmp / "коротко", a.exe, wrap("short"), a.model, a.nosock, assets, ["--last", "К5"])["вердикты"]
    out["ответ короче предела"] = {"ждали": "К2 и К5 «нет»", "вердикты": v}

    keep_until(clean, tmp / "срок", "К3")
    v = stand(cls, tmp / "срок", a.exe, a.serve, a.model, a.nosock, assets, ["--last", "К5", "--t-scale", "0.5"])["вердикты"]
    out["срок вдвое короче"] = {"ждали": "К5 «нет» (−7)", "вердикты": v}

    spoiled = tmp / "assets_short"
    shutil.copytree(assets, spoiled)
    subprocess.run(["python3", "-c", "import sys; sys.path.insert(0, %r); from phone9.assetsK9 import asker, questions; "
                    "open(%r, 'wb').write(asker(questions()['Р2048']))" % (str(HERE.parent), str(spoiled / "k9" / "big.wasm"))],
                   check=True, env={**os.environ, "PYTHONPATH": str(HERE.parent)})
    keep_until(clean, tmp / "наибольшие", "К4")
    v = stand(cls, tmp / "наибольшие", a.exe, a.serve, a.model, a.nosock, spoiled, ["--last", "К5"])["вердикты"]
    out["наибольшие короче"] = {"ждали": "К5 «нет» (счёт)", "вердикты": v}

    jr = tmp / "журнал"
    shutil.copytree(clean, jr)
    spoil_journal(jr)
    out["журнал К6 испорчен"] = {"ждали": "нарушение в разборе", "разбор К6": razbor(jr).get("К6 законы")}

    def says(verdicts: dict, key: str) -> str:
        return next((str(x) for k, x in verdicts.items() if k.startswith(key)), "нет вердикта")

    ok = all(str(x).startswith("да") or k.startswith("К4") for k, x in out["чистый"]["вердикты"].items())
    ok &= not any(r["нарушено"] for r in out["чистый"]["разбор"]["К6 законы"].values())
    caught = {
        "потоки меняют ответ": says(out["потоки меняют ответ"]["вердикты"], "К1").startswith("нет"),
        "память между вопросами": says(out["память между вопросами"]["вердикты"], "К2").startswith("нет"),
        "ответ короче предела": says(out["ответ короче предела"]["вердикты"], "К2").startswith("нет")
        and says(out["ответ короче предела"]["вердикты"], "К5").startswith("нет"),
        "срок вдвое короче": says(out["срок вдвое короче"]["вердикты"], "К5").startswith("нет"),
        "наибольшие короче": says(out["наибольшие короче"]["вердикты"], "К5").startswith("нет"),
        "журнал К6 испорчен": any(r["нарушено"] for r in out["журнал К6 испорчен"]["разбор К6"].values()),
    }
    out["итог"] = {"чистый — «да» по всем, законы К6 целы": ok, "порча поймана": caught,
                   "всё как ждали": ok and all(caught.values())}
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
