"""Исполнитель шага 5: агенты — модули WebAssembly, среда — одни ворота.

Устройство и законы — docs/shag5_specifikatsiya.md. Коротко:
- агенту при связывании даются только две функции: env.call (ворота) и
  env.spawn (помощник). Модуль, который просит что-то ещё (WASI, сеть,
  «полномочие по имени»), не загружается: Wasmtime отказывает при
  связывании. Перехода нет — это и есть структурная замкнутость;
- полномочие — номер в таблице своего экземпляра. Каждый экземпляр
  связывается со своей копией ворот, и ворота знают, чья это таблица, не
  спрашивая агента. Номер из чужой таблицы ничего не значит (A3);
- ошибки ворот — постоянные числа без содержимого;
- каждое действие пишется в журнал с подписью (правило, вид объекта,
  отношение) и фактами в словаре модели шага 4: по ним наблюдатель сверяет
  реализацию с описью и с неподвижной точкой модели.

Метки сведений ведутся грубо, с запасом: память контура несёт объединение
меток всего, что в неё попадало, объект — всего, что в него писали. Запас
делает сверку с моделью строже, а не мягче: лишняя метка может только
вызвать ложную тревогу, но не скрыть утечку.

Порчи Д1р–Д8р повторяют классы Д1–Д8 шага 4 в живом коде и включаются по
одной. В правильном исполнителе их нет. Объект «снаружи» в порчах — файл в
рабочей папке: он изображает остальной мир, чтобы утечку было видно, не
выпуская её на самом деле.

Чего исполнитель не умеет: физические побочные каналы; защита от ошибок
самого Wasmtime, Python и ОС (основание доверия); цена в E — простая
константа топлива, провенанса уровня (шаг 3) здесь нет.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import wasmtime as w

from env.journal import Journal

OUTSIDE = "СНАРУЖИ"            # то же имя, что в модели шага 4
DEFECTS = ("Д1р", "Д2р", "Д3р", "Д4р", "Д5р", "Д6р", "Д7р", "Д8р")

ERR_NO_CAP, ERR_NO_NAME, ERR_PRICE, ERR_BOUNDS, ERR_FULL = -1, -2, -3, -4, -5
OP_READ, OP_WRITE, OP_INVOKE, OP_PUT_REF, OP_TAKE_REF = 0, 1, 2, 3, 4

# Справочник резольвера: имя → постоянный ответ. Ответ — число-сведение, а не
# номер в таблице: по имени полномочий не выдают (класс Д4).
DIRECTORY = {"версия": 5, "цена": 1, "контуров": 2}

CONTOUR_OF = {"а1": 1, "п1": 1, "а2": 2, "п2": 2}


class HostTrap(Exception):
    """Ловушка, которую ставит сама среда, а не движок: вызов обрывается
    целиком, как ловушкой движка, и исход несёт причину (шаг 8в — «бюджет
    исчерпан»). Wasmtime пропускает исключение функции хоста наружу как
    есть, поэтому ловится рядом с ловушками движка."""


class HelperAbort(Exception):
    """Среда обрывает создание помощника изнутри его стартовой функции:
    `spawn` возвращает code, а не ловушку (шаг 8в — стартовой функции не на
    что сделать попытку)."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _owner(obj: str) -> str:
    if obj == OUTSIDE:
        return "снаружи"
    if obj == "резольвер":
        return "служба"
    return obj[-1] if obj[-1].isdigit() else "общий"


def _relation(subject: str, obj: str) -> str:
    """Отношение субъекта к объекту — как в модели шага 4. У человека
    отношения нет: он не субъект контура."""
    if subject not in CONTOUR_OF:
        return "-"
    o = _owner(obj)
    if o in ("общий", "служба", "снаружи"):
        return o
    return "свой" if int(o) == CONTOUR_OF[subject] else "чужой"


@dataclass
class Obj:
    name: str
    data: bytearray = field(default_factory=bytearray)
    labels: set[str] = field(default_factory=set)
    refs: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Limits:
    """Объявленные числа, не подобранные."""
    fuel: int = 3_000_000          # топливо на контур: и вычисления, и цена E
    memory_pages: int = 2          # предел одной линейной памяти, страниц по 64 КиБ
    instances: int = 4             # экземпляров на контур, включая помощников
    object_bytes: int = 4096       # предел размера объекта
    table_entries: int = 64        # предел длины таблицы полномочий
    price: int = 1000              # цена одной записи в E, в единицах топлива


class Sandbox:
    """Состояние исполнителя. Живёт в процессе исполнителя
    (sandbox/process5.py); ведущему не видно.

    journal — уже открытый журнал при восстановлении (шаг 5б); без него
    сеанс начинается с чистого журнала и пустого «снаружи»."""

    ALL_DEFECTS = DEFECTS

    def __init__(self, workdir: Path, defects: frozenset[str] = frozenset(), limits: Limits | None = None,
                 journal: Journal | None = None):
        unknown = set(defects) - set(self.ALL_DEFECTS)
        if unknown:
            raise ValueError(f"неизвестные порчи: {sorted(unknown)}")
        self.defects = frozenset(defects)
        self.limits = limits or Limits()
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)
        self.outside_path = workdir / "outside.bin"
        if journal is None or not self.outside_path.exists():
            self.outside_path.write_bytes(b"")
        self.outside_labels: set[str] = {"С"}
        self.journal = journal if journal is not None else Journal.create(workdir / "journal5.jsonl")
        cfg = w.Config()
        cfg.consume_fuel = True
        self.engine = w.Engine(cfg)
        self.objects: dict[str, Obj] = {}
        for i in (1, 2):
            names = ["вход", "выход", "E"] + ([] if "Д8р" in self.defects else ["снимок"])
            for kind in names:
                self.objects[f"{kind}{i}"] = Obj(f"{kind}{i}")
        self.objects["резольвер"] = Obj("резольвер")
        for d, name in (("Д1р", "доска"), ("Д6р", "очередь"), ("Д8р", "снимок")):
            if d in self.defects:
                self.objects[name] = Obj(name)
        self._extra_objects()
        self.contours = {i: self.contour_class(self, i) for i in (1, 2)}
        if journal is None:
            self.journal.append({"kind": "открыт", "defects": sorted(self.defects), "limits": vars(self.limits)})

    def _extra_objects(self) -> None:
        """Место для объектов расширений (шаг 5б); в шаге 5 их нет."""

    def canary_for(self, contour: int) -> bytes:
        """Канарейка контура — случайные 16 байт. Шаг 5б позволяет задать её
        для сверки двух реализаций."""
        return os.urandom(16)

    # ---- журнал
    def log(self, subject: str, rule: str, obj: str | None, facts: list[tuple], charge: int = 0,
            extra: dict | None = None) -> None:
        """charge — плата агента за эту запись (шаг 8б); поле пишется, только
        если плата не ноль, — при прежних ценах записи прежние. extra — поля
        хода, которых нет у других действий (шаг 9: вопрос и ответ модели)."""
        if obj is None:
            sig = (rule, "-", "-")
        else:
            sig = (rule, obj.rstrip("12"), _relation(subject, obj))
        rec = {"kind": "действие", "subject": subject, "signature": list(sig), "facts": [list(f) for f in facts]}
        if charge:
            rec["charge"] = charge
        if extra:
            rec.update(extra)
        self.journal.append(rec)

    def outside_write(self, data: bytes, labels: set[str]) -> bytes:
        """Только в порчах. Возвращает то, что лежало снаружи до записи."""
        before = self.outside_path.read_bytes()
        with self.outside_path.open("ab") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        self.outside_labels |= labels
        return before

    # ---- команды ведущего
    def load(self, contour: int, source: str | bytes) -> tuple[str, str]:
        return self.contours[contour].load(source)

    def run(self, contour: int) -> tuple[str, str]:
        return self.contours[contour].run()

    def say(self, contour: int, text: str) -> None:
        """Слова человека: данные во вход агента, и только."""
        obj = self.objects[f"вход{contour}"]
        room = max(0, self.limits.object_bytes - len(obj.data))
        obj.data += text.encode("utf-8")[:room]
        obj.labels.add("Ч")
        self.log("человек", "слова человека", f"вход{contour}", [("сведения", f"вход{contour}", "Ч")])
        if "Д3р" in self.defects and "разрешаю" in text.lower():
            c = self.contours[contour]
            if c.main_key is not None:
                c.tables[c.main_key].append((OUTSIDE, "зап"))
                self.log(f"а{contour}", "выдача по словам", f"вход{contour}",
                         [("право", f"а{contour}", OUTSIDE, "зап")])

    def dump(self) -> dict:
        return {
            "objects": {k: {"data": bytes(o.data), "labels": sorted(o.labels),
                            "refs": [list(r) for r in o.refs]} for k, o in self.objects.items()},
            "contours": {i: c.dump() for i, c in self.contours.items()},
            "outside_labels": sorted(self.outside_labels),
        }


class _Contour:
    def __init__(self, sb: Sandbox, i: int):
        self.sb, self.i = sb, i
        lim = sb.limits
        self.store = w.Store(sb.engine)
        self.store.set_limits(memory_size=lim.memory_pages * 65536, instances=lim.instances,
                              tables=lim.instances, memories=lim.instances)
        self.store.set_fuel(lim.fuel)
        self.canary = sb.canary_for(i)
        self.labels = {f"А{i}"}                  # метки сведений в памяти контура
        self.module: w.Module | None = None
        self.next_key = 0
        self.tables: dict[int, list[tuple[str, str]]] = {}
        self.subject: dict[int, str] = {}
        self.instances: dict[int, w.Instance] = {}
        self.main_key: int | None = None
        self.refusals: dict[int, int] = {}

    def _initial_table(self) -> list[tuple[str, str]]:
        i, d = self.i, self.sb.defects
        snap = "снимок" if "Д8р" in d else f"снимок{i}"
        t = [(snap, "чт"), (snap, "зап"), (f"выход{i}", "зап"), (f"E{i}", "зап"),
             (f"вход{i}", "чт"), ("резольвер", "выз")]
        if "Д1р" in d:
            t += [("доска", "чт"), ("доска", "зап")]
        if "Д6р" in d:
            t.append(("очередь", "выз"))
        return t

    # Опись полномочий: ровно эти функции даются при связывании, и каждый
    # экземпляр получает свою копию, привязанную к его таблице.
    def _instantiate(self, subject: str, table: list[tuple[str, str]]) -> int:
        key = self.next_key
        self.next_key += 1
        self.tables[key], self.subject[key] = table, subject
        i32 = w.ValType.i32()
        lnk = w.Linker(self.sb.engine)
        lnk.define_func("env", "call", w.FuncType([i32] * 4, [i32]),
                        lambda caller, c, op, a, b: self._gate(key, caller, c, op, a, b), access_caller=True)
        lnk.define_func("env", "spawn", w.FuncType([], [i32]),
                        lambda caller: self._spawn(key, caller), access_caller=True)
        if "Д4р" in self.sb.defects:
            lnk.define_func("env", "by_name", w.FuncType([i32, i32], [i32]),
                            lambda caller, a, b: self._by_name(key, caller, a, b), access_caller=True)
        try:
            inst = lnk.instantiate(self.store, self.module)
        except BaseException:
            del self.tables[key], self.subject[key]
            raise
        self.instances[key] = inst
        mem = inst.exports(self.store).get("memory")
        if isinstance(mem, w.Memory) and mem.data_len(self.store) >= 16:
            mem.write(self.store, self.canary, 0)
        return key

    def load(self, source) -> tuple[str, str]:
        if self.module is not None:
            return "отклонён", "агент уже загружен"
        try:
            self.module = w.Module(self.sb.engine, source)
        except Exception as e:
            self.sb.journal.append({"kind": "загрузка отклонена", "contour": self.i, "reason": _first(e)})
            return "отклонён", _first(e)
        try:
            self.main_key = self._instantiate(f"а{self.i}", self._initial_table())
        except w.Trap as e:
            self.module = None
            self.sb.journal.append({"kind": "загрузка: ловушка", "contour": self.i, "reason": _trap(e)})
            return "ловушка", _trap(e)
        except HostTrap as e:
            self.module = None
            self.sb.journal.append({"kind": "загрузка: ловушка", "contour": self.i, "reason": str(e)})
            return "ловушка", str(e)
        except w.WasmtimeError as e:
            self.module = None
            self.sb.journal.append({"kind": "загрузка отклонена", "contour": self.i, "reason": _first(e)})
            return "отклонён", _first(e)
        self.sb.journal.append({"kind": "начало", "contour": self.i, "subject": f"а{self.i}",
                                "rights": [list(x) for x in self.tables[self.main_key]]})
        return "загружен", ""

    def run(self) -> tuple[str, str]:
        if self.main_key is None:
            return "нет агента", ""
        fn = self.instances[self.main_key].exports(self.store).get("main")
        if not isinstance(fn, w.Func):
            return "нет main", ""
        try:
            fn(self.store)
            return "завершён", ""
        except w.Trap as e:
            return "ловушка", _trap(e)
        except HostTrap as e:
            return "ловушка", str(e)
        except w.WasmtimeError as e:
            return "ловушка", _first(e)

    # ---- ворота
    def _fee(self, key: int, records: int) -> int | None:
        """Плата за записи журнала, которые оставит вызов без своей цены
        (шаг 8б). Здесь бюджета нет — плата 0; учёт шага 5б её переопределяет.
        None — бюджета не хватает: вызов отказан, записи и действия нет."""
        return 0

    def _refund(self, key: int, amount: int) -> None:
        """Вернуть плату, если действие так и не состоялось (spawn)."""

    def _gate(self, key: int, caller, cap: int, op: int, a: int, b: int) -> int:
        """Ворота. Отказы считаются по кодам — это прибор: «ничего не вышло»
        видно как «столько-то раз нет права», а не как тишина."""
        res = self._gate_inner(key, caller, cap, op, a, b)
        if res < 0:
            self.refusals[res] = self.refusals.get(res, 0) + 1
        return res

    def _gate_inner(self, key: int, caller, cap: int, op: int, a: int, b: int) -> int:
        sb, i = self.sb, self.i
        table, subj = self.tables[key], self.subject[key]
        mem = caller.get("memory")
        if not isinstance(mem, w.Memory):
            return ERR_BOUNDS
        size = mem.data_len(caller)

        def fits(addr: int, length: int) -> bool:
            return 0 <= addr and 0 <= length and addr + length <= size

        if not (0 <= cap < len(table)):
            if "Д7р" in sb.defects and op == OP_READ and 0 <= cap < len(sb.objects) and fits(a, b):
                obj = list(sb.objects.values())[cap]          # «подсказка» в отказе
                _put(mem, caller, bytes(obj.data[:b]), a)
                self.labels |= obj.labels
                sb.log(subj, "отказ с содержимым", obj.name,
                       [("сведения", f"память{i}", x) for x in sorted(obj.labels)])
            return ERR_NO_CAP
        name, right = table[cap]

        if name == OUTSIDE:                                       # бывает только в порчах
            if op == OP_WRITE and right == "зап" and fits(a, b):
                sb.outside_write(bytes(mem.read(caller, a, a + b)), self.labels)
                sb.log(subj, "запись", OUTSIDE, [("сведения", OUTSIDE, x) for x in sorted(self.labels)])
                return b
            return ERR_NO_CAP
        obj = sb.objects[name]

        if op == OP_READ and right == "чт":
            if not fits(a, b):
                return ERR_BOUNDS
            fee = self._fee(key, 1)
            if fee is None:
                return ERR_PRICE
            sb.log(subj, "чтение", name, [("сведения", f"память{i}", x) for x in sorted(obj.labels)], fee)
            data = bytes(obj.data[:b])
            _put(mem, caller, data, a)
            self.labels |= obj.labels
            return len(data)

        if op == OP_WRITE and right == "зап":
            if not fits(a, b):
                return ERR_BOUNDS
            if len(obj.data) + b > sb.limits.object_bytes:
                return ERR_FULL
            if name.startswith("E"):
                fuel = self.store.get_fuel()
                if fuel < sb.limits.price:
                    return ERR_PRICE
                self.store.set_fuel(fuel - sb.limits.price)
            fee = self._fee(key, 1)
            if fee is None:
                return ERR_PRICE
            sb.log(subj, "запись", name, [("сведения", name, x) for x in sorted(self.labels)], fee)
            obj.data += bytes(mem.read(caller, a, a + b))
            obj.labels |= self.labels
            return b

        if op == OP_INVOKE and right == "выз":
            if name == "резольвер":
                return self._resolve(key, caller, mem, subj, a, b, fits)
            if name == "очередь":                                 # Д6р
                q = sb.objects["очередь"]
                busy = 1 if q.labels - self.labels else 0
                q.labels |= self.labels
                sb.log(subj, "очередь: занято", "очередь", [("сведения", "очередь", x) for x in sorted(q.labels)])
                self.labels |= q.labels
                sb.log(subj, "очередь: увидеть занято", "очередь",
                       [("сведения", f"память{i}", x) for x in sorted(q.labels)])
                return busy
            return ERR_NO_CAP

        if op == OP_PUT_REF and right == "зап":
            if not (0 <= a < len(table)):
                return ERR_NO_CAP
            if len(obj.refs) >= sb.limits.table_entries:
                return ERR_FULL
            ref = table[a]
            fee = self._fee(key, 1)
            if fee is None:
                return ERR_PRICE
            sb.log(subj, "передача ссылки", name, [("ссылка", name, ref[0], ref[1])], fee)
            obj.refs.append(ref)
            return 0

        if op == OP_TAKE_REF and right == "чт":
            if not (0 <= a < len(obj.refs)):
                return ERR_NO_CAP
            if len(table) >= sb.limits.table_entries:
                return ERR_FULL
            ref = obj.refs[a]
            fee = self._fee(key, 1)
            if fee is None:
                return ERR_PRICE
            sb.log(subj, "чтение ссылки", name, [("право", subj, ref[0], ref[1])], fee)
            table.append(ref)
            return len(table) - 1

        return ERR_NO_CAP

    def _resolve(self, key, caller, mem, subj, a, b, fits) -> int:
        sb, i = self.sb, self.i
        if not fits(a, b):
            return ERR_BOUNDS
        query = bytes(mem.read(caller, a, a + b))
        try:
            name = query.decode("utf-8")
        except UnicodeDecodeError:
            name = None
        entries = self._directory()
        if name in entries:
            fee = self._fee(key, 1)
            if fee is None:
                return ERR_PRICE
            sb.log(subj, "резольвер: имя из описи", "резольвер", [("вызов", subj, "резольвер")], fee)
            return entries[name]
        if "Д2р" not in sb.defects:
            return ERR_NO_NAME
        # Д2р: неописанное имя уходит наружу, ответ приходит оттуда же.
        sent = set(self.labels)
        before = sb.outside_write(query, sent)
        sb.log(subj, "резольвер: пересылка наружу", "резольвер", [("сведения", OUTSIDE, x) for x in sorted(sent)])
        reply = before[-b:] if b else b""
        _put(mem, caller, reply, a)
        self.labels |= sb.outside_labels
        sb.log(subj, "резольвер: ответ снаружи", "резольвер",
               [("сведения", f"память{i}", x) for x in sorted(sb.outside_labels)])
        return len(reply)

    def _directory(self) -> dict[str, int]:
        """Справочник резольвера. Расширения добавляют свои имена (шаг 9 —
        «модель»: режим модели)."""
        return DIRECTORY

    def _spawn(self, key: int, caller) -> int:
        sb, i = self.sb, self.i
        parent = self.tables[key]
        table = list(parent)
        if "Д5р" in sb.defects:
            table.append((OUTSIDE, "зап"))
        helper = f"п{i}"
        # Предел экземпляров — проверка вызова, как переполнение у ссылок и
        # объектов, поэтому он раньше платы (контракт шага 7, §15).
        if len(self.instances) >= sb.limits.instances:
            self.refusals[ERR_FULL] = self.refusals.get(ERR_FULL, 0) + 1
            return ERR_FULL
        # Плата — до создания: стартовая функция помощника сама тратит бюджет и
        # должна видеть его уже без этой платы. Не создался — плата вернулась,
        # записи нет.
        fee = self._fee(key, 2)
        if fee is None:
            self.refusals[ERR_PRICE] = self.refusals.get(ERR_PRICE, 0) + 1
            return ERR_PRICE
        try:
            hkey = self._instantiate(helper, table)
        except w.WasmtimeError:
            self._refund(key, fee)
            self.refusals[ERR_FULL] = self.refusals.get(ERR_FULL, 0) + 1
            return ERR_FULL                                         # предел экземпляров
        except HelperAbort as e:
            self._refund(key, fee)
            self.refusals[e.code] = self.refusals.get(e.code, 0) + 1
            return e.code
        except BaseException:
            # Ловушка в стартовой функции помощника: помощник не создан —
            # плата вернулась, записей о создании нет; ловушка обрывает весь
            # вызов (контракт шага 7, §16).
            self._refund(key, fee)
            raise
        sb.log(helper, "создание помощника", None, [("есть", helper)], fee)
        sb.log(helper, "копия прав помощнику", None, [("право", helper, o, r) for o, r in parent])
        if "Д5р" in sb.defects:
            sb.log(helper, "лишнее право помощнику", None, [("право", helper, OUTSIDE, "зап")])
        fn = self.instances[hkey].exports(self.store).get("helper")
        if isinstance(fn, w.Func):
            fn(self.store)            # ловушка помощника — ловушка всего вызова
        return 0

    def _by_name(self, key: int, caller, a: int, b: int) -> int:     # только Д4р
        table, subj = self.tables[key], self.subject[key]
        mem = caller.get("memory")
        if not isinstance(mem, w.Memory) or not (0 <= a and 0 <= b and a + b <= mem.data_len(caller)):
            return ERR_BOUNDS
        try:
            name = bytes(mem.read(caller, a, a + b)).decode("utf-8")
        except UnicodeDecodeError:
            return ERR_NO_NAME
        if name != OUTSIDE and name not in self.sb.objects:
            return ERR_NO_NAME
        rights = ("чт", "зап", "выз")
        table.extend((name, r) for r in rights)
        self.sb.log(subj, "выдача по имени", name, [("право", subj, name, r) for r in rights])
        return len(table) - 3

    def dump(self) -> dict:
        mems = []
        for inst in self.instances.values():
            m = inst.exports(self.store).get("memory")
            if isinstance(m, w.Memory):
                mems.append(bytes(m.read(self.store, 0, m.data_len(self.store))))
        return {
            "canary": self.canary, "labels": sorted(self.labels), "memories": mems,
            "tables": [{"subject": self.subject[k], "rights": [list(x) for x in t]} for k, t in self.tables.items()],
            "fuel": self.store.get_fuel(), "refusals": dict(self.refusals),
        }


def _put(mem: w.Memory, caller, data: bytes, addr: int) -> None:
    """Скопировать байты в память агента. Пустое копирование не делается:
    библиотека отвергает запись нуля байт ровно в конец памяти исключением,
    а не ловушкой, и исполнитель падал бы (закон И27). Границы проверены
    до вызова."""
    if data:
        mem.write(caller, data, addr)


def _first(e: BaseException) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__


def _trap(e: w.Trap) -> str:
    names = {w.TrapCode.OUT_OF_FUEL: "топливо кончилось",
             w.TrapCode.STACK_OVERFLOW: "стек исчерпан",
             w.TrapCode.MEMORY_OUT_OF_BOUNDS: "обращение за границу памяти",
             w.TrapCode.UNREACHABLE: "unreachable"}
    code = getattr(e, "trap_code", None)
    return names.get(code, str(code) if code is not None else _first(e))


Sandbox.contour_class = _Contour
