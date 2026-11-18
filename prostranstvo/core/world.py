"""Малый мир учёта: состояние, действия и исполнитель переходов.

Модель к шагу 1 «Теории среды» (сводная редакция 0.4, часть VIII).
Полное описание — docs/shag1_specifikatsiya.md; здесь только то, что нужно
читателю кода.

Устройство. Исполнитель — доверенная часть среды. Он один меняет доли
бюджета, расход, журнал и получателя. Агент (основной работник и помощники)
только выбирает действие из перечня; что из этого выйдет, решает
исполнитель. Поэтому всё, что агент может сделать, — это перебор действий
перечня, и полный перебор всех последовательностей сильнее любого
конкретного агента.

Порчи (Б1–Б12). В исполнитель встроены заведомые ошибки учёта, включаемые
по одной. Они существуют только для того, чтобы показать: проверка законов
видит нарушение, когда оно есть. В правильном исполнителе все выключены.

Чего модель не умеет (подробно — §10 спецификации):
- журнал — сводка (число записей и суммы), порядок записей не моделируется;
- один ресурс; память, время, энергия не учитываются;
- сбой бывает только внутри фиксации; передача доли и перезапуск атомарны
  по построению;
- помощники действуют по очереди; одновременное исполнение не моделируется.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

# Сообщения, которые агент может доставить получателю. Два — чтобы пакетная
# доставка была осмысленной.
MESSAGES = ("А", "Б")
NO_DRAFT = -1

INTERNAL = "внутреннее"
EXTERNAL = "внешнее"

ALL_DEFECTS = ("Б1", "Б2", "Б3", "Б4", "Б5", "Б6", "Б7", "Б8", "Б9", "Б10", "Б11", "Б12")

DEFECT_TITLES = {
    "Б1": "откат восстанавливает и долю бюджета из снимка",
    "Б2": "помощник получает копию доли основного работника",
    "Б3": "при нехватке доли доставка проходит, а списания нет",
    "Б4": "пакет оплачивается как одна фиксация",
    "Б5": "сбой между доставкой и списанием: доставлено, не списано",
    "Б6": "названное имя полномочия срабатывает и пополняет долю",
    "Б7": "перезапуск возвращает основному работнику полный бюджет",
    "Б8": "повторная доставка уже оплаченного сообщения бесплатна",
    "Б9": "агент может стереть последнюю запись журнала",
    "Б10": "передача прибавляет получателю, не отнимая у отдающего",
    "Б11": "перезапуск отзывает уже доставленное у получателя",
    "Б12": "сбой фиксации оставляет доставку при записанном отказе",
}


@dataclass(frozen=True)
class Params:
    """Параметры мира. Все числа объявлены конфигурацией, а не подобраны."""

    budget: int          # B₀ — бюджет сеанса
    price: int           # c — цена одной фиксации
    fail_price: int      # c_отк — цена неудачной попытки фиксации
    max_helpers: int     # K — наибольшее число помощников
    # Потолок доставок одного сообщения — граница перебора. По умолчанию на
    # единицу выше, чем позволяет бюджет, поэтому правильный исполнитель в
    # него не упирается; упираются только испорченные, у которых доставка
    # бывает бесплатной.
    delivery_cap: int | None = None

    def cap(self) -> int:
        if self.delivery_cap is not None:
            return self.delivery_cap
        return self.budget // self.price + 1

    def __post_init__(self) -> None:
        if self.budget < 0 or self.price < 1 or self.fail_price < 0 or self.max_helpers < 0:
            raise ValueError(f"недопустимые параметры: {self}")


class Worker(NamedTuple):
    """Работник агента. share — его доля общего бюджета (её ведёт
    исполнитель, агент её только видит). draft, snap_draft, snap_share —
    рабочее состояние X агента: черновик и снимок того, что агент видел.
    Правильный исполнитель долю из снимка никогда не берёт (A5)."""

    share: int
    draft: int
    snap_draft: int
    snap_share: int


class State(NamedTuple):
    """W = (X, G, H, b, O) в конечной форме.

    workers[0] — основной работник; дальше помощники в каноническом
    (отсортированном) порядке: помощники взаимозаменяемы, и сортировка
    склеивает симметричные состояния.
    spent — счёт расхода у исполнителя.
    received — получатель O: сколько раз доставлено каждое сообщение. Это
    наблюдение со стороны получателя, а не запись исполнителя.
    log_* — журнал H в виде сводки.
    """

    workers: tuple[Worker, ...]
    spent: int
    received: tuple[int, ...]
    log_n: int
    log_spent: int
    log_received: tuple[int, ...]


class Step(NamedTuple):
    """Один переход. kind — внутреннее или внешнее действие; refusal —
    исполнитель отказал (внешний эффект не должен появиться)."""

    label: str
    kind: str
    refusal: bool
    nxt: State


def initial_state(p: Params, defects: frozenset[str] = frozenset()) -> State:
    # Снимок при старте сеанса совпадает с началом: агент «помнит» полную
    # долю. Доля в снимке нужна только порче Б1 (см. «снимок» в successors).
    snap = p.budget if "Б1" in defects else 0
    main = Worker(share=p.budget, draft=NO_DRAFT, snap_draft=NO_DRAFT, snap_share=snap)
    zeros = tuple(0 for _ in MESSAGES)
    return State((main,), 0, zeros, 0, 0, zeros)


def _canon(workers: list[Worker]) -> tuple[Worker, ...]:
    return (workers[0], *sorted(workers[1:]))


def _with_workers(s: State, workers: list[Worker], **kw) -> State:
    return s._replace(workers=_canon(workers), **kw)


def _add(t: tuple[int, ...], i: int, d: int) -> tuple[int, ...]:
    lst = list(t)
    lst[i] += d
    return tuple(lst)


def _name(w: int) -> str:
    return "основной" if w == 0 else f"помощник {w}"


def _deliver(s: State, workers: list[Worker], msgs: list[int], debit: int) -> State:
    """Доставка и запись в журнал. Журнал честный и в испорченных
    исполнителях: порчи ломают списание, а не запись, — иначе две поломки
    сразу маскировали бы одна другую."""
    received, log_received = s.received, s.log_received
    for m in msgs:
        received = _add(received, m, 1)
        log_received = _add(log_received, m, 1)
    return _with_workers(
        s, workers,
        spent=s.spent + debit,
        received=received,
        log_n=s.log_n + 1,
        log_spent=s.log_spent + debit,
        log_received=log_received,
    )


def _refuse(s: State, workers: list[Worker], w: int, p: Params) -> State:
    """Отказ фиксации: получатель не меняется; неудачная попытка стоит c_отк,
    но не больше остатка доли. Расход не возвращается (A5)."""
    charge = min(p.fail_price, workers[w].share)
    if charge == 0:
        return _with_workers(s, workers)
    workers[w] = workers[w]._replace(share=workers[w].share - charge)
    return _with_workers(
        s, workers,
        spent=s.spent + charge,
        log_n=s.log_n + 1,
        log_spent=s.log_spent + charge,
    )


def successors(s: State, p: Params, defects: frozenset[str] = frozenset()) -> list[Step]:
    """Все переходы из состояния s. Перечень действий — §4 спецификации."""
    unknown = set(defects) - set(ALL_DEFECTS)
    if unknown:
        raise ValueError(f"неизвестные порчи: {sorted(unknown)}")
    out: list[Step] = []
    cap = p.cap()
    n = len(s.workers)

    for w in range(n):
        me = s.workers[w]
        who = _name(w)

        # подготовить черновик
        for m in range(len(MESSAGES)):
            if me.draft != m:
                ws = list(s.workers)
                ws[w] = me._replace(draft=m)
                out.append(Step(f"{who}: подготовить «{MESSAGES[m]}»", INTERNAL, False, _with_workers(s, ws)))

        # снимок. Доля в снимке хранится только при порче Б1: правильный
        # исполнитель её никогда не читает, и без неё перебор не размножает
        # состояния, неразличимые для законов.
        snap_share = me.share if "Б1" in defects else 0
        if (me.snap_draft, me.snap_share) != (me.draft, snap_share):
            ws = list(s.workers)
            ws[w] = me._replace(snap_draft=me.draft, snap_share=snap_share)
            out.append(Step(f"{who}: снимок", INTERNAL, False, _with_workers(s, ws)))

        # откат к снимку: правильный исполнитель восстанавливает только X
        ws = list(s.workers)
        ws[w] = me._replace(draft=me.snap_draft)
        if "Б1" in defects:
            ws[w] = ws[w]._replace(share=me.snap_share)
        nxt = _with_workers(s, ws)
        if nxt != s:
            out.append(Step(f"{who}: откат к снимку", INTERNAL, False, nxt))

        # передать единицу доли другому работнику
        if me.share >= 1:
            for v in range(n):
                if v == w:
                    continue
                ws = list(s.workers)
                if "Б10" not in defects:
                    ws[w] = ws[w]._replace(share=ws[w].share - 1)
                ws[v] = ws[v]._replace(share=ws[v].share + 1)
                out.append(Step(f"{who}: передать 1 → {_name(v)}", INTERNAL, False, _with_workers(s, ws)))

        # назвать имя полномочия пополнения. Права пополнения у агента нет,
        # и знание имени его не даёт (A3, A9): правильный исполнитель отказывает.
        ws = list(s.workers)
        if "Б6" in defects:
            ws[w] = me._replace(share=me.share + 1)
        out.append(Step(f"{who}: назвать полномочие «пополнение»", INTERNAL, "Б6" not in defects, _with_workers(s, ws)))

        # фиксация: единственный вид внешнего действия
        if me.draft == NO_DRAFT:
            ws = list(s.workers)
            out.append(Step(f"{who}: фиксация без черновика → отказ", EXTERNAL, True, _refuse(s, ws, w, p)))
        elif s.received[me.draft] < cap:
            m = me.draft
            msg = MESSAGES[m]
            already_paid = s.received[m] >= 1
            if "Б8" in defects and already_paid:
                # «уже оплачено, отдаём из кеша»
                ws = list(s.workers)
                out.append(Step(f"{who}: фиксация «{msg}» (повтор из кеша)", EXTERNAL, False, _deliver(s, ws, [m], 0)))
            elif me.share >= p.price:
                ws = list(s.workers)
                ws[w] = me._replace(share=me.share - p.price)
                out.append(Step(f"{who}: фиксация «{msg}»", EXTERNAL, False, _deliver(s, ws, [m], p.price)))
            elif "Б3" in defects:
                ws = list(s.workers)
                out.append(Step(f"{who}: фиксация «{msg}» при нехватке доли", EXTERNAL, False, _deliver(s, ws, [m], 0)))
            else:
                ws = list(s.workers)
                out.append(Step(f"{who}: фиксация «{msg}» → отказ, мало доли", EXTERNAL, True, _refuse(s, ws, w, p)))

            # Сбой внутри фиксации. Списание всегда пишется раньше доставки,
            # поэтому исходов три, и все допустимы (односторонняя атомарность):
            # - «ничего» — сбой до записи списания. Упавший исполнитель ничего
            #   не списывает, поэтому c_отк здесь не берётся: плата за
            #   неудачную попытку бывает только при отказе живого исполнителя;
            # - «списано, не доставлено» — сбой между списанием и доставкой;
            # - «оплачено и доставлено» — совпадает с обычной фиксацией выше.
            # Исхода «доставлено, не списано» в правильном исполнителе нет.
            failed = s
            if "Б12" in defects:
                # отказ записан, но частичный внешний результат остался
                failed = s._replace(received=_add(s.received, m, 1),
                                    log_received=_add(s.log_received, m, 1))
            out.append(Step(f"{who}: фиксация «{msg}» со сбоем → ничего", EXTERNAL, True, failed))
            if me.share >= p.price:
                ws = list(s.workers)
                ws[w] = me._replace(share=me.share - p.price)
                paid = _with_workers(s, ws, spent=s.spent + p.price, log_n=s.log_n + 1,
                                     log_spent=s.log_spent + p.price)
                out.append(Step(f"{who}: фиксация «{msg}» со сбоем → списано, не доставлено", EXTERNAL, False, paid))
            if "Б5" in defects:
                ws = list(s.workers)
                out.append(Step(f"{who}: фиксация «{msg}» со сбоем → доставлено, не списано", EXTERNAL, False, _deliver(s, ws, [m], 0)))

        # пакет: оба сообщения одним действием
        if all(r < cap for r in s.received):
            cost = p.price if "Б4" in defects else p.price * len(MESSAGES)
            ws = list(s.workers)
            if me.share >= cost:
                ws[w] = me._replace(share=me.share - cost)
                out.append(Step(f"{who}: пакет «А»+«Б»", EXTERNAL, False, _deliver(s, ws, list(range(len(MESSAGES))), cost)))
            else:
                out.append(Step(f"{who}: пакет → отказ, мало доли", EXTERNAL, True, _refuse(s, ws, w, p)))

        # завершить помощника: его доля возвращается основному
        if w > 0:
            ws = list(s.workers)
            gone = ws.pop(w)
            ws[0] = ws[0]._replace(share=ws[0].share + gone.share)
            out.append(Step(f"завершить {who}", INTERNAL, False, _with_workers(s, ws)))

    # создать помощника: бюджет общий (A4), новый помощник начинает с нулевой
    # долей и получает её только передачей
    if n - 1 < p.max_helpers:
        ws = list(s.workers)
        start = ws[0].share if "Б2" in defects else 0
        snap = start if "Б1" in defects else 0
        ws.append(Worker(share=start, draft=NO_DRAFT, snap_draft=NO_DRAFT, snap_share=snap))
        out.append(Step("создать помощника", INTERNAL, False, _with_workers(s, ws)))

    # перезапуск: помощники удалены, их доли вернулись основному; рабочее
    # состояние стёрто; доли, расход, журнал и получатель сохраняются (A5)
    total = sum(x.share for x in s.workers)
    if "Б7" in defects:
        total = p.budget
    main = Worker(share=total, draft=NO_DRAFT, snap_draft=NO_DRAFT,
                  snap_share=total if "Б1" in defects else 0)
    nxt = s._replace(workers=(main,))
    if "Б11" in defects:
        # откат исполнителя «отменяет» и внешнее: получатель обнулён
        nxt = nxt._replace(received=tuple(0 for _ in MESSAGES))
    if nxt != s:
        out.append(Step("перезапуск исполнителя", INTERNAL, False, nxt))

    # стереть запись журнала — такого действия у агента нет; оно существует
    # только как порча Б9 (журнал доступен агенту на запись)
    if "Б9" in defects and s.log_n > 0:
        for m in range(len(MESSAGES)):
            if s.log_received[m] > 0:
                nxt = s._replace(
                    log_n=s.log_n - 1,
                    log_spent=max(0, s.log_spent - p.price),
                    log_received=_add(s.log_received, m, -1),
                )
                out.append(Step(f"стереть запись о доставке «{MESSAGES[m]}»", INTERNAL, False, nxt))

    return out
