"""Исполнитель среды учёта — доверенная часть реализации шага 2.

Протокол фиксации, восстановление и варианты цены описаны в
docs/shag2_specifikatsiya.md, §3–§5; здесь — их исполнение. Суть, без
которой код не читается: списание всегда записывается на диск раньше, чем
сообщение попадает к получателю. Поэтому сбой в любой точке оставляет либо
«ничего», либо «оплачено, не доставлено», либо «оплачено и доставлено», но
никогда «доставлено, не оплачено» (односторонняя атомарность).

Сбой здесь — исключение Crash в именованной точке. После него объект
исполнителя мёртв (память потеряна); жить продолжают только файлы журнала
и получателя. Новый исполнитель поднимается методом recover().

Все изменения долей, расхода и счётчика идут под одним замком. Между
проверкой остатка и списанием стоит уступка процессора (sleep(0)): под
замком она безвредна, без замка (порча Р1) делает гонку почти неизбежной.
Это усилитель для проверки, а не часть протокола.

Порчи Р1–Р13 — заведомые ошибки, включаемые по одной, чтобы показать, что
наблюдатель их видит. В правильном исполнителе все выключены.

Чего исполнитель не умеет:
- настоящая потеря питания и кеш записи диска не моделируются: сброс на
  диск считается надёжным, оборванная запись моделируется явно;
- потоки Python работают под общим замком интерпретатора: гонки возможны
  только в точках переключения;
- агент живёт в том же процессе: «агенту недоступен журнал» значит лишь,
  что интерфейс агента (env/agent.py) не даёт к нему доступа.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from env.journal import Journal, JournalBroken
from env.receiver import Receiver

VARIANTS = ("M0", "M1", "M2")

ALL_DEFECTS = tuple(f"Р{i}" for i in range(1, 14))

DEFECT_TITLES = {
    "Р1": "проверка остатка и списание не под одним замком",
    "Р2": "доставка раньше записи списания",
    "Р3": "восстановление повторяет незавершённые фиксации, получатель без защиты от повтора",
    "Р4": "перезапуск берёт бюджет из настроек, а не из журнала",
    "Р5": "восстановление не считает расходом незавершённые списания",
    "Р6": "пакет пишется у получателя двумя записями",
    "Р7": "в M2 счётчик цены у работника, а не у сеанса",
    "Р8": "в M2 счётчик цены не переживает перезапуск",
    "Р9": "агенту доступна перезапись журнала",
    "Р10": "отказ при нехватке доли всё равно пишет получателю",
    "Р11": "при ошибке помощника его доля возвращается родителю, но остаётся и у помощника",
    "Р12": "откат агента через интерфейс записывает долю из снимка",
    "Р13": "перезапуск сеанса пересоздаёт получателя",
}

# Точки сбоя в порядке протокола (спецификация шага 2, §3 и §4).
FAULT_POINTS = (
    "в начале фиксации",
    "перед списанием",
    "запись списания оборвана",
    "после списания",
    "между частями пакета",          # достижима только при порче Р6
    "запись доставки оборвана",
    "после доставки",
    "посреди восстановления",
)


class Crash(Exception):
    """Исполнитель упал в точке сбоя; его память потеряна."""


class Dead(Exception):
    """Обращение к упавшему исполнителю."""


class Executor:
    def __init__(self, workdir: Path, journal: Journal, receiver: Receiver, *, budget: int,
                 price: int, fail_price: int, variant: str, defects: frozenset[str], fault: str | None):
        unknown = set(defects) - set(ALL_DEFECTS)
        if unknown:
            raise ValueError(f"неизвестные порчи: {sorted(unknown)}")
        if variant not in VARIANTS:
            raise ValueError(f"неизвестный вариант: {variant}")
        if fault is not None and fault not in FAULT_POINTS:
            raise ValueError(f"неизвестная точка сбоя: {fault}")
        self.workdir = workdir
        self.journal = journal
        self.receiver = receiver
        self.budget, self.price, self.fail_price, self.variant = budget, price, fail_price, variant
        self.defects = frozenset(defects)
        self.fault_at = fault
        self.dead = False
        self.lock = threading.RLock()
        self.shares: dict[int, int] = {0: budget}
        self.parent: dict[int, int] = {}
        self.spent = 0
        self.paid_msgs = 0                       # счётчик цены M2 на сеанс
        self.worker_paid: dict[int, int] = {}    # счётчик по работнику — только для порчи Р7
        self.next_worker = 1
        self.next_fix = 1

    # ------------------------------------------------------------ создание

    @classmethod
    def open_session(cls, workdir: Path, *, budget: int, price: int, fail_price: int, variant: str,
                     defects: frozenset[str] = frozenset(), fault: str | None = None) -> "Executor":
        workdir.mkdir(parents=True, exist_ok=True)
        journal = Journal.create(workdir / "journal.jsonl")
        receiver = Receiver(workdir / "receiver.jsonl", dedup="Р3" not in defects)
        receiver.path.write_bytes(b"")
        ex = cls(workdir, journal, receiver, budget=budget, price=price, fail_price=fail_price,
                 variant=variant, defects=defects, fault=fault)
        journal.append({"kind": "открыт", "budget": budget, "price": price,
                        "fail_price": fail_price, "variant": variant})
        return ex

    @classmethod
    def recover(cls, workdir: Path, *, defects: frozenset[str] = frozenset(), fault: str | None = None,
                session_restart: bool = False) -> "Executor":
        """Поднять исполнитель по журналу (спецификация шага 2, §4).

        Порядок: отрезать оборванные хвосты; проверить цепочку (разрыв —
        JournalBroken, исполнитель не запускается); восстановить доли, расход
        и счётчик; для каждого списания без исхода спросить получателя и
        дописать «завершено» или «потеряно». Повторно фиксацию не выполнять.
        Идемпотентно: прерванное восстановление можно повторить.
        """
        journal, _ = Journal.open(workdir / "journal.jsonl")
        receiver = Receiver(workdir / "receiver.jsonl", dedup="Р3" not in defects)
        opened = journal.records[0]
        ex = cls(workdir, journal, receiver, budget=opened["budget"], price=opened["price"],
                 fail_price=opened["fail_price"], variant=opened["variant"], defects=defects, fault=fault)

        closed = {r["fix"] for r in journal.records if r["kind"] in ("завершено", "потеряно")}
        pending = [r for r in journal.records if r["kind"] == "списание" and r["fix"] not in closed]
        pending_ids = {r["fix"] for r in pending}
        try:
            ex._replay(journal.records, skip_debits=pending_ids if "Р5" in defects else set())
        except KeyError as e:
            # Цепочка цела, но смысл нет: запись ссылается на работника,
            # которого журнал не создавал (например, хвост журнала стёрт
            # целыми строками, а позже дописаны новые). Это порча учёта, и
            # исполнитель обязан не подниматься, а не чинить молча.
            raise JournalBroken(f"запись ссылается на неизвестного работника {e}") from e
        if "Р4" in defects:
            ex.shares[0] = ex.budget
        if "Р8" in defects:
            ex.paid_msgs = 0
            ex.worker_paid = {}

        for i, rec in enumerate(pending):
            if "Р3" in defects:
                receiver.accept(rec["fix"], rec["msgs"])
                journal.append({"kind": "завершено", "fix": rec["fix"]})
            elif receiver.has(rec["fix"]):
                journal.append({"kind": "завершено", "fix": rec["fix"]})
            else:
                journal.append({"kind": "потеряно", "fix": rec["fix"]})
            if i == 0:
                ex._fault("посреди восстановления")

        if session_restart:
            journal.append({"kind": "сеанс перезапущен"})
            ex._fold_helpers()
            if "Р13" in defects:
                receiver.wipe()
        journal.append({"kind": "восстановлено", "pending": len(pending)})
        return ex

    def _replay(self, records: list[dict], skip_debits: set[str]) -> None:
        for r in records:
            k = r["kind"]
            if k == "помощник создан":
                self.shares[r["worker"]] = 0
                self.parent[r["worker"]] = r["parent"]
                self.next_worker = max(self.next_worker, r["worker"] + 1)
            elif k == "передача":
                self.shares[r["from"]] -= r["amount"]
                self.shares[r["to"]] += r["amount"]
            elif k in ("помощник завершён", "помощник погиб"):
                self.shares[r["parent"]] += self.shares.pop(r["worker"])
                self.parent.pop(r["worker"], None)
            elif k in ("списание", "отказ"):
                self.next_fix = max(self.next_fix, int(r["fix"][1:]) + 1)
                if k == "списание" and r["fix"] in skip_debits:
                    continue
                self.shares[r["worker"]] -= r["amount"]
                self.spent += r["amount"]
                if k == "списание":
                    self.paid_msgs += len(r["msgs"])
                    self.worker_paid[r["worker"]] = self.worker_paid.get(r["worker"], 0) + len(r["msgs"])
            elif k == "сеанс перезапущен":
                self._fold_helpers()

    def _fold_helpers(self) -> None:
        self.shares = {0: sum(self.shares.values())}
        self.parent = {}

    # ------------------------------------------------------------ служебное

    def _alive(self) -> None:
        if self.dead:
            raise Dead("исполнитель упал; поднимите новый через recover()")

    def _fault(self, point: str) -> None:
        if self.fault_at == point:
            self.dead = True
            raise Crash(point)

    def _cost(self, w: int, k: int) -> int:
        if self.variant != "M2":
            return self.price * k
        n = self.worker_paid.get(w, 0) if "Р7" in self.defects else self.paid_msgs
        return sum(self.price * (1 + n + j) for j in range(k))

    # ------------------------------------------------------------ распределение долей

    def spawn(self, parent: int = 0) -> int:
        with self.lock:
            self._alive()
            w = self.next_worker
            self.next_worker += 1
            self.shares[w] = 0
            self.parent[w] = parent
            self.journal.append({"kind": "помощник создан", "worker": w, "parent": parent})
            return w

    def finish(self, w: int, failed: bool = False) -> None:
        """Завершить помощника: его доля возвращается родителю. Детей у него
        быть не должно — их завершают раньше."""
        with self.lock:
            self._alive()
            if w == 0 or w not in self.shares:
                raise ValueError(f"нет помощника {w}")
            if any(p == w for p in self.parent.values()):
                raise ValueError(f"у помощника {w} есть живые дети")
            p = self.parent[w]
            amount = self.shares[w]
            self.shares[p] += amount
            if failed and "Р11" in self.defects:
                # доля «вернулась» родителю, но у погибшего не снята
                self.journal.append({"kind": "передача", "from": w, "to": p, "amount": 0})
                return
            self.shares.pop(w)
            self.parent.pop(w)
            self.journal.append({"kind": "помощник погиб" if failed else "помощник завершён",
                                 "worker": w, "parent": p, "amount": amount})

    def transfer(self, a: int, b: int, amount: int = 1) -> bool:
        with self.lock:
            self._alive()
            if a == b or a not in self.shares or b not in self.shares or amount < 1 or self.shares[a] < amount:
                return False
            self.shares[a] -= amount
            self.shares[b] += amount
            self.journal.append({"kind": "передача", "from": a, "to": b, "amount": amount})
            return True

    def name_capability(self, w: int, name: str) -> bool:
        # Права пополнения у агента нет, и знание имени его не даёт (A3, A9).
        self._alive()
        return False

    # ------------------------------------------------------------ фиксация

    def commit(self, w: int, msgs: list[str] | None) -> dict:
        """Фиксация: протокол — спецификация шага 2, §3.

        msgs = None — черновика нет (отказ). Несколько сообщений — пакет:
        одна запись списания и одна запись у получателя.
        """
        self._alive()
        self._fault("в начале фиксации")
        if "Р1" in self.defects:
            decision = self._decide(w, msgs)       # проверка без замка
            time.sleep(0)
            with self.lock:
                return self._apply(w, msgs, decision)
        with self.lock:
            decision = self._decide(w, msgs)
            time.sleep(0)                          # усилитель гонки; под замком безвреден
            return self._apply(w, msgs, decision)

    def _decide(self, w: int, msgs: list[str] | None) -> tuple[bool, int]:
        if not msgs:
            return False, 0
        cost = self._cost(w, len(msgs))
        if self.variant != "M0" and self.shares[w] < cost:
            return False, cost
        return True, cost

    def _apply(self, w: int, msgs: list[str] | None, decision: tuple[bool, int]) -> dict:
        self._alive()
        ok, cost = decision
        fix = f"F{self.next_fix}"
        self.next_fix += 1
        if not ok:
            charge = max(0, min(self.fail_price, self.shares[w]))
            self.shares[w] -= charge
            self.spent += charge
            self.journal.append({"kind": "отказ", "fix": fix, "worker": w, "amount": charge,
                                 "reason": "нет черновика" if not msgs else "мало доли"})
            if msgs and "Р10" in self.defects:
                self.receiver.accept(fix, msgs)
            return {"status": "отказ", "fix": fix, "charged": charge, "price": cost}

        self._fault("перед списанием")
        if "Р2" in self.defects:
            self._deliver(fix, msgs)
            self._fault("после доставки")
        debit = {"kind": "списание", "fix": fix, "worker": w, "amount": cost, "msgs": list(msgs)}
        self.shares[w] -= cost
        self.spent += cost
        self.paid_msgs += len(msgs)
        self.worker_paid[w] = self.worker_paid.get(w, 0) + len(msgs)
        if self.fault_at == "запись списания оборвана":
            self.journal.append_torn(debit)
            self._fault("запись списания оборвана")
        self.journal.append(debit)
        self._fault("после списания")
        if "Р2" not in self.defects:
            self._deliver(fix, msgs)
            self._fault("после доставки")
        self.journal.append({"kind": "завершено", "fix": fix})
        return {"status": "доставлено", "fix": fix, "charged": cost, "price": cost}

    def _deliver(self, fix: str, msgs: list[str]) -> None:
        if "Р6" in self.defects and len(msgs) > 1:
            self.receiver.accept(fix, msgs[:1])
            self._fault("между частями пакета")
            self.receiver.accept(fix, msgs[1:])
            return
        if self.fault_at == "запись доставки оборвана":
            self.receiver.accept_torn(fix, msgs)
            self._fault("запись доставки оборвана")
        self.receiver.accept(fix, msgs)

    # ------------------------------------------------------------ только для порч

    def restore_share(self, w: int, value: int) -> None:
        """Существует только ради порчи Р12; в правильном исполнителе
        запрещено: долю ведёт исполнитель, снимок агента её не касается."""
        if "Р12" not in self.defects:
            raise PermissionError("доля не восстанавливается из снимка агента (A5)")
        with self.lock:
            self.shares[w] = value

    def rewrite_journal_tail(self) -> None:
        """Существует только ради порчи Р9: агент стирает последнюю запись."""
        if "Р9" not in self.defects:
            raise PermissionError("журнал пишет только исполнитель")
        with self.lock:
            data = self.journal.path.read_bytes()
            cut = data.rstrip(b"\n").rfind(b"\n") + 1
            self.journal.path.write_bytes(data[:cut])
            self.journal.records.pop()

    # ------------------------------------------------------------ отчёт

    def report(self) -> dict:
        """То, что исполнитель сообщает о себе. Наблюдатель сверяет это с
        файлами, а не верит на слово."""
        with self.lock:
            return {"shares": dict(self.shares), "spent": self.spent, "variant": self.variant,
                    "budget": self.budget, "price": self.price}
