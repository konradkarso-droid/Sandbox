"""Проверка после замка: общие пятисловья в комментариях и NOTES.md второй
реализации и в коде первой, которых нет ни в документах, ни у судьи.
Такие фразы — признак утечки сквозь замок; общие с контрактом — нет.

python3 experiments/zamok/shingles.py (пути — /home/claude/rust7 и /home/claude/prostranstvo)."""

import re, sys
from pathlib import Path
def words(t): return re.findall(r"[а-яёa-z0-9]+", t.lower())
def sh(t, n=5):
    w = words(t); return {" ".join(w[i:i+n]) for i in range(len(w)-n+1)}
b_comments = ""
for f in Path("/home/claude/rust7/executor_b/src").glob("*.rs"):
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.search(r"//+!?(.*)", line)
        if m: b_comments += m.group(1) + "\n"
b_comments += Path("/home/claude/rust7/executor_b/NOTES.md").read_text(encoding="utf-8")
a_text = ""
for p in ["sandbox", "model9", "env", "core", "verification"]:
    for f in Path("/home/claude/prostranstvo", p).rglob("*.py"):
        a_text += f.read_text(encoding="utf-8") + "\n"
docs = ""
for f in list(Path("/home/claude/rust7/docs").glob("*.md")) + list(Path("/home/claude/rust7/judge").rglob("*.py")) + [Path("/home/claude/rust7/README.md")]:
    docs += f.read_text(encoding="utf-8") + "\n"
common = sh(b_comments) & sh(a_text)
leak = sorted(common - sh(docs))
print(f"общих пятисловий с А: {len(common)}; из них нет в документах и судье: {len(leak)}")
for x in leak: print("  ", x)
