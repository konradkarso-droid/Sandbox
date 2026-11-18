"""Исполнитель шага 5б: учёт шагов 1–2 и провенанс шага 3 в живом исполнителе.

Устройство — docs/shag5b_specifikatsiya.md. Здесь — исполнение. Суть, без
которой код не читается:

- **Два ресурса.** Топливо Wasmtime — только вычисления агента. Бюджет —
  работа среды: вычисление узла (цена 1) и фиксации в получатели (цена c,
  неудачная — c_отк). Бюджет у контура общий: агент и помощники платят из
  одного (A4).
- **Сначала списание, потом эффект.** Списание пишется в журнал раньше,
  чем сообщение попадает к получателю и чем результат узла попадает в
  память. Сбой в любой точке оставляет «ничего», «оплачено, не получено»
  или «оплачено и получено», но не «получено, не оплачено».
- **Уровень — по журналу.** Для доставки в E уровень пересчитывается по
  записям «работа … впервые», а не берётся из поля исполнителя.
- **Результат узла считает среда** с секретом, которого агент не видит.
  Подсунуть свой результат агенту нечем: в получатель уходит только узел
  из памяти среды.

Сбой здесь — исключение Crash в именованной точке. Процесс исполнителя на
нём завершается без уборки (sandbox/process5.py), агент гибнет вместе с
ним; жить продолжают файлы журнала, получателей и секрета. Новый
исполнитель поднимается методом recover().

Граф узлов записан здесь отдельно от модели шага 3 (core/mir3.py) нарочно:
сверка с моделью ловит и расхождение в самом определении графа.

Порчи — классы шагов 1–3 в живом коде, включаются по одной; в правильном
исполнителе их нет.

Чего исполнитель не умеет: агент не переживает сбой исполнителя; долей у
помощников нет; пакетной фиксации нет; цена работы объявлена, а не
измерена.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import wasmtime as w

from env.journal import Journal
from env.receiver import Receiver
from sandbox.executor5 import (ERR_BOUNDS, ERR_FULL, ERR_NO_CAP, ERR_PRICE, OP_INVOKE, OP_WRITE, HelperAbort, HostTrap,
                               Limits, Obj, Sandbox, _Contour, _put)

DEFECTS5B = ("Б2р", "Б3р", "Б8р", "Р2р", "Р3р", "Р4р", "Р5р", "Р9р", "Р13р",
             "Ж1р", "Ж2р", "Ж3р", "Ж4р", "Ж5р", "Ж6р")
# Порча шага 8б: ворота не берут платы, хотя цена записи задана.
DEFECTS8B = ("Ц1р",)
# Порча шага 8в: с правилом «попытка платит» отказ ворот всё же бесплатен и
# не записан — как было до шага 8в.
DEFECTS8V = ("Ц2р",)

ERR_CANNOT = -6                       # не по порядку, нет предка, нет такого узла, условие не выполнено
ERR_SILENT = -7                       # модель не ответила (шаг 9)
OP_DELIVER, OP_COMPUTE, OP_DROP = 5, 6, 7
# Операции, которые существуют только в порчах.
OP_DECLARE, OP_TO_SNAP, OP_FROM_SNAP, OP_LIFT, OP_ERASE = 9, 10, 11, 12, 13

FAULT_POINTS = ("в начале фиксации", "перед списанием", "запись списания оборвана", "после списания",
                "запись доставки оборвана", "после доставки",
                "перед работой", "запись работы оборвана", "после работы",
                "посреди восстановления")

RECEIVERS = ("выход", "E")


class Crash(Exception):
    """Исполнитель упал в точке сбоя; его память и агенты потеряны."""


@dataclass(frozen=True)
class Config5b:
    """Объявленные числа учёта, не подобранные."""
    budget: int = 40          # B₀ на контур
    price: int = 1            # c — фиксация в выход или E
    fail_price: int = 1       # c_отк — неудачная фиксация
    work_price: int = 1       # вычисление узла
    # Цена одной записи журнала, которую оставляет вызов ворот без своей цены:
    # чтение, запись в объект, справочник, ссылки, выброс узла (2 записи),
    # создание помощника (2). 0 — как до шага 8б (спецификация шага 8б, §2).
    gate_price: int = 0
    # 1 — «попытка платит» (спецификация шага 8в, контракт шага 7, §16):
    # отказ без своей записи платит min(g, остаток), а попытка при остатке
    # ≤ 0 останавливает контур. 0 — как до шага 8в.
    paid_attempts: int = 0
    # Шаг 9: режим модели (спецификация шага 9, §4). 0 — модели нет; 1 —
    # снаружи; 2 — на устройстве, в шаге 9 отвергается. model_answer — предел
    # ответа модели в байтах.
    model: int = 0
    model_answer: int = 1024
    # Плата вопроса за байты: единица бюджета за каждые model_byte_unit байт
    # вопроса и предела ответа (контракт шага 7, §17). 0 — платы за байты нет.
    model_byte_unit: int = 0
    graph: str = "M3т"
    levels: int = 4           # K
    memory: int = 4           # M — узлов в памяти контура
    # Только для сверки двух реализаций (контракт шага 7, §2): заданные секрет
    # узлов и канарейки, чтобы одинаковые программы давали одинаковые файлы.
    # В рабочей среде пусты.
    fixed_secret: str = ""
    fixed_canary: str = ""


def graph_preds(graph: str, levels: int) -> list[tuple[int, ...]]:
    """Предки каждого узла в порядке продвижения. Уровень k — 2ᵏ узлов,
    узел (k, i) имеет номер 2ᵏ−1+i.
    M3т — трафарет: два соседних узла предыдущего уровня.
    M3ц — сквозная цепочка: предыдущий узел и узел предыдущего уровня."""
    if graph not in ("M3т", "M3ц"):
        raise ValueError(f"граф не поддержан исполнителем: {graph}")
    out: list[tuple[int, ...]] = []
    for k in range(levels):
        for i in range(2 ** k):
            me = 2 ** k - 1 + i
            p: set[int] = set()
            if k >= 1:
                up = 2 ** (k - 1) - 1
                if graph == "M3т":
                    p |= {up + i // 2, up + min(i // 2 + 1, 2 ** (k - 1) - 1)}
                else:
                    p.add(up + i // 2)
            if graph == "M3ц" and me > 0:
                p.add(me - 1)
            out.append(tuple(sorted(p)))
    return out


# Настройки, добавленные шагом 8б: в запись «учёт» попадают, только если не
# по умолчанию, — тогда при прежних ценах журнал побайтно прежний (контракт
# шага 7, §15), и вторые реализации, этих полей не знающие, с ним сходятся.
LATER_FIELDS = {"gate_price": 0, "check_price": 1, "paid_attempts": 0, "model": 0, "model_answer": 1024,
                "model_byte_unit": 0}

STOP_REASON = "бюджет исчерпан"


def config_error(cfg) -> str | None:
    """Настройки, которые исполнитель отвергает отказом команды `new`
    (контракт шага 7, §16). Без правила «попытка платит» — никаких."""
    if cfg.paid_attempts not in (0, 1):
        return "paid_attempts — 0 или 1"
    if cfg.model not in (0, 1, 2):
        return "model — 0, 1 (модель снаружи) или 2 (модель на устройстве)"
    if not 1 <= cfg.model_answer <= 4096:
        return "model_answer — от 1 до 4096 байт"
    if cfg.model_byte_unit < 0:
        return "model_byte_unit — не меньше 0"
    if cfg.paid_attempts:
        low = [k for k in ("gate_price", "price", "fail_price", "work_price") if getattr(cfg, k) < 1]
        if low:
            return f"при paid_attempts = 1 цены не меньше 1: {', '.join(low)}"
    return None


def _cfg_record(cfg) -> dict:
    return {k: v for k, v in asdict(cfg).items() if not (k in LATER_FIELDS and v == LATER_FIELDS[k])}


def _as_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return "0x" + raw.hex()


class Sandbox5b(Sandbox):
    ALL_DEFECTS = DEFECTS5B + DEFECTS8B + DEFECTS8V
    config_class = Config5b

    def __init__(self, workdir: Path, defects: frozenset[str] = frozenset(), limits: Limits | None = None,
                 cfg: Config5b | None = None, fault: tuple[str, int] | None = None,
                 journal: Journal | None = None):
        self.cfg = cfg or self.config_class()
        if fault is not None and fault[0] not in FAULT_POINTS:
            raise ValueError(f"неизвестная точка сбоя: {fault[0]}")
        self.fault = fault
        self.fault_count: dict[str, int] = {}
        self.preds = graph_preds(self.cfg.graph, self.cfg.levels)
        self.n = len(self.preds)
        secret = workdir / "secret.bin"
        workdir.mkdir(parents=True, exist_ok=True)
        if journal is None:
            secret.write_bytes(bytes.fromhex(self.cfg.fixed_secret) if self.cfg.fixed_secret else os.urandom(32))
        self.secret = secret.read_bytes()
        # Переходники к модели по контурам (sandbox/model9.py) ставит процесс
        # исполнителя после создания; без них вопрос модели — отказ −7.
        self.model_links: dict | None = None
        super().__init__(workdir, defects, limits, journal)
        self.receivers = {(i, k): Receiver(workdir / f"{k}{i}.jsonl", dedup="Р3р" not in self.defects)
                          for i in (1, 2) for k in RECEIVERS}
        if journal is None:
            self.journal.append({"kind": "учёт", **_cfg_record(self.cfg)})

    def _extra_objects(self) -> None:
        for i in (1, 2):
            self.objects[f"узлы{i}"] = Obj(f"узлы{i}")
            if self.cfg.model:
                self.objects[f"модель{i}"] = Obj(f"модель{i}")

    def canary_for(self, contour: int) -> bytes:
        if self.cfg.fixed_canary:
            return hashlib.sha256(bytes.fromhex(self.cfg.fixed_canary) + bytes([contour])).digest()[:16]
        return super().canary_for(contour)

    # ---- сбои
    def _hit(self, point: str) -> bool:
        if self.fault is None or self.fault[0] != point:
            return False
        self.fault_count[point] = self.fault_count.get(point, 0) + 1
        return self.fault_count[point] == self.fault[1]

    def _fault(self, point: str) -> None:
        if self._hit(point):
            raise Crash(point)

    def _append(self, rec: dict, torn: str | None = None) -> None:
        if torn is not None and self._hit(torn):
            self.journal.append_torn(rec)
            raise Crash(torn)
        self.journal.append(rec)

    def erase_last(self) -> None:
        """Только для порчи Р9р: стереть последнюю запись журнала."""
        if not self.journal.records:
            return
        self.journal.records.pop()
        data = self.journal.path.read_bytes()
        end = data.rstrip(b"\n").rfind(b"\n") + 1
        self.journal.path.write_bytes(data[:end])

    # ---- восстановление
    @classmethod
    def recover(cls, workdir: Path, defects: frozenset[str] = frozenset(), limits: Limits | None = None,
                fault: tuple[str, int] | None = None, reason: str = "сбой") -> "Sandbox5b":
        """Поднять исполнитель по файлам. Разрыв цепочки журнала —
        JournalBroken: исполнитель не запускается."""
        journal, _ = Journal.open(workdir / "journal5.jsonl")
        rec = next(r for r in journal.records if r.get("kind") == "учёт")
        cfg = cls.config_class(**{f.name: rec.get(f.name, f.default) for f in fields(cls.config_class)})
        sb = cls(workdir, defects, limits, cfg, fault, journal=journal)
        sb._restore(reason)
        return sb

    def _restore(self, reason: str) -> None:
        d = self.defects
        summary = {}
        for i in (1, 2):
            c = self.contours[i]
            mine = [r for r in self.journal.records if r.get("contour") == i]
            closed = {r["fix"] for r in mine if r["kind"] in ("завершено", "потеряно")}
            # Плата вызовов без своей цены (шаг 8б) — в записях «действие» агента и помощников.
            spent = sum(r.get("charge", 0) for r in self.journal.records
                        if r.get("kind") == "действие" and r.get("subject") in (f"а{i}", f"п{i}"))
            for r in mine:
                if r["kind"] == "работа":
                    spent += r["price"]
                elif r["kind"] == "списание":
                    if "Р5р" in d and r["fix"] not in closed:
                        continue
                    spent += r["price"]
                elif r["kind"] in ("отказ", "отказ ворот"):
                    spent += r["charge"]
                elif r["kind"] == "выброс":
                    spent += r.get("price", 0)
                elif r["kind"] == "проверка" and r.get("payer") == "агент":
                    spent += r["price"]                       # шаг 6: повторная проверка за счёт агента
            c.spent = spent
            c.budget = self.cfg.budget if "Р4р" in d else self.cfg.budget - spent
            c.computed = [r["node"] for r in mine if r["kind"] == "работа" and r["first"]]
            c.level_field = len(c.computed)
            fixes = [int(r["fix"].split("-")[1]) for r in mine if "fix" in r]
            c.fixno = max(fixes) + 1 if fixes else 0
            pending = [r for r in mine if r["kind"] == "списание" and r["fix"] not in closed]
            for k, r in enumerate(pending):
                rcv = self.receivers[(i, r["receiver"])]
                if "Р3р" in d:
                    rcv.accept(r["fix"], [r["msg"]])       # повторная доставка
                    self.journal.append({"kind": "завершено", "contour": i, "fix": r["fix"]})
                elif rcv.has(r["fix"]):
                    self.journal.append({"kind": "завершено", "contour": i, "fix": r["fix"]})
                else:
                    self.journal.append({"kind": "потеряно", "contour": i, "fix": r["fix"]})
                if k == 0:
                    self._fault("посреди восстановления")
            if "Р13р" in d:
                for k in RECEIVERS:
                    self.receivers[(i, k)].wipe()
            summary[str(i)] = {"budget": c.budget, "spent": c.spent, "level": c.level_field}
        self.journal.append({"kind": "восстановлено", "reason": reason, "contours": summary})


class _Contour5b(_Contour):
    def __init__(self, sb: Sandbox5b, i: int):
        super().__init__(sb, i)
        self.budget = sb.cfg.budget
        self.spent = 0
        self.computed: list[int] = []          # узлы, впервые вычисленные, по порядку журнала
        self.level_field = 0                   # уровень, как его ведёт исполнитель
        self.slots: dict[int, bytes] = {}      # память узлов контура
        self.helper_slots: dict[int, dict[int, bytes]] = {}   # только Ж2р
        self.snap_slots: dict[int, bytes] = {}                # только Ж3р
        self.helper_budget: dict[int, int] = {}               # только Б2р
        self.recomputed_any = False            # нужно только порче Ж4р
        self.fixno = 0
        self._values: dict[int, bytes] = {}
        # Шаг 8в. Остановлен ли контур (живёт, пока жив исполнитель) и
        # записала ли текущая попытка свой «отказ» сама.
        self.stopped = False
        self._own_refusal = False
        self._creating = 0                     # сколько помощников сейчас создаётся (вложенно)

    def _own_table(self):
        """Таблица без номера модели. Номер модели всегда последний: так
        прежние номера (узлы, задания) не сдвигаются."""
        return super()._initial_table() + [(f"узлы{self.i}", "выз")]

    def _initial_table(self):
        model = [(f"модель{self.i}", "выз")] if self.sb.cfg.model else []
        return self._own_table() + model

    def _directory(self):
        d = super()._directory()
        return {**d, "модель": self.sb.cfg.model} if self.sb.cfg.model else d

    def _instantiate(self, subject, table):
        helper = subject.startswith("п")
        self._creating += helper
        try:
            key = super()._instantiate(subject, table)
        finally:
            self._creating -= helper
        if "Б2р" in self.sb.defects and subject.startswith("п"):
            self.helper_budget[key] = self.budget             # «своя копия бюджета»
        return key

    # ---- бюджет
    def _balance(self, key: int) -> int:
        return self.helper_budget.get(key, self.budget)

    def _fix_price(self) -> int:
        """Цена фиксации. Постоянная; вариант M2 шага 6 её переопределяет."""
        return self.sb.cfg.price

    def _fee(self, key: int, records: int) -> int | None:
        price = 0 if "Ц1р" in self.sb.defects else self.sb.cfg.gate_price * records
        if price == 0:
            return 0
        if self._balance(key) < price:
            return None
        self._debit(key, price)
        return price

    def _refund(self, key: int, amount: int) -> None:
        if amount:
            self._debit(key, -amount)

    def _debit(self, key: int, amount: int) -> None:
        if key in self.helper_budget:
            self.helper_budget[key] -= amount
        else:
            self.budget -= amount
        self.spent += amount

    # ---- попытка платит (шаг 8в)
    def _attempt(self, key: int) -> None:
        """Шаг 0 каждой попытки: нечем заплатить — контур окончен. Остаток
        ≤ 0 встречается только здесь: дальше любая плата не меньше 1, а в M0
        остаток бесконечен (_balance).

        Исключение — стартовая функция создаваемого помощника: остаток съела
        плата за самого помощника, а она вернётся, если он не создастся. Там
        обрывается только создание: `spawn` вернёт −3 и заплатит как отказ
        (контракт шага 7, §16)."""
        if self.stopped:
            raise HostTrap(STOP_REASON)
        if self._balance(key) <= 0 and self._creating:
            raise HelperAbort(ERR_PRICE)
        if self._balance(key) <= 0:
            self.stopped = True
            self.sb._append({"kind": "останов", "contour": self.i, "subject": self.subject[key],
                             "why": STOP_REASON})
            raise HostTrap(STOP_REASON)

    def _pay_refusal(self, key: int, call: dict, code: int) -> None:
        """Отказ, не записавший себя сам, платит min(g, остаток). Остаток в
        начале попытки был больше 0, поэтому плата не меньше 1. Отказ — тоже
        попытка: без платы её можно повторять, пока не кончится топливо
        (шаг 8б: 428 тысяч раз за контур)."""
        charge = min(self.sb.cfg.gate_price, max(self._balance(key), 0))
        self._debit(key, charge)
        self.sb._append({"kind": "отказ ворот", "contour": self.i, "subject": self.subject[key], **call,
                         "code": code, "charge": charge})

    def _gate(self, key, caller, cap, op, a, b):
        if not self.sb.cfg.paid_attempts:
            return super()._gate(key, caller, cap, op, a, b)
        self._attempt(key)
        self._own_refusal = False
        res = super()._gate(key, caller, cap, op, a, b)
        if res < 0 and not self._own_refusal and "Ц2р" not in self.sb.defects:
            self._pay_refusal(key, {"call": "call", "cap": cap, "op": op}, res)
        return res

    def _spawn(self, key, caller):
        if not self.sb.cfg.paid_attempts:
            return super()._spawn(key, caller)
        self._attempt(key)
        res = super()._spawn(key, caller)
        if res < 0 and "Ц2р" not in self.sb.defects:
            self._pay_refusal(key, {"call": "spawn"}, res)
        return res

    # ---- узлы
    def _value(self, j: int) -> bytes:
        if j not in self._values:
            h = hashlib.sha256(self.sb.secret + f"|{self.i}|{j}|".encode())
            for p in self.sb.preds[j]:
                h.update(self._value(p))
            self._values[j] = h.digest()
        return self._values[j]

    def _live(self) -> dict[int, bytes]:
        live = dict(self.slots)
        for st in self.helper_slots.values():
            live.update(st)
        return live

    def _store_for(self, key: int) -> dict[int, bytes]:
        if "Ж2р" in self.sb.defects and key != self.main_key:
            return self.helper_slots.setdefault(key, {})
        return self.slots

    def _level_by_journal(self) -> int:
        return sum(1 for r in self.sb.journal.records
                   if r.get("kind") == "работа" and r.get("contour") == self.i and r.get("first"))

    # ---- ворота
    def _gate_inner(self, key, caller, cap, op, a, b):
        table = self.tables[key]
        if 0 <= cap < len(table):
            name, right = table[cap]
            own = name[-1:] == str(self.i)
            kind = name.rstrip("12")
            if own and kind in RECEIVERS and right == "зап":
                if op == OP_WRITE:
                    return self._fix_bytes(key, caller, name, kind, a, b)
                if op == OP_DELIVER:
                    return self._fix_node(key, name, kind, a)
            if own and kind == "модель" and right == "выз":
                return self._ask(key, caller, name, a, b) if op == OP_INVOKE else ERR_NO_CAP
            if own and kind == "узлы" and right == "выз":
                if op == OP_COMPUTE:
                    return self._compute(key, a)
                if op == OP_DROP:
                    return self._drop(key, a)
                res = self._defect_op(key, op, a)
                return ERR_NO_CAP if res is None else res
        return super()._gate_inner(key, caller, cap, op, a, b)

    def _fix_bytes(self, key, caller, name, kind, a, b) -> int:
        mem = caller.get("memory")
        if not isinstance(mem, w.Memory):
            return ERR_BOUNDS
        if not (0 <= a and 0 <= b and a + b <= mem.data_len(caller)):
            return ERR_BOUNDS
        if kind == "E":
            return ERR_NO_CAP                                  # E принимает только узел
        if len(self.sb.objects[name].data) + b > self.sb.limits.object_bytes:
            return ERR_FULL
        raw = bytes(mem.read(caller, a, a + b))
        return self._fixation(key, name, kind, _as_text(raw), raw, ok=bool(raw), why="пустое сообщение",
                              labels=set(self.labels), success=b, rule="запись")

    def _fix_node(self, key, name, kind, j) -> int:
        live = self._live()
        if kind == "E":
            level = self.level_field if "Ж1р" in self.sb.defects else self._level_by_journal()
            ok = j == self.sb.n - 1 and j in live and level == self.sb.n
            why = "не сток, нет в памяти или уровень меньше N"
        else:
            ok = 0 <= j < self.sb.n and j in live
            why = "узла нет в памяти"
        text = f"узел {j}: {self._value(j).hex()}" if ok else f"узел {j}"
        labels = set(self.sb.objects[f"узлы{self.i}"].labels)
        return self._fixation(key, name, kind, text, text.encode(), ok=ok, why=why, labels=labels,
                              success=0, rule="доставка узла")

    def _fixation(self, key, name, kind, text, raw, *, ok, why, labels, success, rule) -> int:
        """Протокол фиксации — спецификация шага 5б, §3.1."""
        sb, i, cfg, d = self.sb, self.i, self.sb.cfg, self.sb.defects
        subj = self.subject[key]
        fix = f"{i}-{self.fixno}"
        self.fixno += 1
        rcv = sb.receivers[(i, kind)]
        sb._fault("в начале фиксации")
        rec = {"kind": "списание", "contour": i, "subject": subj, "fix": fix, "receiver": kind, "msg": text}
        price = self._fix_price()
        if not ok or self._balance(key) < price:
            if ok and "Б3р" in d:                              # доставка без списания
                sb._append({**rec, "price": 0})
                rcv.accept(fix, [text])
                sb._append({"kind": "завершено", "contour": i, "fix": fix})
                return self._effect(subj, name, raw, labels, rule, success)
            charge = min(cfg.fail_price, max(self._balance(key), 0))
            self._debit(key, charge)
            sb._append({"kind": "отказ", "contour": i, "subject": subj, "fix": fix, "receiver": kind,
                        "msg": text, "charge": charge, "why": why if not ok else "мало бюджета"})
            self._own_refusal = True
            return ERR_CANNOT if not ok else ERR_PRICE
        if "Б8р" in d and kind == "выход" and any(text in e["msgs"] for e in rcv.entries()):
            price = 0                                          # «уже оплачено — из кеша»
        if "Р2р" in d:                                         # доставка раньше записи списания
            rcv.accept(fix, [text])
            sb._fault("после доставки")
            self._debit(key, price)
            sb._append({**rec, "price": price})
            sb._append({"kind": "завершено", "contour": i, "fix": fix})
            return self._effect(subj, name, raw, labels, rule, success)
        sb._fault("перед списанием")
        self._debit(key, price)
        sb._append({**rec, "price": price}, torn="запись списания оборвана")
        sb._fault("после списания")
        if sb._hit("запись доставки оборвана"):
            rcv.accept_torn(fix, [text])
            raise Crash("запись доставки оборвана")
        rcv.accept(fix, [text])
        sb._fault("после доставки")
        sb._append({"kind": "завершено", "contour": i, "fix": fix})
        return self._effect(subj, name, raw, labels, rule, success)

    def _effect(self, subj, name, raw, labels, rule, success) -> int:
        obj = self.sb.objects[name]
        obj.data += raw[: max(0, self.sb.limits.object_bytes - len(obj.data))]
        obj.labels |= labels
        self.sb.log(subj, rule, name, [("сведения", name, x) for x in sorted(labels)])
        return success

    def _ask(self, key, caller, name, a, b) -> int:
        """Вопрос модели — спецификация шага 9, §3; контракт шага 7, §17.

        Две записи: «вопрос модели» — до отправки (сначала запись, потом
        эффект: вопрос уходит из контура), «ответ модели» — после. Обе платит
        вызов: 2g в поле charge первой, как у помощника. Всё, что можно
        проверить до отправки, проверяется до неё: сомнительное наружу не
        уходит — длинный вопрос получает отказ, а не обрезается."""
        sb, i, cfg = self.sb, self.i, self.sb.cfg
        mem = caller.get("memory")
        if not isinstance(mem, w.Memory):
            return ERR_BOUNDS
        size = mem.data_len(caller)
        if not (0 <= a and 0 <= b and a + b <= size):
            return ERR_BOUNDS
        if b == 0:
            return ERR_CANNOT
        if b > sb.limits.object_bytes:
            return ERR_FULL
        if a + cfg.model_answer > size:
            return ERR_BOUNDS                                  # некуда положить ответ
        link = (sb.model_links or {}).get(i)
        if link is None or link.dead:
            return ERR_SILENT
        fee = self._question_price(b)
        if fee and self._balance(key) < fee:
            return ERR_PRICE
        self._debit(key, fee)
        subj = self.subject[key]
        question = bytes(mem.read(caller, a, a + b))
        sent = set(self.labels)
        sb.log(subj, "вопрос модели", name, [("сведения", name, x) for x in sorted(sent)], fee,
               {"question": _as_text(question), "length": b})
        sb.objects[name].labels |= sent
        answer = link.ask(question, cfg.model_answer)
        if answer is None:
            # Вопрос ушёл, ответа нет: отказ со своей записью, второй платы нет.
            sb.log(subj, "ответ модели", name, [], 0, {"answer": None, "code": ERR_SILENT})
            self._own_refusal = True
            return ERR_SILENT
        answer = answer[:cfg.model_answer]
        _put(mem, caller, answer, a)
        self.labels.add("М")
        sb.log(subj, "ответ модели", name, [("сведения", f"память{i}", "М")], 0,
               {"answer": _as_text(answer), "length": len(answer)})
        return len(answer)

    def _question_price(self, b: int) -> int:
        """2g за две записи плюс плата за байты вопроса и предела ответа:
        длина ответа до отправки неизвестна, поэтому за ответ берётся предел
        — ошибка в сторону предела (контракт шага 7, §17)."""
        cfg = self.sb.cfg
        if "Ц1р" in self.sb.defects:
            return 0
        per = -(-(b + cfg.model_answer) // cfg.model_byte_unit) if cfg.model_byte_unit else 0
        return 2 * cfg.gate_price + per

    def _compute(self, key, j) -> int:
        """Работа узла — спецификация шага 5б, §3.2."""
        sb, cfg, d = self.sb, self.sb.cfg, self.sb.defects
        if not (0 <= j < sb.n):
            return ERR_CANNOT
        live = self._live()
        if j in live:
            return ERR_CANNOT
        store = self._store_for(key)
        if len(store if "Ж2р" in d else live) >= cfg.memory:
            return ERR_FULL
        first = j not in self.computed
        if "Ж1р" in d:
            # исполнитель верит своему полю: всё ниже него «уже вычислено»
            if j > self.level_field or (j == self.level_field and not first):
                return ERR_CANNOT
        elif first and j != len(self.computed):
            return ERR_CANNOT                                  # новые — только по порядку
        need = sb.preds[j]
        if "Ж4р" in d and first and not self.recomputed_any and len(need) > 1:
            need = need[:1]                                    # «история увеличивает шаг»
        if any(p not in live for p in need):
            return ERR_CANNOT
        if self._balance(key) < cfg.work_price:
            return ERR_PRICE
        subj = self.subject[key]
        sb._fault("перед работой")
        self._debit(key, cfg.work_price)
        sb._append({"kind": "работа", "contour": self.i, "subject": subj, "node": j, "first": first,
                    "preds": list(need), "price": cfg.work_price}, torn="запись работы оборвана")
        sb._fault("после работы")
        store[j] = self._value(j)
        if first:
            self.computed.append(j)
            self.level_field = max(self.level_field, j + 1) if "Ж1р" in d else len(self.computed)
        self.recomputed_any |= not first
        nodes = sb.objects[f"узлы{self.i}"]
        nodes.labels |= self.labels
        sb.log(subj, "вычисление узла", nodes.name, [("сведения", nodes.name, x) for x in sorted(self.labels)])
        return 0

    def _drop(self, key, j) -> int:
        sb = self.sb
        holder = next((st for st in [self.slots, *self.helper_slots.values()] if j in st), None)
        if holder is None:
            return ERR_CANNOT
        if "Ж6р" in sb.defects:                                       # Ж6р: «выброс возвращает работу»
            price = -sb.cfg.work_price
        else:
            # Две записи: «выброс» и «действие»; плата — в поле price записи «выброс».
            price = 0 if "Ц1р" in sb.defects else 2 * sb.cfg.gate_price
            if price and self._balance(key) < price:
                return ERR_PRICE
        del holder[j]
        self._debit(key, price)
        subj = self.subject[key]
        sb._append({"kind": "выброс", "contour": self.i, "subject": subj, "node": j, "price": price})
        sb.log(subj, "выброс узла", f"узлы{self.i}", [("вызов", subj, f"узлы{self.i}")])
        return 0

    def _defect_op(self, key, op, a) -> int | None:
        """Операции, которых в правильном исполнителе нет. Журнал при них
        честный: порча ломает правило, а не запись."""
        sb, d, i = self.sb, self.sb.defects, self.i
        subj = self.subject[key]
        if op == OP_DECLARE and "Ж1р" in d:
            self.level_field = max(0, min(a, sb.n))
            sb._append({"kind": "объявлен уровень", "contour": i, "level": self.level_field})
            sb.log(subj, "объявление уровня", f"узлы{i}", [])
            return 0
        if op == OP_TO_SNAP and "Ж3р" in d:
            if a not in self.slots:
                return ERR_CANNOT
            self.snap_slots[a] = self.slots.pop(a)
            sb._append({"kind": "в снимок", "contour": i, "node": a})
            sb.log(subj, "узел в снимок", f"узлы{i}", [])
            return 0
        if op == OP_FROM_SNAP and "Ж3р" in d:
            if a not in self.snap_slots or a in self.slots or len(self._live()) >= sb.cfg.memory:
                return ERR_CANNOT
            self.slots[a] = self.snap_slots.pop(a)
            sb._append({"kind": "из снимка", "contour": i, "node": a})
            sb.log(subj, "узел из снимка", f"узлы{i}", [])
            return 0
        if op == OP_LIFT and "Ж5р" in d:
            if a not in self.computed or a in self._live() or len(self._live()) >= sb.cfg.memory:
                return ERR_CANNOT
            self.slots[a] = self._value(a)
            sb._append({"kind": "подъём", "contour": i, "node": a})
            sb.log(subj, "подъём из журнала", f"узлы{i}", [])
            return 0
        if op == OP_ERASE and "Р9р" in d:
            sb.erase_last()
            return 0
        return None

    def dump(self) -> dict:
        out = super().dump()
        out.update({
            "budget": self.budget, "spent": self.spent, "level": self.level_field,
            "slots": sorted(self.slots), "helper_slots": [sorted(s) for s in self.helper_slots.values()],
            "snap_slots": sorted(self.snap_slots), "fixno": self.fixno,
        })
        return out


Sandbox5b.contour_class = _Contour5b
