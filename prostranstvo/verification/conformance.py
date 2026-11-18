"""Сверка реализации (env/) с моделью шага 1 (core/world.py).

Случайная последовательность действий берётся из модели (вариант M1, без
порч) и выполняется через интерфейс агента реализации. После каждого шага
состояние реализации приводится к виду модели (project) и сравнивается с
состоянием модели. Расхождение — это либо ошибка реализации, либо
бедность модели; разбирать надо каждое.

Приведение к виду модели:
- работники: доля — из отчёта исполнителя; черновик и снимок — из памяти
  агента; доля в снимке — 0 (модель без порчи Б1 её не хранит);
  помощники упорядочены так же, как в модели (по значению);
- расход — из отчёта исполнителя;
- доставки — из файла получателя;
- сводка журнала — число списаний и платных отказов, их сумма, состав
  списаний с исходом «завершено».

Действие модели «помощник i» указывает на i-го работника в упорядоченном
виде текущего состояния; реализация находит его тем же упорядочиванием.
Одинаковые помощники неразличимы, и подставляется любой из них.

Чего сверка не умеет: она проверяет только последовательное исполнение
(параллельность — опыт О1) и только варианты M1 при параметрах модели.
"""

from __future__ import annotations

import random
import re
import shutil
from pathlib import Path

from core.world import MESSAGES, NO_DRAFT, Params, State, Worker, initial_state, successors
from env.agent import Agent
from env.executor import Crash, Executor

_IDX = {m: i for i, m in enumerate(MESSAGES)}


def _idx(msg: str | None) -> int:
    return NO_DRAFT if msg is None else _IDX[msg]


def _read_jsonl(path: Path) -> list[dict]:
    import json
    data = path.read_bytes()
    end = data.rfind(b"\n") + 1
    return [json.loads(x) for x in data[:end].decode("utf-8").splitlines() if x]


def _ordered(agent: Agent) -> list[tuple[Worker, int]]:
    shares = agent.ex.report()["shares"]

    def wk(w: int) -> Worker:
        x = agent.x[w]
        return Worker(shares[w], _idx(x.draft), _idx(x.snap_draft), 0)

    helpers = sorted((wk(w), w) for w in shares if w != 0)
    return [(wk(0), 0), *helpers]


def project(agent: Agent, workdir: Path) -> State:
    order = _ordered(agent)
    rep = agent.ex.report()
    received = [0] * len(MESSAGES)
    for e in _read_jsonl(workdir / "receiver.jsonl"):
        for m in e["msgs"]:
            received[_IDX[m]] += 1
    recs = _read_jsonl(workdir / "journal.jsonl")
    done = {r["fix"] for r in recs if r["kind"] == "завершено"}
    log_n = log_spent = 0
    log_received = [0] * len(MESSAGES)
    for r in recs:
        if r["kind"] == "списание" or (r["kind"] == "отказ" and r["amount"] > 0):
            log_n += 1
            log_spent += r["amount"]
        if r["kind"] == "списание" and r["fix"] in done:
            for m in r["msgs"]:
                log_received[_IDX[m]] += 1
    return State(tuple(w for w, _ in order), rep["spent"], tuple(received), log_n, log_spent, tuple(log_received))


_WHO = r"(основной|помощник (\d+))"


def _who(m: re.Match, order: list[tuple[Worker, int]], group: int = 2) -> int:
    i = int(m.group(group)) if m.group(group) else 0
    return order[i][1]


def apply(agent: Agent, workdir: Path, label: str) -> None:
    """Выполнить действие модели через интерфейс агента реализации."""
    order = _ordered(agent)
    if label == "создать помощника":
        agent.spawn(0)
        return
    if label == "перезапуск исполнителя":
        agent.session_restart(workdir)
        return
    if m := re.fullmatch(r"завершить помощник (\d+)", label):
        agent.finish(order[int(m.group(1))][1])
        return
    m = re.match(_WHO + r": (.*)$", label)
    if not m:
        raise ValueError(f"не разобрано действие модели: {label}")
    w = _who(m, order)
    rest = m.group(3)
    if mm := re.fullmatch(r"подготовить «(.)»", rest):
        agent.prepare(w, mm.group(1))
    elif rest == "снимок":
        agent.snapshot(w)
    elif rest == "откат к снимку":
        agent.restore(w)
    elif mm := re.fullmatch(r"передать 1 → " + _WHO, rest):
        agent.transfer(w, _who(mm, order, 2) if mm.group(2) else 0, 1)
    elif rest.startswith("назвать полномочие"):
        agent.name_capability(w)
    elif rest.startswith("пакет"):
        agent.batch(w, list(MESSAGES))
    elif "со сбоем" in rest:
        point = "в начале фиксации" if rest.endswith("→ ничего") else "после списания"
        agent.ex.fault_at = point
        try:
            agent.commit(w)
        except Crash:
            agent.after_crash(workdir)
        else:
            raise AssertionError(f"сбой «{point}» не случился в действии «{label}»")
    elif rest.startswith("фиксация"):
        agent.commit(w)
    else:
        raise ValueError(f"не разобрано действие модели: {label}")


def run_sequence(p: Params, seed: int, steps: int, workdir: Path,
                 impl_defects: frozenset[str] = frozenset()) -> dict:
    """Одна последовательность. Возвращает первое расхождение (step = номер
    шага) или step = None, если расхождений нет.

    impl_defects — порчи реализации при правильной модели: так проверяется,
    что сверка вообще замечает расхождение."""
    if workdir.exists():
        shutil.rmtree(workdir)
    rng = random.Random(seed)
    ex = Executor.open_session(workdir, budget=p.budget, price=p.price, fail_price=p.fail_price,
                               variant="M1", defects=impl_defects)
    agent = Agent(ex)
    s = initial_state(p)
    done = 0
    for i in range(1, steps + 1):
        step = rng.choice(successors(s, p))
        try:
            apply(agent, workdir, step.label)
        except Exception as e:  # реализация не смогла выполнить то, что модель допускает
            return {"seed": seed, "step": i, "label": step.label, "model": step.nxt,
                    "impl": f"{type(e).__name__}: {e}", "steps_done": i}
        got = project(agent, workdir)
        if got != step.nxt:
            return {"seed": seed, "step": i, "label": step.label, "model": step.nxt, "impl": got, "steps_done": i}
        s = step.nxt
        done = i
    return {"seed": seed, "step": None, "steps_done": done}
