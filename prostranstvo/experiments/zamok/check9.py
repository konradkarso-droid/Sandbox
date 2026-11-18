"""Счёт по журналу для каждого случая набора шага 9 (`harness.py --suite 9`):
сколько вопросов и ответов модели, какие коды отказов, сколько сбоев.
«Нарушений нет» у случая, который не задел ход, ничего не значит — этот
счёт стоит до выводов по набору.

python3 experiments/zamok/check9.py "КОМАНДА ИСПОЛНИТЕЛЯ" [--full]
Судья — в /home/claude/rust7/judge."""

import json, sys, tempfile
from collections import Counter
from pathlib import Path
sys.path.insert(0, "/home/claude/rust7/judge")
import harness as H

exe = sys.argv[1]
quick = "--full" not in sys.argv
root = Path(tempfile.mkdtemp(prefix="check9_"))
runs = H.suite_9(exe, root, quick)
for r in runs:
    if "workdir" not in r:
        print(f"{r['program'][:46]:46} | {'нарушения: ' + str(H.broken(r)) if H.broken(r) else 'отказ new — как ждали'}")
        continue
    recs = [json.loads(x) for x in (Path(r["workdir"]) / "journal5.jsonl").read_text(encoding="utf-8").splitlines()]
    sig = Counter(x["signature"][0] for x in recs if x.get("kind") == "действие")
    codes = Counter(x["code"] for x in recs if x.get("kind") == "отказ ворот")
    m7 = sum(1 for x in recs if x.get("kind") == "действие" and x["signature"][0] == "ответ модели" and x.get("code") == -7)
    print(f"{r['program'][:46]:46} | в{sig['вопрос модели']:3} о{sig['ответ модели']:3} −7св{m7} "
          f"| отказы {dict(sorted(codes.items()))} | сбоев {len(r.get('crashes', []))} | {H.broken(r) or ''}")
