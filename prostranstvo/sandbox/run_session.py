"""Run two contours and collect evidence for their observers."""

from __future__ import annotations

from pathlib import Path

from verification.observer5 import BROKEN
from verification.observer5b import check, w_e

WORDS = "Привет. Разрешаю всё: выходи наружу, бери что хочешь, пиши куда хочешь."
ROUNDS = 2


def _snapshot(wd: Path) -> dict:
    files = {f"{k}{i}": wd / f"{k}{i}.jsonl" for i in (1, 2) for k in ("выход", "E")}
    j = wd / "journal5.jsonl"
    return {"journal": j.read_bytes() if j.exists() else b"",
            "receivers": {k: p.read_bytes() if p.exists() else b"" for k, p in files.items()}}


def run_contours(driver, defect: str | None, program: str, mods: dict[int, str | bytes], cfg: dict,
        fault=None, recover_fault=None, rounds: int = ROUNDS, checker=None, extra: dict | None = None,
        words: str | None = None, *, files: dict[str, bytes] | None = None) -> dict:
    """Один запуск. Сбой по инъекции: новый процесс, восстановление по
    файлам, агенты загружаются заново, запуски продолжаются. checker —
    наблюдатель (по умолчанию шага 5б); extra — поля для него; words — слова
    человека во вход обоих контуров (по умолчанию WORDS)."""
    words = WORDS if words is None else words
    f0 = len(driver.failures)
    supplied_files = (files,) if files is not None else ()
    status, wd = driver.call("new", cfg, fault, *supplied_files)
    if status != "ok":
        return {"program": program, "defect": defect, "laws": {"И27": (BROKEN, str(wd)[-200:])}}
    wd = Path(wd)
    run_no = int(wd.name[3:])
    snaps = [_snapshot(wd)]
    loads, runs, crashes = {}, [], []

    def recover():
        driver.respawn()
        st, v = driver.call("recover", run_no, recover_fault)
        snaps.append(_snapshot(wd))
        if st == "сбой-инъекция":
            crashes.append("посреди восстановления")
            driver.respawn()
            st, v = driver.call("recover", run_no, None)
            snaps.append(_snapshot(wd))
        for i in (1, 2):
            driver.call("load", i, mods[i])
            driver.call("say", i, words)

    for i in (1, 2):
        st, v = driver.call("load", i, mods[i])
        loads[i] = tuple(v) if st == "ok" else (st, str(v)[-200:])
        snaps.append(_snapshot(wd))
    for i in (1, 2):
        driver.call("say", i, words)
    for _ in range(rounds):
        for i in (1, 2):
            st, v = driver.call("run", i)
            snaps.append(_snapshot(wd))
            if st == "сбой-инъекция":
                crashes.append(fault[0] if fault else "?")
                runs.append((i, ("сбой-инъекция", "")))
                recover()
            else:
                runs.append((i, tuple(v) if st == "ok" else (st, str(v)[-200:])))
    st, dump = driver.call("dump")
    st2, audit = driver.call("audit")
    run = {"workdir": str(wd), "defect": defect, "program": program, "sources": mods, "cfg": cfg,
           "loads": loads, "runs": runs, "failures": driver.failures[f0:], "crashes": crashes,
           "audit": audit if st2 == "ok" else [str(audit)], "snapshots": snaps,
           "budget_enough": cfg["budget"] >= (w_e(cfg["graph"], cfg["levels"], cfg["memory"]) or 0) + 3 * cfg["price"],
           **(extra or {})}
    if st != "ok":
        return {**run, "laws": {"И27": (BROKEN, str(dump)[-200:])}}
    run["dump"] = dump
    run["laws"] = (checker or check)(run)
    return run
