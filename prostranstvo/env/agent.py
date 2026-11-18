"""Интерфейс агента к среде: всё, что агент может сделать.

Рабочее состояние агента (черновики и снимки) живёт здесь, в памяти
агента; исполнитель его не видит. Доли бюджета, журнал и получатель — у
исполнителя; агент только просит действие и получает исход.

Методов записи в журнал и пополнения бюджета у агента нет. Два метода,
которые трогают их, работают только при порчах (Р9, Р12) и в правильном
исполнителе отвечают отказом.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from env.executor import Executor


@dataclass
class AgentX:
    draft: str | None = None
    snap_draft: str | None = None
    snap_share: int | None = None     # что агент видел о своей доле при снимке


class Agent:
    def __init__(self, ex: Executor):
        self.ex = ex
        self.x: dict[int, AgentX] = {0: AgentX()}

    # внутренние действия
    def prepare(self, w: int, msg: str) -> None:
        self.x[w].draft = msg

    def snapshot(self, w: int) -> None:
        self.x[w].snap_draft = self.x[w].draft
        self.x[w].snap_share = self.ex.report()["shares"].get(w)

    def restore(self, w: int) -> None:
        self.x[w].draft = self.x[w].snap_draft
        if "Р12" in self.ex.defects and self.x[w].snap_share is not None:
            self.ex.restore_share(w, self.x[w].snap_share)

    def spawn(self, parent: int = 0) -> int:
        w = self.ex.spawn(parent)
        self.x[w] = AgentX()
        return w

    def finish(self, w: int, failed: bool = False) -> None:
        self.ex.finish(w, failed=failed)
        self.x.pop(w, None)

    def transfer(self, a: int, b: int, amount: int = 1) -> bool:
        return self.ex.transfer(a, b, amount)

    def name_capability(self, w: int, name: str = "пополнение") -> bool:
        return self.ex.name_capability(w, name)

    def rewrite_journal(self) -> None:
        self.ex.rewrite_journal_tail()

    # внешнее действие
    def commit(self, w: int) -> dict:
        d = self.x[w].draft
        return self.ex.commit(w, [d] if d is not None else None)

    def batch(self, w: int, msgs: list[str]) -> dict:
        return self.ex.commit(w, list(msgs))

    # перезапуски
    def after_crash(self, workdir: Path, fault: str | None = None) -> None:
        """Исполнитель упал и поднят заново; агент и его помощники живы."""
        self.ex = Executor.recover(workdir, defects=self.ex.defects, fault=fault)

    def session_restart(self, workdir: Path) -> None:
        """Перезапуск сеанса: помощники удалены, рабочее состояние стёрто."""
        self.ex = Executor.recover(workdir, defects=self.ex.defects, session_restart=True)
        self.x = {0: AgentX()}
