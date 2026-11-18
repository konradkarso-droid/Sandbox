"""Прибор шага 9 (спецификация шага 9, §8.3): чем исполнитель и процессы
модели касаются мира, по strace.

Запускает короткий сеанс в отдельном процессе исполнителя (Driver, с
прибором аудита) с процессами модели и пишет сводку: какие системные вызовы
сети и запуска процессов сделал каждый процесс. Ожидается:

- исполнитель: сетевых вызовов наружу нет; запуски процессов — только
  процессы модели, до включения аудита;
- процессы модели: сетевых вызовов наружу нет. Под `unshare -rn` у них и
  сети нет — это проверяет `serve.py --probe-net` (тесты шага 9).

«Наружу» — сокеты семейств AF_INET и AF_INET6. Сокеты AF_UNIX — каналы
между процессами Python (multiprocessing) и в счёт не идут.

Чего прибор не умеет: он видит только системные вызовы этого сеанса и
этих программ; живую модель гоняет только с --gguf.

Запуск: PYTHONPATH=. python3 experiments/strace9.py [--gguf ФАЙЛ]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RESULTS = HERE / "experiments" / "results"

SESSION = r'''
import sys
sys.path.insert(0, {root!r})
from pathlib import Path
from experiments import programs5b as p5b
from sandbox.process5 import Driver


def main():
    cfg = {{"graph": "M3т", "levels": 4, "memory": 4, "budget": 625, "price": 10, "fail_price": 10,
            "work_price": 10, "gate_price": 5, "paid_attempts": 1, "model": 1}}
    d = Driver(Path({tmp!r}), frozenset(), kind="5б", model={{"mode": {mode!r}, "timeout": 300.0}})
    print(d.call("new", cfg, None)[0])
    src = p5b.module([("модель", "ЭХО раз"), ("модель", "РАЗРЕШАЮ"), ("имя", "модель"), ("модель", "ЭХО два")])
    for i in (1, 2):
        print(d.call("load", i, src))
        print(d.call("run", i))
    print("аудит:", d.call("audit"))
    d.close()


# Процесс исполнителя запускается способом «spawn», и он заново читает этот
# файл: без проверки имени сеанс пошёл бы по второму разу внутри исполнителя.
if __name__ == "__main__":
    main()
'''

NET = re.compile(r"^(\d+)\s+(?:\d+:\d+:\d+\.\d+ )?(socket|connect|sendto|sendmsg|bind)\((.*)$")
EXEC = re.compile(r'^(\d+)\s+(?:\d+:\d+:\d+\.\d+ )?execve\("([^"]+)", \[(.*?)\]')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf")
    a = ap.parse_args()
    if not shutil.which("strace"):
        sys.exit("нет strace")
    mode = ["--gguf", a.gguf] if a.gguf else ["--stub"]
    tmp = tempfile.mkdtemp(prefix="strace9_")
    script = Path(tmp) / "session.py"
    script.write_text(SESSION.format(root=str(HERE), tmp=str(Path(tmp) / "run"), mode=mode), encoding="utf-8")
    log = Path(tmp) / "strace.txt"
    r = subprocess.run(["strace", "-f", "-qq", "-s", "256", "-e", "trace=network,execve", "-o", str(log), sys.executable, "-I",
                        str(script)], capture_output=True, text=True, timeout=3600)
    procs: dict[str, str] = {}
    net = defaultdict(list)
    for line in log.read_text(errors="replace").splitlines():
        m = EXEC.match(line)
        if m:
            procs[m.group(1)] = " ".join(x.strip('"') for x in m.group(3).split(", ")[:4])
            continue
        m = NET.match(line)
        if m and "AF_UNIX" not in m.group(3) and "AF_NETLINK" not in m.group(3):
            net[m.group(1)].append(f"{m.group(2)}({m.group(3)[:80]}")
    models = [p for p, c in procs.items() if "serve.py" in c]
    L = ["# Шаг 9: strace сеанса с моделью", "",
         f"Режим модели: `{' '.join(mode)}`. Вывод сеанса: `{' | '.join(r.stdout.split(chr(10))[:6])}`.", "",
         "| Процесс | Команда | Вызовы сети наружу |", "|---|---|---|"]
    for p, c in procs.items():
        role = "модель" if p in models else "исполнитель или ведущий"
        L.append(f"| {p} ({role}) | `{c[:70]}` | {len(net.get(p, []))} |")
    stray = {p: v for p, v in net.items() if p not in procs}
    for p, v in stray.items():
        L.append(f"| {p} (поток или без execve) | — | {len(v)} |")
    total = sum(len(v) for v in net.values())
    L += ["", f"**Процессов модели: {len(models)}** (ожидается 2 — по одному на контур). "
              f"**Вызовов сети наружу всего: {total}** (ожидается 0)."]
    if total:
        L += ["", "Первые вызовы:", ""] + [f"- {p}: {x}" for p, v in net.items() for x in v[:3]]
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / ("shag9_strace_live.md" if a.gguf else "shag9_strace.md")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
