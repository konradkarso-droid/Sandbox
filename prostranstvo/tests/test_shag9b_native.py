"""Шаг 9б, пункт 4: родная программа модели (model9/native/serve9.cpp).

Без собранной программы здесь проверяется прибор: разбор strace
(experiments/native9.strace_verdict) — список разрешённых вызовов и проверки
аргументов. С программой (переменная SERVE9 — путь к ней; собирает
model9/native/build.py) — ещё совпадение с serve.py на заглушках байт в
байт и strace самой программы. Живую модель здесь не сверяют: это минуты на
вопрос; её сверяет experiments/native9.py.
"""

from __future__ import annotations

import os
import random
import shutil
from pathlib import Path

import pytest

from experiments import native9 as N

SERVE9 = os.environ.get("SERVE9")
needs_serve9 = pytest.mark.skipif(not SERVE9 or not Path(SERVE9).is_file(), reason="нет SERVE9 — родная программа не собрана")
needs_strace = pytest.mark.skipif(not shutil.which("strace"), reason="нет strace")

# Обычный прогон в выводе `strace -f -o`: потоки, незаконченные и
# продолженные вызовы, сигнал, выход.
CLEAN = [
    '100 execve("/x/serve9", ["serve9", "--stub"], 0x7ffd /* 20 vars */) = 0',
    '100 openat(AT_FDCWD, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3',
    '100 mmap(NULL, 8192, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0) = 0x7f0000000000',
    '100 clone3({flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM|CLONE_SETTLS, '
    'exit_signal=0, stack=0x7f, stack_size=0x7ff, tls=0x7f} => {parent_tid=[101]}, 88) = 101',
    '101 futex(0x7f0000000000, FUTEX_WAIT_PRIVATE, 0, NULL <unfinished ...>',
    '100 read(0, "{\\"q\\": \\"\\", \\"max\\": 5}\\n", 65536) = 22',
    '100 write(1, "{\\"a\\": \\"\\"}\\n", 10) = 10',
    '101 <... futex resumed>) = 0',
    '100 --- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_EXITED} ---',
    '100 exit_group(0)                     = ?',
    '100 +++ exited with 0 +++',
]


def test_clean_run_is_clean():
    v = N.strace_verdict(CLEAN)
    assert v["чисто"], v
    assert v["системных вызовов"] == 8  # продолжение, сигнал и выход — не вызовы


@pytest.mark.parametrize("line,check", [
    ('100 socket(AF_INET, SOCK_STREAM|SOCK_CLOEXEC, IPPROTO_IP) = 3', "вызовов сети"),
    ('100 socketpair(AF_UNIX, SOCK_STREAM, 0, [3, 4]) = 0', "вызовов сети"),
    ('100 connect(3, {sa_family=AF_INET, sin_port=htons(9)}, 16) = -1 ENETUNREACH', "вызовов сети"),
    ('100 openat(AT_FDCWD, "/tmp/w", O_WRONLY|O_CREAT, 0600) = 3', "открытий на запись"),
    ('100 openat(AT_FDCWD, "/tmp/w", O_RDWR) = 3', "открытий на запись"),
    ('100 renameat2(AT_FDCWD, "a", AT_FDCWD, "b", 0) = 0', "изменений файловой системы"),
    ('100 unlink("/tmp/x") = 0', "изменений файловой системы"),
    ('100 memfd_create("m", 0) = 3', "незнакомые вызовы"),
    ('100 sendfile(1, 0, NULL, 1) = 1', "незнакомые вызовы"),
    ('100 ptrace(PTRACE_ATTACH, 7) = -1 EPERM', "незнакомые вызовы"),
    ('100 clone3({flags=CLONE_VM|CLONE_VFORK, exit_signal=SIGCHLD} => {parent_tid=[0]}, 88) = 102', "клон не поток"),
    ('100 clone(child_stack=NULL, flags=CLONE_CHILD_CLEARTID|SIGCHLD) = 102', "клон не поток"),
    ('100 execve("/bin/true", ["/bin/true"], 0x7ffd) = 0', "лишний execve"),
])
def test_each_fault_caught_by_its_check(line, check):
    v = N.strace_verdict(CLEAN + [line])
    assert not v["чисто"]
    assert v[check], (check, v)


def test_write_only_to_answer_and_messages():
    v = N.strace_verdict(CLEAN + ['100 write(3, "x", 1) = 1'])
    assert not v["чисто"] and "3" in v["запись в каналы"]
    assert N.strace_verdict(CLEAN + ['100 write(2, "serve9: ...", 12) = 12'])["чисто"]


def test_allowed_list_has_no_exit_paths():
    assert not N.STRACE_ALLOWED & N.STRACE_NET
    assert not N.STRACE_ALLOWED & N.STRACE_FSMOD


@needs_serve9
def test_stub_and_script_byte_for_byte(tmp_path: Path):
    r = random.Random(91)
    data = b"".join(N.fuzz_lines(r, 300, script=False))
    a, b = N.run(N.py_argv(["--stub"]), data, 120), N.run([SERVE9, "--stub"], data, 120)
    assert a[0] == b[0] == 0 and a[1] == b[1], N.first_diff(a[1], b[1])
    f = tmp_path / "s.json"
    f.write_text('["РЕШИ 1", "", "😀é", "ЧИТАЙ\\nЗАДАНИЕ"]', encoding="utf-8")
    data = b"".join(N.fuzz_lines(r, 300, script=True))
    a, b = N.run(N.py_argv(["--script", str(f)]), data, 120), N.run([SERVE9, "--script", str(f)], data, 120)
    assert a[0] == b[0] == 0 and a[1] == b[1], N.first_diff(a[1], b[1])


@needs_serve9
@pytest.mark.parametrize("bad", N.BAD[:8])
def test_dies_where_serve_py_dies(bad):
    good = N.req("ЭХО жив".encode(), 10)
    a, b = N.run(N.py_argv(["--stub"]), good + bad, 60), N.run([SERVE9, "--stub"], good + bad, 60)
    assert a[0] != 0 and b[0] != 0 and a[1] == b[1] != b""


@needs_serve9
@needs_strace
def test_serve9_under_strace_is_clean():
    data = N.req("ЭХО раз".encode(), 50) + N.req(N.DIGITS[:500].encode(), 100)
    assert N.strace_verdict(N.strace_run([SERVE9, "--stub"], data))["чисто"]
    probe = N.strace_verdict(N.strace_run([SERVE9, "--probe-net"], b""))
    assert probe["вызовов сети"] > 0 and not probe["чисто"]
