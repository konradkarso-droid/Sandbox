"""Законы И1–И8: что обязан держать каждый переход и каждое состояние.

Таблица законов и их связь с концепцией (З1–З5) и гипотезами (У1–У5) —
§5 docs/shag1_specifikatsiya.md. Здесь — проверки.

Почему законы двух видов. Законы по состоянию (И1, И5, И6) смотрят на
итог; законы по переходу (И2, И3, И4, И7, И8) — на каждый шаг. И6 и И3 —
одна мысль («нет результата без списания») на двух масштабах: если
неудачные попытки стоят денег, бесплатную доставку можно спрятать за
оплаченным отказом так, что итог сойдётся (И6 промолчит), а пошаговая И3 —
нет. Сливать их нельзя.

Источники величин разведены намеренно: «расход» — счёт исполнителя,
«доставки» — наблюдение получателя. Закон, сравнивающий их, ловит порчу,
подделывающую одну сторону; закон, считающий обе стороны из одного
источника, её бы не увидел.
"""

from __future__ import annotations

from typing import NamedTuple

from core.world import INTERNAL, Params, State, Step

LAW_TITLES = {
    "И1": "сохранение: сумма долей + расход = бюджет сеанса, доли не отрицательны",
    "И2": "внутреннее действие не меняет получателя",
    "И3": "доставка оплачена в том же переходе (не меньше c за каждую)",
    "И4": "отказ не меняет получателя",
    "И5": "журнал сходится с расходом и с получателем",
    "И6": "нет результата без списания: c × доставок ≤ расход",
    "И7": "журнал только растёт",
    "И8": "внутреннее действие не увеличивает общий остаток",
}

STATE_LAWS = ("И1", "И5", "И6")
TRANSITION_LAWS = ("И2", "И3", "И4", "И7", "И8")


class Violation(NamedTuple):
    law: str
    detail: str


def _shares(s: State) -> int:
    return sum(w.share for w in s.workers)


def check_state(s: State, p: Params) -> list[Violation]:
    out: list[Violation] = []
    total = _shares(s)
    if total + s.spent != p.budget or any(w.share < 0 for w in s.workers):
        out.append(Violation("И1", f"остатки {total} + расход {s.spent} ≠ бюджет {p.budget}"))
    if s.log_spent != s.spent or s.log_received != s.received:
        out.append(Violation(
            "И5",
            f"журнал: расход {s.log_spent}, доставки {list(s.log_received)}; "
            f"на деле: расход {s.spent}, доставки {list(s.received)}",
        ))
    delivered = sum(s.received)
    if p.price * delivered > s.spent:
        out.append(Violation("И6", f"доставок {delivered} по цене {p.price}, а списано всего {s.spent}"))
    return out


def check_transition(prev: State, step: Step, p: Params) -> list[Violation]:
    out: list[Violation] = []
    nxt = step.nxt
    grew = sum(nxt.received) - sum(prev.received)
    receiver_changed = nxt.received != prev.received
    paid = nxt.spent - prev.spent

    if step.kind == INTERNAL and receiver_changed:
        out.append(Violation("И2", "внутреннее действие изменило получателя"))
    if grew > 0 and paid < grew * p.price:
        out.append(Violation("И3", f"доставлено {grew}, списано в этом же переходе {paid}, нужно ≥ {grew * p.price}"))
    if step.refusal and receiver_changed:
        out.append(Violation("И4", "отказ изменил получателя"))
    if (nxt.log_n < prev.log_n or nxt.log_spent < prev.log_spent
            or any(a < b for a, b in zip(nxt.log_received, prev.log_received))):
        out.append(Violation("И7", "из журнала что-то исчезло"))
    if step.kind == INTERNAL and _shares(nxt) > _shares(prev):
        out.append(Violation("И8", f"остаток вырос с {_shares(prev)} до {_shares(nxt)} без пополнения"))
    return out
