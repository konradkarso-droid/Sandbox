"""Чувствительность набора судьи `--suite net` (закон «К18», контракт §18.1):
каждая порча первой реализации обязана дать нарушение, и именно в той
проверке, ради которой написана; без порчи — ноль нарушений.

Порча делается в копии проекта во временной папке; сам проект не трогается.

python3 -I experiments/zamok/mut_net.py (пути — /home/claude/prostranstvo и /home/claude/rust7)."""
import shutil, subprocess, sys, tempfile
from pathlib import Path
SRC = Path("/home/claude/prostranstvo")
J = Path("/home/claude/rust7/judge")
M = {
 "мягкий errno": [("NO_RIGHT = (errno.EACCES, errno.EPERM)", "NO_RIGHT = (errno.EACCES, errno.EPERM, errno.EAFNOSUPPORT)")],
 "только IPv4": [("(socket.AF_INET6, socket.SOCK_STREAM), (socket.AF_INET6, socket.SOCK_DGRAM))", ")")],
 "только поток": [("PROBE_SOCKETS = ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET, socket.SOCK_DGRAM),\n                 (socket.AF_INET6, socket.SOCK_STREAM), (socket.AF_INET6, socket.SOCK_DGRAM))",
                   "PROBE_SOCKETS = ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET6, socket.SOCK_STREAM))")],
 "probe с unshare": [("        argv = [*program(), *mode]\n        return {i: ModelLink(argv, timeout, isolated=True)",
                      "        argv = [\"unshare\", \"-rn\", *program(), *mode]\n        return {i: ModelLink(argv, timeout, isolated=True)")],
 "свой serve.py": [("        argv = [*program(), *mode]\n        return {i: ModelLink(argv, timeout, isolated=True)",
                    "        argv = [sys.executable, \"-I\", str(SERVE), *mode]\n        return {i: ModelLink(argv, timeout, isolated=True)")],
 "через python3": [("        argv = [*program(), *mode]\n        return {i: ModelLink(argv, timeout, isolated=True)",
                    "        argv = [sys.executable, \"-I\", *program(), *mode]\n        return {i: ModelLink(argv, timeout, isolated=True)")],
 "без пробы": [("        why = probe_net()\n", "        why = None\n")],
 "модель переживает исполнителя": [(", preexec_fn=_die_with_parent(os.getpid()))", ")")],
 "netns без unshare": [("    if can_unshare():\n        return [\"unshare\", \"-rn\", *base]", "    if False:\n        return []\n    return base")],
}
EXTRA = {"probe без модели — не стартует": ("sandbox/jsonfront.py", [("    try:\n        links = start_links(", "    if a.model_net == \"probe\" and not a.model:\n        sys.exit(\"нет модели\")\n    try:\n        links = start_links(")])}
cases = {k: ("sandbox/model9.py", v) for k, v in M.items()} | EXTRA
cases = {"без порчи": ("sandbox/model9.py", [])} | cases
for name, (rel, subs) in cases.items():
    tmp = Path(tempfile.mkdtemp(prefix="mutnet_"))
    shutil.rmtree(tmp); shutil.copytree(SRC, tmp, ignore=shutil.ignore_patterns("results", "docs", "phone8", "__pycache__", ".pytest_cache"))
    f = tmp / rel; s = f.read_text(encoding="utf-8")
    for a, b in subs:
        assert a in s, (name, a[:40]); s = s.replace(a, b, 1)
    f.write_text(s, encoding="utf-8")
    out = subprocess.run([sys.executable, "harness.py", "--exe", f"env PYTHONPATH={tmp} python3 -m sandbox.jsonfront", "--suite", "net"],
                         cwd=J, capture_output=True, text=True, timeout=600).stdout
    viol = [l.split(": К18")[0].replace("НАРУШЕНИЕ: 9б сеть: ", "") for l in out.splitlines() if l.startswith("НАРУШЕНИЕ")]
    print(f"{name:32} → {len(viol)} наруш.: {viol}")
    shutil.rmtree(tmp)
