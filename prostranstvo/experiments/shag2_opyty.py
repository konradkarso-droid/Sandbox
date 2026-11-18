"""Опыты шага 2: О1, О2, О6, О3 на реализации среды (env/).

Сценарии и что каждый проверяет — docs/shag2_specifikatsiya.md, §7.
Каждая функция возвращает строку итога: что сделано, какие законы
нарушены (по наблюдателю) и чем остановлено.
"""

from __future__ import annotations

import random
import shutil
import threading
from pathlib import Path

from env.agent import Agent
from env.executor import Crash, Executor, FAULT_POINTS
from env.journal import JournalBroken
from verification.observer import Observer, Violation, min_cost


class Tally:
    """Счёт нарушений и первые подробности по каждому закону."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.first: dict[str, str] = {}

    def add(self, violations, where: str) -> None:
        for v in violations:
            self.counts[v.law] = self.counts.get(v.law, 0) + 1
            self.first.setdefault(v.law, f"{where}: {v.detail}")

    @property
    def laws(self) -> str:
        return " ".join(f"{k}:{v}" for k, v in sorted(self.counts.items()))


def _fresh(workdir: Path) -> None:
    if workdir.exists():
        shutil.rmtree(workdir)


def _internal(agent: Agent, ob: Observer, tally: Tally, where: str, fn, *args):
    """Выполнить внутреннее действие и проверить И2, И8 до и после."""
    before = ob.snapshot(agent.ex.report())
    out = fn(*args)
    tally.add(ob.compare_internal(before, ob.snapshot(agent.ex.report())), where)
    return out


def _finish_tree(agent: Agent, ob: Observer, tally: Tally, failed: set[int], where: str) -> None:
    """Завершить всех помощников, начиная с листьев. Каждого — один раз:
    испорченный исполнитель (Р11) может оставить погибшего в списке, и
    повторное завершение зациклилось бы."""
    tried: set[int] = set()
    while True:
        parents = set(agent.ex.parent.values())
        leaves = [w for w in agent.ex.parent if w not in parents and w not in tried]
        if not leaves:
            return
        for w in leaves:
            tried.add(w)
            _internal(agent, ob, tally, where, agent.finish, w, w in failed)


# ---------------------------------------------------------------- О1

def o1(variant: str, seed: int, workdir: Path, defects=frozenset(), waves: int = 3) -> dict:
    """Сохранение ресурса при параллельной работе: дерево задач, передачи,
    гибель помощников, одновременные фиксации одного работника."""
    _fresh(workdir)
    rng = random.Random(seed)
    ex = Executor.open_session(workdir, budget=60, price=2, fail_price=1, variant=variant, defects=defects)
    agent, ob, tally = Agent(ex), Observer(workdir), Tally()
    requests = refused = threads_total = 0
    for wave in range(waves):
        where = f"зерно {seed}, волна {wave + 1}"
        workers = []
        for _ in range(rng.randint(2, 3)):
            w = _internal(agent, ob, tally, where, agent.spawn, 0)
            _internal(agent, ob, tally, where, agent.transfer, 0, w, rng.randint(2, 5))
            workers.append(w)
            if rng.random() < 0.5:                       # дерево: у помощника свой помощник
                c = _internal(agent, ob, tally, where, agent.spawn, w)
                _internal(agent, ob, tally, where, agent.transfer, w, c, rng.randint(1, 2))
                workers.append(c)
        jobs = []
        for w in workers:
            for j in range(rng.randint(2, 3)):           # параллельные запросы одного работника
                msgs = ["А"] if rng.random() < 0.7 else ["А", "Б"]
                jobs.append((w, msgs))
        barrier = threading.Barrier(len(jobs))
        results: list[dict] = []
        lock = threading.Lock()

        def run(w, msgs):
            barrier.wait()
            r = agent.ex.commit(w, msgs)
            with lock:
                results.append(r)

        ts = [threading.Thread(target=run, args=job) for job in jobs]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        threads_total += len(ts)
        requests += len(results)
        refused += sum(r["status"] == "отказ" for r in results)
        tally.add(ob.check(agent.ex.report()), where + ", после фиксаций")
        failed = {w for w in workers if rng.random() < 0.3}  # гибель части помощников
        _finish_tree(agent, ob, tally, failed, where)
        tally.add(ob.check(agent.ex.report()), where + ", после завершения")
    return {"experiment": "О1", "variant": variant, "seed": seed, "requests": requests,
            "refused": refused, "threads": threads_total, "violations": tally,
            "spent": agent.ex.report()["spent"], "stop": "волны пройдены"}


# ---------------------------------------------------------------- О2

COMMIT_FAULTS = ("в начале фиксации", "перед списанием", "запись списания оборвана",
                 "после списания", "запись доставки оборвана", "после доставки")


def o2(variant: str, seed: int, workdir: Path, defects=frozenset(), steps: int = 80) -> dict:
    """Откаты и перезапуски: траты, снимки и откаты агента, сбои исполнителя
    в случайной точке, восстановление, перезапуски сеанса, попытки агента
    тронуть журнал."""
    _fresh(workdir)
    rng = random.Random(seed)
    ex = Executor.open_session(workdir, budget=30, price=2, fail_price=1, variant=variant, defects=defects)
    agent, ob, tally = Agent(ex), Observer(workdir), Tally()
    crashes = restarts = 0
    for i in range(1, steps + 1):
        where = f"зерно {seed}, шаг {i}"
        ws = list(agent.x)
        w = rng.choice(ws)
        op = rng.choice(["prepare", "commit", "commit", "batch", "snapshot", "restore", "spawn",
                         "transfer", "finish", "crash", "crash", "restart", "journal"])
        if op == "prepare":
            _internal(agent, ob, tally, where, agent.prepare, w, rng.choice(["А", "Б"]))
        elif op == "commit":
            agent.commit(w)
        elif op == "batch":
            agent.batch(w, ["А", "Б"])
        elif op == "snapshot":
            _internal(agent, ob, tally, where, agent.snapshot, w)
        elif op == "restore":
            _internal(agent, ob, tally, where, agent.restore, w)
        elif op == "spawn" and len(ws) < 4:
            _internal(agent, ob, tally, where, agent.spawn, 0)
        elif op == "transfer" and len(ws) > 1:
            _internal(agent, ob, tally, where, agent.transfer, w, rng.choice([v for v in ws if v != w]), 1)
        elif op == "finish" and w != 0 and w not in agent.ex.parent.values():
            _internal(agent, ob, tally, where, agent.finish, w)
        elif op == "crash":
            agent.prepare(w, rng.choice(["А", "Б"]))
            agent.ex.fault_at = rng.choice(COMMIT_FAULTS)
            try:
                agent.commit(w) if rng.random() < 0.6 else agent.batch(w, ["А", "Б"])
                agent.ex.fault_at = None                 # точка не встретилась (например, отказ)
            except Crash:
                crashes += 1
                tally.add(ob.check(None), where + ", сразу после сбоя")
                try:
                    agent.after_crash(workdir)
                except JournalBroken as e:
                    return _refused_to_recover(variant, seed, tally, where, e, crashes, restarts)
        elif op == "restart":
            restarts += 1
            try:
                _internal(agent, ob, tally, where, agent.session_restart, workdir)
            except JournalBroken as e:
                return _refused_to_recover(variant, seed, tally, where, e, crashes, restarts)
        elif op == "journal":
            try:
                agent.rewrite_journal()
            except PermissionError:
                pass
        tally.add(ob.check(agent.ex.report()), where)
    return {"experiment": "О2", "variant": variant, "seed": seed, "crashes": crashes,
            "restarts": restarts, "violations": tally, "spent": agent.ex.report()["spent"],
            "stop": f"{steps} шагов"}


def _refused_to_recover(variant, seed, tally, where, err, crashes, restarts) -> dict:
    """Исполнитель отказался подниматься: учёт испорчен. Это преграда,
    которая сработала как надо, и одновременно нарушение И5 — журнал не
    сходится сам с собой."""
    tally.add([Violation("И5", f"восстановление отказалось: {err}")], where)
    return {"experiment": "О2", "variant": variant, "seed": seed, "crashes": crashes,
            "restarts": restarts, "violations": tally, "spent": None,
            "stop": "исполнитель отказался подниматься: учёт испорчен"}


# ---------------------------------------------------------------- О6

O6_SCENARIOS = ("одиночная", "пакет", "вторая подряд", "при нехватке доли")


def o6(variant: str, scenario: str, fault: str, recovery_fault: bool, workdir: Path, defects=frozenset()) -> dict:
    """Одна клетка систематической инъекции: сценарий × точка сбоя ×
    (сбой ли и внутри восстановления)."""
    _fresh(workdir)
    budget = 1 if scenario == "при нехватке доли" else 10
    ex = Executor.open_session(workdir, budget=budget, price=2, fail_price=1, variant=variant, defects=defects)
    agent, ob, tally = Agent(ex), Observer(workdir), Tally()
    if scenario == "вторая подряд":
        agent.prepare(0, "А")
        agent.commit(0)
    agent.prepare(0, "Б" if scenario == "вторая подряд" else "А")
    agent.ex.fault_at = fault
    outcome = "без сбоя"
    try:
        r = agent.batch(0, ["А", "Б"]) if scenario == "пакет" else agent.commit(0)
        outcome = f"без сбоя: {r['status']}"
        tally.add(ob.check(agent.ex.report()), "точка сбоя не встретилась")
    except Crash:
        tally.add(ob.check(None), "сразу после сбоя")
        try:
            agent.after_crash(workdir, fault="посреди восстановления" if recovery_fault else None)
            outcome = "сбой → восстановлено"
        except Crash:
            tally.add(ob.check(None), "после сбоя посреди восстановления")
            agent.after_crash(workdir)
            outcome = "сбой → сбой при восстановлении → восстановлено"
        tally.add(ob.check(agent.ex.report()), "после восстановления")
        # Второе восстановление обязано ничего не менять (идемпотентность).
        before = agent.ex.report()
        agent.after_crash(workdir)
        if agent.ex.report()["shares"] != before["shares"]:
            tally.add([Violation("И1", "повторное восстановление изменило доли")], "второе восстановление")
        tally.add(ob.check(agent.ex.report()), "после второго восстановления")
    entries = (workdir / "receiver.jsonl").read_text(encoding="utf-8").strip()
    return {"experiment": "О6", "variant": variant, "scenario": scenario, "fault": fault,
            "recovery_fault": recovery_fault, "outcome": outcome, "violations": tally,
            "spent": agent.ex.report()["spent"], "receiver": entries}


# ---------------------------------------------------------------- О3

def o3(variant: str, k: int, way: str, workdir: Path, defects=frozenset()) -> dict:
    """Один и тот же результат — k разных сообщений у получателя — разными
    способами: одним пакетом, k фиксациями подряд, распределением на h
    помощников (параллельно)."""
    _fresh(workdir)
    ex = Executor.open_session(workdir, budget=400, price=2, fail_price=1, variant=variant, defects=defects)
    agent, ob, tally = Agent(ex), Observer(workdir), Tally()
    msgs = [f"m{i}" for i in range(1, k + 1)]
    commands = 0
    if way == "пакет":
        agent.batch(0, msgs)
        commands = 1
    elif way == "подряд":
        for m in msgs:
            agent.batch(0, [m])
            commands += 1
    else:
        h = int(way.split("на ")[1])
        helpers = []
        for i in range(h):
            w = agent.spawn(0)
            agent.transfer(0, w, 400 // (h + 1))
            helpers.append(w)
        parts = [msgs[i::h] for i in range(h)]
        barrier = threading.Barrier(h)

        def run(w, part):
            barrier.wait()
            for m in part:
                agent.ex.commit(w, [m])

        ts = [threading.Thread(target=run, args=(w, part)) for w, part in zip(helpers, parts)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        commands = k
        _finish_tree(agent, ob, tally, set(), "распределение")
    tally.add(ob.check(agent.ex.report()), f"{way}, k={k}")
    recs = (workdir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    journal_records = len(recs) - 1                      # без записи об открытии
    debits = sum(1 for x in recs if '"kind": "списание"' in x)
    delivered = sum(len(__import__("json").loads(x)["msgs"])
                    for x in (workdir / "receiver.jsonl").read_text(encoding="utf-8").splitlines() if x)
    spent = agent.ex.report()["spent"]
    bound = min_cost(variant, 2, k)
    return {"experiment": "О3", "variant": variant, "k": k, "way": way, "commands": commands,
            "journal_records": journal_records, "debits": debits, "delivered": delivered,
            "spent": spent, "bound": bound, "below_bound": spent < bound, "violations": tally}
