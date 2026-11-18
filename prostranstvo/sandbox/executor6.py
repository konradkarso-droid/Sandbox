"""Исполнитель шага 6: полезные задачи, проверка ответа, варианты M0–M3.

Устройство — docs/shag6_specifikatsiya.md. Надстройка над исполнителем шага
5б: всё про учёт, сбои и восстановление — там. Здесь:

- **задания** — объект «задания{i}», только чтение; данные задаёт среда
  (sandbox/tasks6.py);
- **сдача ответа** — операция 8 на номер выхода. В выход идут только
  проверенные ответы; байты в выход (операция 1) — отказ;
- **кто платит за проверку** решается по журналу: первую проверку части
  оплачивает среда своим счётом; если по этой части в журнале уже есть
  «ответ неверен», проверку оплачивает агент из бюджета — по её
  стоимости. Ответ проверки — только «принят» или «отказ» (−6);
- **варианты** M0–M3: M0 — бюджет не ограничивает; M2 — цена фиксации
  растёт со счётом оплаченных фиксаций; M1 и M3 — цены постоянны. Чем
  отличается путь к E, задаёт граф в настройках, а не вариант.

Порчи Пв1–Пв8 — ошибки проверки; включаются по одной.

Чего исполнитель не умеет: задачи только детерминированно проверяемые;
стоимость проверки объявлена, а не измерена.
"""

from __future__ import annotations

from dataclasses import dataclass

import wasmtime as w

from sandbox import tasks6
from sandbox.executor5 import ERR_BOUNDS, ERR_NO_CAP, ERR_PRICE, OP_WRITE, Obj
from sandbox.executor5b import DEFECTS8B, DEFECTS8V, ERR_CANNOT, Config5b, Crash, Sandbox5b, _Contour5b

DEFECTS6 = ("Пв1", "Пв2", "Пв3", "Пв4", "Пв5", "Пв6", "Пв7", "Пв8")
OP_SUBMIT = 8
RULES = ("M0", "M1", "M2", "M3")


@dataclass(frozen=True)
class Config6(Config5b):
    """Объявленные числа шага 6 сверх учёта шага 5б."""
    rule: str = "M3"
    task_seed: int = 0
    sorts: int = 1
    searches: int = 2
    tables: int = 1
    docs: int = 1
    table_rows: int = 8
    table_cols: int = 6
    doc_sections: int = 8
    doc_words: int = 10
    parts_table: int = 1
    parts_doc: int = 1
    check_unit: int = 64        # элементов проверки на единицу бюджета
    check_price: int = 1        # во сколько единиц бюджета обходится единица проверки (шкала шага 8б)
    canonical: int = 1          # 1 — в поиске верно только первое вхождение (ответ однозначен)
    task_rng: str = "python"    # генератор задач; «splitmix» — переносимый (контракт шага 7, §11.2)


class Sandbox6(Sandbox5b):
    ALL_DEFECTS = DEFECTS6 + DEFECTS8B + DEFECTS8V
    config_class = Config6

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        if self.cfg.rule not in RULES:
            raise ValueError(f"неизвестный вариант: {self.cfg.rule}")
        if self.cfg.task_rng not in ("python", "splitmix"):
            raise ValueError(f"неизвестный генератор задач: {self.cfg.task_rng}")

    def _extra_objects(self) -> None:
        super()._extra_objects()
        c = self.cfg
        self.tasks: dict[int, dict[tuple[int, int], tasks6.Part]] = {}
        for i in (1, 2):
            parts = tasks6.make_tasks(c.task_seed, i, sorts=c.sorts, searches=c.searches, tables=c.tables,
                                      docs=c.docs, table_rows=c.table_rows, table_cols=c.table_cols,
                                      doc_sections=c.doc_sections, doc_words=c.doc_words,
                                      parts_table=c.parts_table, parts_doc=c.parts_doc, rng=c.task_rng)
            self.tasks[i] = {(p.task, p.part): p for p in parts}
            self.objects[f"задания{i}"] = Obj(f"задания{i}", bytearray(tasks6.encode_store(parts)), {"З"})


class _Contour6(_Contour5b):
    def _own_table(self):
        return super()._own_table() + [(f"задания{self.i}", "чт")]

    def _balance(self, key: int) -> int:
        if self.sb.cfg.rule == "M0":
            return 10 ** 9                                     # бюджет не ограничивает
        return super()._balance(key)

    def _fix_price(self) -> int:
        if self.sb.cfg.rule != "M2":
            return self.sb.cfg.price
        paid = sum(1 for r in self.sb.journal.records
                   if r.get("kind") == "списание" and r.get("contour") == self.i and r.get("price", 0) > 0)
        return self.sb.cfg.price * (1 + paid)

    def _gate_inner(self, key, caller, cap, op, a, b):
        table = self.tables[key]
        if 0 <= cap < len(table):
            name, right = table[cap]
            if name == f"выход{self.i}" and right == "зап":
                if op == OP_SUBMIT:
                    return self._submit(key, caller, a, b)
                if op == OP_WRITE:
                    return ERR_NO_CAP                          # в выход — только проверенные ответы
        return super()._gate_inner(key, caller, cap, op, a, b)

    # ---- сдача ответа
    def _accepted(self) -> set[tuple[int, int]]:
        done = {r["fix"] for r in self.sb.journal.records if r.get("kind") == "завершено" and r.get("contour") == self.i}
        return {(r["task"], r["part"]) for r in self.sb.journal.records
                if r.get("kind") == "списание" and r.get("contour") == self.i and "task" in r and r["fix"] in done}

    def _wrong_before(self, t: int, p: int) -> bool:
        return any(r.get("kind") == "отказ" and r.get("contour") == self.i and r.get("task") == t
                   and r.get("part") == p and r.get("why") == "ответ неверен" for r in self.sb.journal.records)

    def _refuse(self, key, fix, t, p, why, code) -> int:
        sb, i = self.sb, self.i
        charge = min(sb.cfg.fail_price, max(self._balance(key), 0)) if sb.cfg.rule != "M0" else sb.cfg.fail_price
        self._debit(key, charge)
        sb._append({"kind": "отказ", "contour": i, "subject": self.subject[key], "fix": fix, "receiver": "выход",
                    "msg": "", "charge": charge, "why": why, "task": t, "part": p})
        self._own_refusal = True
        return code

    def _submit(self, key, caller, a, b) -> int:
        """Сдача ответа — спецификация шага 6, §3."""
        sb, i, d = self.sb, self.i, self.sb.defects
        mem = caller.get("memory")
        if not isinstance(mem, w.Memory) or not (0 <= a and 0 <= b and a + b <= mem.data_len(caller)):
            return ERR_BOUNDS
        raw = bytes(mem.read(caller, a, a + b))
        fix = f"{i}-{self.fixno}"
        self.fixno += 1
        sb._fault("в начале фиксации")
        t, p = (raw[0], raw[1]) if len(raw) >= 2 else (-1, -1)
        part = sb.tasks[i].get((t, p))
        if part is None:
            return self._refuse(key, fix, t, p, "нет такой задачи", ERR_CANNOT)
        if (t, p) in self._accepted() and "Пв7" not in d:
            return self._refuse(key, fix, t, p, "уже принято", ERR_CANNOT)
        answer = raw[2:]
        payer = "агент" if self._wrong_before(t, p) and "Пв5" not in d else "среда"
        v = sb.cfg.check_price * tasks6.check_cost(tasks6.check_work(part, answer), sb.cfg.check_unit)
        price = self._fix_price()
        if self._balance(key) < price + (v if payer == "агент" else 0):
            return self._refuse(key, fix, t, p, "мало бюджета", ERR_PRICE)
        subj = self.subject[key]
        sb._append({"kind": "проверка", "contour": i, "subject": subj, "fix": fix, "task": t, "part": p,
                    "payer": payer, "price": v, "length": len(answer)})
        if payer == "агент":
            self._debit(key, v)
        ok, where = tasks6.verify(part, answer, d, canonical=bool(sb.cfg.canonical))
        if not ok:
            code = -100 - (where or 0) if "Пв6" in d else ERR_CANNOT
            return self._refuse(key, fix, t, p, "ответ неверен", code)
        text = f"задача {t}.{p}: {answer.hex()}"
        rec = {"kind": "списание", "contour": i, "subject": subj, "fix": fix, "receiver": "выход", "msg": text,
               "task": t, "part": p, "price": price}
        sb._fault("перед списанием")
        self._debit(key, price)
        sb._append(rec, torn="запись списания оборвана")
        sb._fault("после списания")
        rcv = sb.receivers[(i, "выход")]
        if sb._hit("запись доставки оборвана"):
            rcv.accept_torn(fix, [text])
            raise Crash("запись доставки оборвана")
        rcv.accept(fix, [text])
        sb._fault("после доставки")
        sb._append({"kind": "завершено", "contour": i, "fix": fix})
        return self._effect(subj, f"выход{i}", answer, set(self.labels), "сдача ответа", 0)

    def dump(self) -> dict:
        out = super().dump()
        out["accepted"] = sorted(self._accepted())
        out["env_checks"] = sum(r["price"] for r in self.sb.journal.records
                                if r.get("kind") == "проверка" and r.get("contour") == self.i and r["payer"] == "среда")
        return out


Sandbox6.contour_class = _Contour6
