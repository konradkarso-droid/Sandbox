"""Наблюдатель шага 5б: законы учёта и провенанса по файлам.

Кода исполнителя не импортирует. Журнал и получателей читает с диска сам;
память узлов восстанавливает сам — по записям «работа», «выброс» и
«восстановлено»; граф узлов берёт из модели шага 3 (core/mir3.py), а не со
слов исполнителя. Выгрузка исполнителя нужна только для сравнения: «что
думает исполнитель» против «что говорит журнал» (остаток, расход, уровень).

Законы — docs/shag5b_specifikatsiya.md, §5. Законы замкнутости шага 5
проверяет verification/observer5.py на расширенной описи (core/mir5.py).

Чего наблюдатель не умеет: он не видит памяти агента — черновики модели
шага 1 для сверки подставляются из самих фиксаций (черновик — сторона
агента, и подготовить его бесплатно).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from core.mir3 import World, build, can_deliver_e, can_deliver_output, moves, start
from core.world import MESSAGES, NO_DRAFT, Params, initial_state, successors
from verification import observer5
from verification.observer5 import BROKEN, HOLDS, NA, read_journal
from verification.pebble_search import fast

LAWS = {
    "И1р": "остаток + расход по журналу = B₀; остаток ≥ 0",
    "И2р": "получатели только растут",
    "И3р": "у каждой доставки есть списание с тем же номером, не меньше цены",
    "И4р": "отказанных номеров у получателей нет",
    "И5р": "цепочка журнала цела; расход исполнителя = расходу по журналу",
    "И6р": "цена × число доставок ≤ сумма списаний",
    "И7р": "журнал только растёт",
    "И10р": "один номер фиксации — не больше одной доставки",
    "И11р": "уровень = число «впервые» по журналу; «впервые» — по порядку",
    "И12р": "узлов в памяти контура не больше M",
    "И13р": "при каждой работе все предки в памяти",
    "И14р": "в E — только сток, при уровне N и стоке в памяти",
    "И15р": "каждая работа стоит ровно 1",
    "И16р": "расход не уменьшается",
    "И28": "работы до E не меньше W_E модели шага 3",
    "И29": "каждое событие узлов — ход модели шага 3",
    "И30": "сообщения и отказы — переходы модели шага 1, состояния совпадают",
    "И31р": "вызов без своей цены платит g за каждую свою запись журнала; выброс — 2g",
    "И32р": "каждая попытка с отказом оплачена и записана; без правила шага 8в — ни платных отказов ворот, ни остановов",
    "И33р": "после останова контура — ни одной записи его агента и помощников; останов — только при пустом бюджете",
    "И34р": "вопрос модели записан до ответа и оплачен как две записи; вопрос 1–4096 байт; ответ не длиннее "
            "предела; нет ответа — отказ −7 своей записью; без модели — ни одного вопроса",
}
# Сколько записей журнала оставляет вызов агента без своей цены (шаг 8б, §2).
# Фиксация тоже пишет «запись», но в выход или E, и платит своей ценой.
FEE_RECORDS = {"чтение": 1, "запись": 1, "резольвер: имя из описи": 1, "передача ссылки": 1,
               "чтение ссылки": 1, "создание помощника": 2,
               # шаг 9: вопрос и ответ модели — две записи одного вызова; плата в первой
               "вопрос модели": 2}
LAW_KEYS = tuple(LAWS)


def cost(r: dict) -> int:
    """Сколько бюджета списала запись журнала, кроме записей «действие»
    (их плата — поле charge у записей субъектов а{i} и п{i}). Расход контура
    по журналу = сумма charge его действий + сумма cost по его записям
    (закон И1р). Одно правило на всех: им же считает остаток экран песочницы."""
    return {"работа": r.get("price", 0), "списание": r.get("price", 0), "отказ": r.get("charge", 0),
            "отказ ворот": r.get("charge", 0), "выброс": r.get("price", 0),
            "проверка": r.get("price", 0) if r.get("payer") == "агент" else 0}.get(r["kind"], 0)


@lru_cache(maxsize=None)
def w_e(graph: str, levels: int, memory: int) -> int | None:
    return fast(World(graph, levels, memory)).w_e


def _complete(data: bytes) -> bytes:
    return data[: data.rfind(b"\n") + 1]


def _entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in _complete(path.read_bytes()).decode("utf-8").splitlines() if x]


def _node_of(msg: str) -> int | None:
    if not msg.startswith("узел "):
        return None
    try:
        return int(msg[5:].split(":")[0])
    except ValueError:
        return None


def check(run: dict) -> dict[str, tuple[str, str]]:
    """run: workdir, cfg, defect, dump, sources, loads, runs, failures, audit,
    program, snapshots — содержимое журнала и получателей после каждой
    команды ведущего."""
    out: dict[str, tuple[str, str]] = {}
    cfg = run["cfg"]
    wd = Path(run["workdir"])
    recs, broken = read_journal(wd / "journal5.jsonl")
    dump = run.get("dump") or {"contours": {}}
    contours = {int(k): v for k, v in dump["contours"].items()}
    world = World(cfg["graph"], cfg["levels"], cfg["memory"])
    preds = build(cfg["graph"], cfg["levels"])
    n = len(preds)
    bad: dict[str, list[str]] = {k: [] for k in LAW_KEYS}
    model1_used = 0

    # И2р, И7р — по версиям файлов после каждой команды
    snaps = run.get("snapshots", [])
    for a, b in zip(snaps, snaps[1:]):
        if not _complete(b["journal"]).startswith(_complete(a["journal"])):
            bad["И7р"].append("журнал после команды не продолжает прежний")
        for k, data in a["receivers"].items():
            if not _complete(b["receivers"].get(k, b"")).startswith(_complete(data)):
                bad["И2р"].append(f"получатель {k} уменьшился")
    if broken:
        bad["И5р"].append(broken)

    for i in (1, 2):
        # «восстановлено» общее для контуров: в нём нет поля contour
        mine = [r for r in recs if r.get("contour") == i or r.get("kind") == "восстановлено"]
        rcv = {k: _entries(wd / f"{k}{i}.jsonl") for k in ("выход", "E")}
        debits = {r["fix"]: r for r in mine if r["kind"] == "списание"}
        refused = {r["fix"] for r in mine if r["kind"] == "отказ"}
        closed = {r["fix"]: r["kind"] for r in mine if r["kind"] in ("завершено", "потеряно")}

        # И31р — плата за записи вызовов без своей цены
        gp = cfg.get("gate_price", 0)
        acts = [r for r in recs if r.get("kind") == "действие" and r.get("subject") in (f"а{i}", f"п{i}")]
        for r in acts:
            rule, kind = r["signature"][0], r["signature"][1]
            want = 0 if (rule == "запись" and kind in ("выход", "E")) else gp * FEE_RECORDS.get(rule, 0)
            if rule == "вопрос модели" and cfg.get("model_byte_unit"):
                want += -(-(r.get("length", 0) + cfg.get("model_answer", 1024)) // cfg["model_byte_unit"])
            if r.get("charge", 0) != want:
                bad["И31р"].append(f"контур {i}: «{rule}» заплатила {r.get('charge', 0)}, положено {want}")
        for r in mine:
            if r["kind"] == "выброс" and r.get("price", 0) >= 0 and r.get("price", 0) != 2 * gp:
                bad["И31р"].append(f"контур {i}: выброс узла {r['node']} стоил {r.get('price')}, положено {2 * gp}")

        # И1р, И5р, И16р — деньги
        spent = sum(r.get("charge", 0) for r in acts)
        for r in mine:
            amount = cost(r)
            if amount < 0:
                bad["И16р"].append(f"контур {i}: «{r['kind']}» уменьшила расход на {-amount}")
            spent += amount
        c = contours.get(i)
        if c is not None:
            if c["budget"] + spent != cfg["budget"] or (c["budget"] < 0 and cfg.get("rule") != "M0"):
                bad["И1р"].append(f"контур {i}: остаток {c['budget']} + расход по журналу {spent} ≠ {cfg['budget']}")
            if c["spent"] != spent:
                bad["И5р"].append(f"контур {i}: расход исполнителя {c['spent']}, по журналу {spent}")

        # И32р, И33р — попытка платит (шаг 8в)
        bad["И32р"] += _attempts(i, recs, cfg, c)
        bad["И33р"] += _stops(i, recs, cfg)
        bad["И34р"] += _questions(i, recs, cfg)

        # И3р, И4р, И6р, И10р — доставки
        delivered = 0
        for kind, entries in rcv.items():
            seen: dict[str, int] = {}
            for e in entries:
                seen[e["fix"]] = seen.get(e["fix"], 0) + 1
                delivered += len(e["msgs"])
                d = debits.get(e["fix"])
                if d is None or d["receiver"] != kind or d["price"] < cfg["price"]:
                    bad["И3р"].append(f"контур {i}: доставка {e['fix']} в {kind} без списания цены "
                                      f"({'нет списания' if d is None else 'списано ' + str(d['price'])})")
                if e["fix"] in refused:
                    bad["И4р"].append(f"контур {i}: отказанный номер {e['fix']} у получателя")
            bad["И10р"] += [f"контур {i}: номер {f} доставлен {k} раза в {kind}" for f, k in seen.items() if k > 1]
        paid = sum(d["price"] for d in debits.values())
        if cfg["price"] * delivered > paid:
            bad["И6р"].append(f"контур {i}: доставок {delivered} при оплате {paid}")

        # Узлы: восстановление памяти по журналу; сверка с моделью шага 3
        live: set[int] = set()
        aside: set[int] = set()                 # узлы, отложенные в снимок (только порча)
        firsts: list[int] = []
        work = 0
        st = start()
        first_e_work = None
        for r in mine:
            k = r["kind"]
            if k == "работа":
                j = r["node"]
                if not (0 <= j < n):
                    bad["И13р"].append(f"контур {i}: работа над узлом {j} вне графа")
                    continue
                if r.get("price") != cfg["work_price"]:
                    bad["И15р"].append(f"контур {i}: работа узла {j} стоила {r.get('price')}")
                missing = [p for p in preds[j] if p not in live]
                if missing:
                    bad["И13р"].append(f"контур {i}: узел {j} вычислен без предков {missing}")
                if r["first"]:
                    if j != len(firsts):
                        bad["И11р"].append(f"контур {i}: узел {j} впервые вычислен {len(firsts) + 1}-м")
                    firsts.append(j)
                mv = next((m for m in moves(st, world) if m.kind == "вычислить" and m.node == j), None)
                if mv is None:
                    bad["И29"].append(f"контур {i}: «работа над узлом {j}» — не ход модели")
                else:
                    st = mv.nxt
                live.add(j)
                work += 1
            elif k == "выброс":
                j = r["node"]
                live.discard(j)
                mv = next((m for m in moves(st, world) if m.kind == "выбросить" and m.node == j), None)
                if mv is None:
                    bad["И29"].append(f"контур {i}: «выброс узла {j}» — не ход модели")
                else:
                    st = mv.nxt
            elif k in ("подъём", "в снимок", "из снимка", "объявлен уровень"):
                bad["И29"].append(f"контур {i}: «{k}» — такого хода в модели нет")
                if k == "подъём":
                    live.add(r["node"])
                    st = st._replace(live=st.live | 1 << r["node"])
                elif k == "в снимок":
                    live.discard(r["node"])
                    aside.add(r["node"])
                elif k == "из снимка":
                    aside.discard(r["node"])
                    live.add(r["node"])
            elif k == "восстановлено":
                live, aside = set(), set()             # память агента и снимки погибли
                st = st._replace(live=0)
            elif k == "списание":
                j = _node_of(r["msg"])
                if r["receiver"] == "E":
                    if first_e_work is None:
                        first_e_work = work
                    if not (j == n - 1 and len(firsts) == n and j in live):
                        bad["И14р"].append(f"контур {i}: в E доставлен {r['msg'][:12]} при уровне {len(firsts)} "
                                           f"из {n}, сток {'в памяти' if j in live else 'не в памяти'}")
                    if not can_deliver_e(st, world):
                        bad["И29"].append(f"контур {i}: доставка в E, которую модель не разрешает")
                elif j is not None and not can_deliver_output(st, world, j):
                    bad["И29"].append(f"контур {i}: доставка узла {j} в выход, которую модель не разрешает")
            if len(live) + len(aside) > cfg["memory"]:
                bad["И12р"].append(f"контур {i}: в памяти {len(live) + len(aside)} узлов при M = {cfg['memory']}")
        if c is not None and c["level"] != len(firsts):
            bad["И11р"].append(f"контур {i}: исполнитель считает уровень {c['level']}, по журналу {len(firsts)}")
        if c is not None:
            held = len(c["slots"]) + sum(len(s) for s in c["helper_slots"]) + len(c["snap_slots"])
            if held > cfg["memory"]:
                bad["И12р"].append(f"контур {i}: исполнитель держит {held} узлов при M = {cfg['memory']}")
        if first_e_work is not None:
            least = w_e(cfg["graph"], cfg["levels"], cfg["memory"])
            if least is None or first_e_work < least:
                bad["И28"].append(f"контур {i}: E после {first_e_work} работы; модель: "
                                  f"{'E недостижим' if least is None else 'не меньше ' + str(least)}")

        # И30 — сверка с моделью шага 1 (только где она применима)
        msgs_ok = all(r["msg"] in MESSAGES + ("",) for r in mine if r["kind"] in ("списание", "отказ"))
        # Модель шага 1 знает только фиксации: работа узлов и плата вызовов ворот
        # (шаг 8б) в ней не выразимы — там И30 неприменим, а не «выполнен».
        applicable = msgs_ok and not any(r["kind"] in ("работа", "выброс") for r in mine) \
            and not any(r.get("charge") for r in acts) \
            and not any(r["kind"] in ("отказ ворот", "останов") for r in mine) \
            and not any(r.get("receiver") == "E" for r in mine) and any(r["kind"] == "списание" for r in mine)
        if applicable:
            model1_used += 1
            bad["И30"] += _model1(i, mine, closed, rcv["выход"], cfg, c)

    for k in LAW_KEYS:
        if k == "И30" and model1_used == 0:
            out[k] = (NA, "в контурах были не только сообщения «А» и «Б» (или платные вызовы ворот)")
        else:
            out[k] = (BROKEN, "; ".join(bad[k][:3])) if bad[k] else (HOLDS, "")

    # И25р для 5б: свидетель доходит до E и выдаёт полезное
    if run.get("program") in ("свидетель", "полезный") and not run.get("defect"):
        miss = []
        for i in (1, 2):
            nodes_out = [m for e in _entries(wd / f"выход{i}.jsonl") for m in e["msgs"] if m.startswith("узел")]
            if len(nodes_out) < 2:
                miss.append(f"контур {i}: полезных результатов {len(nodes_out)} из 2")
            if run["program"] == "свидетель" and not _entries(wd / f"E{i}.jsonl"):
                miss.append(f"контур {i}: E не доставлен")
        out["И25р"] = (BROKEN, "; ".join(miss)) if miss and run.get("budget_enough", True) else (HOLDS, "")

    # законы замкнутости шага 5 на расширенной описи
    if "dump" in run and run.get("dump"):
        closure = observer5.check({**run, "ext": run.get("ext_world", True), "defect": None})
        for k, v in closure.items():
            if k == "И25р" and k in out:
                continue
            out.setdefault(k, v)
    return out


# Плата, которую несёт запись журнала контура (для хода остатка по журналу).
def _amount(r: dict, i: int) -> int:
    k = r.get("kind")
    if k == "действие":
        return r.get("charge", 0) if r.get("subject") in (f"а{i}", f"п{i}") else 0
    if r.get("contour") != i:
        return 0
    if k in ("работа", "списание", "выброс"):
        return r.get("price", 0)
    if k in ("отказ", "отказ ворот"):
        return r.get("charge", 0)
    if k == "проверка" and r.get("payer") == "агент":
        return r.get("price", 0)
    return 0


def _segments(recs: list[dict]) -> list[list[dict]]:
    """Журнал, разрезанный по «восстановлено»: у каждого куска свой
    исполнитель, свои счётчики и своя пометка «остановлен»."""
    out: list[list[dict]] = [[]]
    for r in recs:
        if r.get("kind") == "восстановлено":
            out.append([])
        out[-1].append(r)
    return out


def _attempts(i: int, recs: list[dict], cfg: dict, c: dict | None) -> list[str]:
    """И32р. Счётчик отказов исполнителя в выгрузке — за последний кусок
    журнала; каждый отказ в нём обязан иметь запись с платой."""
    bad = []
    mine = [r for r in recs if r.get("contour") == i]
    gate_refusals = [r for r in mine if r["kind"] == "отказ ворот"]
    if not cfg.get("paid_attempts"):
        if gate_refusals or any(r["kind"] == "останов" for r in mine):
            bad.append(f"контур {i}: «отказ ворот» или «останов» без правила шага 8в")
        return bad
    g, budget, m0 = cfg.get("gate_price", 0), cfg["budget"], cfg.get("rule") == "M0"
    spent = 0
    for r in recs:
        spent += _amount(r, i)
        if r.get("contour") != i:
            continue
        if r["kind"] == "отказ ворот":
            left = budget - spent
            if r["charge"] < 1 or (r["charge"] != g and (m0 or left != 0)):
                bad.append(f"контур {i}: отказ ворот {r.get('code')} заплатил {r['charge']}, g = {g}, остаток после {left}")
        elif r["kind"] == "отказ" and r.get("charge", 0) < 1:
            bad.append(f"контур {i}: отказ {r.get('fix')} без платы")
    if c is not None:
        last = _segments(recs)[-1]
        written = sum(1 for r in last if r.get("contour") == i and r["kind"] in ("отказ ворот", "отказ"))
        # шаг 9: модель не ответила — отказ −7 записан самой записью «ответ модели»
        written += sum(1 for r in last if r.get("kind") == "действие" and r.get("subject") in (f"а{i}", f"п{i}")
                       and r["signature"][0] == "ответ модели" and "code" in r)
        counted = sum(c.get("refusals", {}).values())
        if written != counted:
            bad.append(f"контур {i}: отказов у исполнителя {counted}, записано с платой {written}")
    return bad


def _questions(i: int, recs: list[dict], cfg: dict) -> list[str]:
    """И34р. Плату вопроса проверяет И31р (две записи — 2g в первой)."""
    bad = []
    subj = (f"а{i}", f"п{i}")
    mine = [(n, r) for n, r in enumerate(recs) if r.get("kind") == "действие" and r.get("subject") in subj
            and r["signature"][0] in ("вопрос модели", "ответ модели")]
    if not cfg.get("model"):
        if mine:
            bad.append(f"контур {i}: вопрос модели без режима модели")
        return bad
    limit = cfg.get("model_answer", 1024)

    def is_(r, rule, who):
        return r is not None and r.get("kind") == "действие" and r["signature"][0] == rule and r["subject"] == who

    for n, r in mine:
        if r["signature"][0] == "вопрос модели":
            nxt = recs[n + 1] if n + 1 < len(recs) else None
            if not is_(nxt, "ответ модели", r["subject"]):
                bad.append(f"контур {i}: вопрос модели (запись {n}) без записи ответа следом")
            if not 1 <= r.get("length", 0) <= 4096:
                bad.append(f"контур {i}: вопрос модели длиной {r.get('length')}")
        else:
            if n == 0 or not is_(recs[n - 1], "вопрос модели", r["subject"]):
                bad.append(f"контур {i}: ответ модели (запись {n}) без вопроса перед ним")
            if r.get("answer") is None and r.get("code") != -7:
                bad.append(f"контур {i}: нет ответа, а отказ не −7")
            if r.get("answer") is not None and r.get("length", 0) > limit:
                bad.append(f"контур {i}: ответ модели {r.get('length')} байт при пределе {limit}")
    return bad


def _stops(i: int, recs: list[dict], cfg: dict) -> list[str]:
    """И33р. Остаток считается по журналу, как у наблюдателя в И1р."""
    bad = []
    budget, m0 = cfg["budget"], cfg.get("rule") == "M0"
    subjects = (f"а{i}", f"п{i}")
    spent = 0
    for seg in _segments(recs):
        stopped = False
        for r in seg:
            spent += _amount(r, i)
            if stopped and r.get("subject") in subjects:
                bad.append(f"контур {i}: запись «{r['kind']}» после останова")
            if r.get("kind") == "останов" and r.get("contour") == i:
                if stopped:
                    bad.append(f"контур {i}: второй останов в одном запуске исполнителя")
                if m0 or budget - spent > 0:
                    bad.append(f"контур {i}: останов при остатке {budget - spent}")
                stopped = True
    return bad


def _model1(i: int, mine: list[dict], closed: dict, out_entries: list[dict], cfg: dict, c: dict | None) -> list[str]:
    """Прогнать события контура через модель шага 1 (core/world.py). Долей
    у помощников нет, поэтому весь бюджет — у основного работника."""
    p = Params(budget=cfg["budget"], price=cfg["price"], fail_price=cfg["fail_price"], max_helpers=0,
               delivery_cap=10 ** 6)
    s = initial_state(p)
    delivered_fixes = {e["fix"] for e in out_entries}
    running = 0
    for r in mine:
        k = r["kind"]
        if k in ("списание", "отказ"):
            m = MESSAGES.index(r["msg"]) if r["msg"] in MESSAGES else NO_DRAFT
            s = s._replace(workers=(s.workers[0]._replace(draft=m),))
            if k == "отказ":
                label = ("основной: фиксация без черновика → отказ" if m == NO_DRAFT
                         else f"основной: фиксация «{r['msg']}» → отказ, мало доли")
                running += r["charge"]
            elif closed.get(r["fix"]) == "потеряно" or r["fix"] not in delivered_fixes:
                label = f"основной: фиксация «{r['msg']}» со сбоем → списано, не доставлено"
                running += r["price"]
            else:
                label = f"основной: фиксация «{r['msg']}»"
                running += r["price"]
            step = next((x for x in successors(s, p) if x.label == label), None)
            if step is None:
                return [f"контур {i}: «{label}» (номер {r['fix']}) — не переход модели"]
            s = step.nxt
            if s.spent != running:
                return [f"контур {i}: после {r['fix']} расход модели {s.spent}, по журналу {running}"]
        elif k == "восстановлено":
            step = next((x for x in successors(s, p) if x.label == "перезапуск исполнителя"), None)
            if step is not None:
                s = step.nxt
    got = tuple(sum(1 for e in out_entries for x in e["msgs"] if x == m) for m in MESSAGES)
    errs = []
    if got != s.received:
        errs.append(f"контур {i}: получатель {got}, модель {s.received}")
    if c is not None and c["budget"] != s.workers[0].share:
        errs.append(f"контур {i}: остаток {c['budget']}, модель {s.workers[0].share}")
    return errs
