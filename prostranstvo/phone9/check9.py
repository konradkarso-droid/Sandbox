"""Проверка малого стенда шага 9б на сервере — до телефона (спецификация
шага 9б, §16): тот же ведущий Stand9, чистый прогон и порчи.

Каждая порча обязана провалить свой вердикт, а чистый прогон — дать «да»
по всем; иначе вердикт стенда ничего не проверяет (урок 41 хендовера 18.11):

  без подмены socket()  — сеть на сервере есть: В3 «нет»;
  обёртка-внук          — программа модели запускает настоящую дочерним
                          процессом и сама не exec: В1 «нет» (лишние
                          потомки, другая программа), В5 «нет» (внук
                          переживает исполнителя);
  случайные ответы      — программа модели отвечает каждый раз иначе:
                          В7 «нет», при этом В2 «да» (ответы есть).

  LC_ALL=C.UTF-8 python3 phone9/check9.py --exe ИСПОЛНИТЕЛЬ --serve SERVE9 --model GGUF --nosock БИБЛИОТЕКА

Чего не умеет: телефона не заменяет — отказ сети здесь изображён подменой,
а не системой, и Android может убивать детей приложения иначе, чем Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = [HERE.parent / "phone8" / "src" / "stend" / "core", HERE / "src" / "stend9" / "core", HERE / "src" / "stend9" / "server"]

GRANDCHILD = '#!/bin/sh\n# порча: программа модели — внук исполнителя\n{serve} "$@"\n'
RANDOM = r'''#!/usr/bin/env -S python3 -I
# порча: ответ каждый раз другой
import base64, json, os, sys
for line in sys.stdin.buffer:
    req = json.loads(line)
    q = base64.b64decode(req["q"]).decode("utf-8", "replace")
    a = "жив" if q == "ЭХО жив" else os.urandom(4).hex()
    sys.stdout.write(json.dumps({"a": base64.b64encode(a.encode()).decode()}) + "\n")
    sys.stdout.flush()
'''


def compile_classes(out: Path) -> None:
    files = [str(p) for d in SRC for p in d.rglob("*.java")]
    subprocess.run(["javac", "-encoding", "UTF-8", "-d", str(out), *files], check=True, capture_output=True)


def stand(cls: Path, work: Path, exe: str, serve: str, model: str, nosock: str | None) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    out = work / "out.json"
    cmd = ["java", "-cp", str(cls), "stend9.server.ServerMain9", "--exe", exe, "--serve", serve, "--model", model,
           "--assets", str(HERE / "assets"), "--out", str(out)]
    if nosock:
        cmd += ["--nosock", nosock]
    subprocess.run(cmd, capture_output=True, timeout=3600, env={**os.environ, "LC_ALL": "C.UTF-8"})
    return json.loads(out.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    for k in ("--exe", "--serve", "--model", "--nosock"):
        ap.add_argument(k, required=True)
    ap.add_argument("--out", default=str(HERE.parent / "experiments" / "results" / "shag9b_stend_server.json"))
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="stend9_"))
    cls = root / "cls"
    compile_classes(cls)
    wrap, rnd = root / "grandchild.sh", root / "random.py"
    wrap.write_text(GRANDCHILD.format(serve=a.serve))
    rnd.write_text(RANDOM)
    wrap.chmod(0o755)
    rnd.chmod(0o755)
    # (имя, программа модели, подмена socket(), какие вердикты обязаны начаться с «да» / не начинаться)
    runs = [
        ("чистый", a.serve, a.nosock, {"В1", "В2", "В3", "В4", "В5", "В6", "В7"}, set()),
        ("без подмены socket()", a.serve, None, {"В6"}, {"В3"}),
        ("обёртка-внук", str(wrap), a.nosock, {"В2", "В7"}, {"В1", "В5"}),
        ("случайные ответы", str(rnd), a.nosock, {"В2"}, {"В7"}),
    ]
    res, ok_all = {}, True
    for name, serve, nosock, yes, no in runs:
        r = stand(cls, root / name.replace(" ", "_"), a.exe, serve, a.model, nosock)
        v = r.get("вердикты", {})
        got = {k.split()[0]: str(val) for k, val in v.items() if k.startswith("В")}
        bad = [k for k in yes if not got.get(k, "").startswith("да")] + [k for k in no if got.get(k, "").startswith("да")]
        ok_all &= not bad
        res[name] = {"вердикты": v, "ждали «да»": sorted(yes), "ждали не «да»": sorted(no), "не так": sorted(bad),
                     "смерть": r.get("5. смерть с исполнителем"),
                     "память": (r.get("4. главный прогон") or {}).get("память")}
        print(f"{name}: {'как ждали' if not bad else 'НЕ ТАК: ' + ', '.join(sorted(bad))}", flush=True)
    res["итог"] = "каждая порча провалила свой вердикт, чистый прогон — «да» по всем" if ok_all else "есть расхождения"
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(res["итог"])
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
