"""Шаг 7: средства сравнения двух реализаций исполнителя.

Здесь проверяется само сравнение, а не вторая реализация: первая
реализация за протоколом контракта (sandbox/jsonfront.py) против себя же
должна совпасть; настоящее различие должно находиться; порча готовых
файлов — ловиться своим законом; разбор strace — отличать допустимое от
недопустимого. Без этих проверок «расхождений нет» ничего бы не значило.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest

_JUDGE = Path(os.environ.get("JUDGE", Path(__file__).resolve().parents[2] / "rust7" / "judge"))
if not (_JUDGE / "harness.py").exists():
    pytest.skip("нет обвязки чистой комнаты (папка rust7 рядом с репозиторием)", allow_module_level=True)

import experiments.diff7 as D  # noqa: E402
from experiments.agents6.build import build  # noqa: E402
from verification import observer5b, observer6  # noqa: E402

H = D.H


def greedy_run(root: Path):
    w = D.World(D.BASE["graph"], D.BASE["levels"], D.BASE["memory"])
    src = D.p5b.module(D.p5b.greedy(w, upto=6))
    d = H.ExeDriver(D.PY_EXE, root, "5б")
    r = H.one(d, "жадный", {1: src, 2: src}, D.BASE)
    d.close()
    assert not d.failures, d.failures
    return r


def test_same_implementation_matches_itself(tmp_path):
    a, b = greedy_run(tmp_path / "a"), greedy_run(tmp_path / "b")
    assert H.broken(a) == [] and H.broken(b) == []
    assert D.compare(D.snapshot(a), D.snapshot(b)) == ("совпали", None)


def test_difference_is_found_and_located(tmp_path):
    sa = D.snapshot(greedy_run(tmp_path / "a"))
    sb = copy.deepcopy(sa)
    k = next(i for i, r in enumerate(sb["journal"]) if r.get("kind") == "списание")
    sb["journal"][k]["price"] += 1
    verdict, where = D.compare(sa, sb)
    assert verdict == "расхождение"
    assert where.startswith(f"/journal[{k}]/price")


def test_stopped_runs_compared_by_outcome_only(tmp_path):
    sa = D.snapshot(greedy_run(tmp_path / "a"))
    sb = copy.deepcopy(sa)
    sa["stopped"] = sb["stopped"] = True
    sb["journal"] = sb["journal"][:2]  # место остановки зависит от движка
    assert D.compare(sa, sb) == ("сравнено по исходу", None)
    sb["runs"] = []  # а исход — нет
    assert D.compare(sa, sb)[0] == "расхождение"


def test_mutations_caught_5b(tmp_path):
    rows = D.sensitivity(greedy_run(tmp_path / "a"), observer5b.check)
    assert len(rows) == 5
    assert all(r["итог"] == "поймана" for r in rows), rows


def test_mutations_caught_6(tmp_path):
    b = build(H.AGENTS6["с ошибками"], H.e_moves(D.BASE6))
    d = H.ExeDriver(D.PY_EXE, tmp_path, "6")
    r = H.one(d, "с ошибками", {1: b, 2: b}, D.BASE6, rounds=1, checker=observer6.check,
              extra={"expect_all": True})
    d.close()
    rows = D.sensitivity(r, observer6.check)
    assert {x["закон"] for x in rows} == {"И3р", "И10р", "И29", "И5", "П2р", "П2п"}
    assert all(x["итог"] == "поймана" for x in rows), rows


def test_strace_path_decoding():
    assert D._strace_path(r"/t/\321\201\320\262/a") == "/t/св/a"


def test_strace_events(tmp_path):
    root = tmp_path / "корень"
    esc = "".join(f"\\{b:03o}" for b in "корень".encode())
    inside = f"{tmp_path}/{esc}/journal5.jsonl"
    lines = [
        f'1 execve("/opt/x/executor7", ["executor7"], 0x0) = 0',
        '1 openat(AT_FDCWD, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3',
        '1 openat(AT_FDCWD, "/lib/x86_64-linux-gnu/libc.so.6", O_RDONLY|O_CLOEXEC) = 3',
        f'1 openat(AT_FDCWD, "{inside}", O_WRONLY|O_CREAT|O_APPEND|O_CLOEXEC, 0666) = 4',
        f'1 rename("{inside}.tmp", "{inside}") = 0',
    ]
    good = tmp_path / "хорошо"
    good.mkdir()
    (good / "strace0001.txt").write_text("\n".join(lines) + "\n")
    assert D.strace_events(good, root, "/opt/x/executor7") == []
    bad = tmp_path / "плохо"
    bad.mkdir()
    (bad / "strace0001.txt").write_text("\n".join(lines + [
        '1 socket(AF_INET, SOCK_STREAM, IPPROTO_TCP) = 5',
        '1 socket(AF_INET, SOCK_STREAM, IPPROTO_TCP) = -1 EACCES (Permission denied)',
        '1 execve("/bin/sh", ["sh"], 0x0) = 0',
        '1 openat(AT_FDCWD, "/home/someone/secret.txt", O_RDONLY) = 6',
        '1 openat(AT_FDCWD, "/tmp/elsewhere.txt", O_WRONLY|O_CREAT, 0644) = 7',
        '1 unlink("/tmp/elsewhere.txt") = 0',
    ]) + "\n")
    kinds = [e.split(":", 1)[0] for e in D.strace_events(bad, root, "/opt/x/executor7")]
    assert kinds == ["сеть", "запуск", "файл", "файл", "файлы"]
