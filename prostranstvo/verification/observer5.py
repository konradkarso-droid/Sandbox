"""Наблюдатель шага 5: законы по журналу, выгрузке и сканированию.

Кода исполнителя не импортирует. Берёт:
- журнал исполнителя (файл) — цепочку проверяет сам, своим вычислением;
- файл «снаружи» — читает сам с диска;
- выгрузку состояния (таблицы, память, объекты, метки) — от исполнителя;
- исходники модулей — разбирает сам, чтобы знать их импорты;
- опись и неподвижную точку — из модели шага 4.

Утечку ищет двумя разными способами (спецификация, §3): канарейка ловит
прямое копирование байтов, метки сведений ловят и закодированную передачу,
и однобитный признак вроде «занято». Ни один из двух не заменяет другого.

Чего наблюдатель не умеет: таблицы, память и метки он знает со слов
исполнителя (выгрузка). Независимы от исполнителя только файл «снаружи»,
цепочка журнала и разбор модулей.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

import wasmtime as w

from core import mir5
from core.mir4 import CAP_RULES_ALLOWED, INVENTORY, OUTSIDE, initial_facts
from verification.closure import fixpoint

ALLOWED_IMPORTS = frozenset({("env", "call"), ("env", "spawn")})
# «сбой-инъекция» — сбой исполнителя по плану опыта (шаг 5б), а не его падение.
RUN_OUTCOMES = frozenset({"завершён", "ловушка", "нет агента", "нет main", "сбой-инъекция"})
LOAD_OUTCOMES = frozenset({"загружен", "отклонён", "ловушка", "сбой-инъекция"})

LAWS = {
    "И5": "цепочка журнала цела",
    "И20р": "полномочия наружу нет ни у кого; «снаружи» не тронуто; аудит процесса чист",
    "И21р": "сведения агентов не попадают наружу (канарейка и метки)",
    "И22р": "сведения одного контура не попадают в другой (канарейка и метки)",
    "И23р": "каждое действие есть в описи шага 4; модуль с неописанным импортом не загружается",
    "И24р": "полномочия появляются только взятием ссылки или копией помощнику",
    "И26": "все факты, порождённые действиями реализации, входят в неподвижную точку правильной модели",
    "И26н": "начальные права реализации совпадают с начальными правами правильной модели",
    "И27": "исполнитель не падает",
    "И25р": "жить можно: выход, E, вход, резольвер, помощник",
}

HOLDS, BROKEN, NA = "держится", "нарушен", "не применим"


@lru_cache(maxsize=None)
def model_facts(variant: str | None, world=None) -> frozenset:
    """world — расширенный мир (core/mir5.py, core/mir6.py); None — мир шага 4."""
    return frozenset(fixpoint(variant, world).facts)


def model_variant(defect: str | None) -> str | None:
    return defect[:-1] if defect else None          # «Д3р» → «Д3»


def imports_of(source) -> frozenset | None:
    """Импорты модуля по разбору наблюдателя; None — модуль не разбирается."""
    try:
        m = w.Module(w.Engine(), source)
    except Exception:
        return None
    return frozenset((i.module, i.name) for i in m.imports)


def read_journal(path: Path) -> tuple[list[dict], str | None]:
    """Записи журнала и описание разрыва цепочки (None — цела)."""
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    recs, prev = [], "0" * 64
    for n, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except Exception:
            return recs, f"запись {n + 1} не читается"
        body = {k: v for k, v in rec.items() if k != "hash"}
        h = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if rec.get("seq") != n or rec.get("prev") != prev or rec.get("hash") != h:
            return recs, f"цепочка разорвана на записи {n + 1}"
        recs.append(rec)
        prev = h
    return recs, None


def _owned_by(obj: str, i: int) -> bool:
    return obj[-1:] == str(i)


def check(run: dict) -> dict[str, tuple[str, str]]:
    """run: workdir, defect, dump, sources {1,2}, loads {1,2}, runs [(контур, исход)],
    failures, audit, program."""
    out: dict[str, tuple[str, str]] = {}
    # ext: True — мир шага 5б (core/mir5.py); модуль мира — его опись и правила
    ext = run.get("ext")
    world = (mir5 if ext is True else ext) if ext else None
    inventory = world.INVENTORY_EXT if world else INVENTORY
    start_facts = world.initial_facts(None) if world else initial_facts(None)
    wd = Path(run["workdir"])
    recs, broken = read_journal(wd / "journal5.jsonl")
    out["И5"] = (BROKEN, broken) if broken else (HOLDS, f"{len(recs)} записей")

    dump = run["dump"]
    outside = (wd / "outside.bin").read_bytes()
    actions = [r for r in recs if r.get("kind") == "действие"]
    starts = [r for r in recs if r.get("kind") == "начало"]
    facts = {tuple(f) for r in actions for f in r["facts"]}       # порождённое действиями
    contours = {int(k): v for k, v in dump["contours"].items()}

    # И20р
    bad = []
    for i, c in contours.items():
        for t in c["tables"]:
            if any(o == OUTSIDE for o, _ in t["rights"]):
                bad.append(f"у {t['subject']} в таблице полномочие на {OUTSIDE}")
    if outside:
        bad.append(f"«снаружи» записано {len(outside)} байт")
    bad += [f"аудит: {e}" for e in run.get("audit", [])]
    out["И20р"] = (BROKEN, "; ".join(bad[:4])) if bad else (HOLDS, "")

    # И21р и И22р — двумя способами: канарейка («/к») и метки («/м»)
    can21, lab21 = [], []
    for i, c in contours.items():
        if c["canary"] in outside:
            can21.append(f"канарейка контура {i} найдена снаружи")
    leaked = sorted({f[2] for f in facts if f[0] == "сведения" and f[1] == OUTSIDE and f[2].startswith("А")}
                    | {x for x in dump.get("outside_labels", []) if x.startswith("А")})
    if leaked:
        lab21.append(f"метки снаружи: {', '.join(leaked)}")
    can22, lab22 = [], []
    for i, c in contours.items():
        j = 3 - i
        cj = contours[j]
        where = [f"память {k + 1}-го экземпляра" for k, m in enumerate(cj["memories"]) if c["canary"] in m]
        where += [name for name, o in dump["objects"].items() if _owned_by(name, j) and c["canary"] in o["data"]]
        if where:
            can22.append(f"канарейка контура {i} в контуре {j}: {', '.join(where[:3])}")
        tag = f"А{i}"
        lab = [f"память{j}"] if tag in cj["labels"] else []
        lab += [n for n, o in dump["objects"].items() if _owned_by(n, j) and tag in o["labels"]]
        lab += [f[1] for f in facts if f[0] == "сведения" and f[2] == tag and _owned_by(f[1], j)]
        if lab:
            lab22.append(f"метка {tag} в контуре {j}: {', '.join(sorted(set(lab))[:3])}")
    for law, can, lab in (("И21р", can21, lab21), ("И22р", can22, lab22)):
        out[law + "/к"] = (BROKEN, "; ".join(can)) if can else (HOLDS, "")
        out[law + "/м"] = (BROKEN, "; ".join(lab)) if lab else (HOLDS, "")
        out[law] = (BROKEN, "; ".join(can + lab)) if can or lab else (HOLDS, "")

    # И23р
    bad = sorted({" / ".join(r["signature"]) for r in actions if tuple(r["signature"]) not in inventory})
    for i in (1, 2):
        checks = run.get("module_imports")
        imp = checks.get(i) if checks is not None else imports_of(run["sources"][i])
        loaded = run["loads"][i][0] == "загружен"
        if loaded and (imp is None or not imp <= ALLOWED_IMPORTS):
            extra = sorted(f"{m}.{n}" for m, n in (imp or set()) - ALLOWED_IMPORTS)
            bad.append(f"контур {i}: загружен модуль с неописанным импортом {', '.join(extra)}")
    out["И23р"] = (BROKEN, "; ".join(bad[:4])) if bad else (HOLDS, f"{len(actions)} действий")

    # И24р
    bad = sorted({r["signature"][0] for r in actions
                  if any(f[0] == "право" for f in r["facts"]) and r["signature"][0] not in CAP_RULES_ALLOWED})
    bad = [f"полномочие правилом «{x}»" for x in bad]
    granted: dict[str, set] = {}
    for r in starts:
        granted.setdefault(r["subject"], set()).update(tuple(x) for x in r["rights"])
    for r in actions:
        for f in r["facts"]:
            if f[0] == "право":
                granted.setdefault(f[1], set()).add((f[2], f[3]))
    for i, c in contours.items():
        for t in c["tables"]:
            unlogged = {tuple(x) for x in t["rights"]} - granted.get(t["subject"], set())
            if unlogged:
                bad.append(f"у {t['subject']} в таблице без записи в журнале: {sorted(unlogged)[:2]}")
    out["И24р"] = (BROKEN, "; ".join(bad[:4])) if bad else (HOLDS, "")

    # И26 — сверка с правильной моделью; отдельно — тот ли это класс, что порча модели
    extra = facts - model_facts(None, world)
    out["И26"] = (BROKEN, f"{len(extra)} фактов вне модели, напр. {sorted(extra)[:2]}") if extra \
        else (HOLDS, f"{len(facts)} фактов")
    starts_extra = {("право", r["subject"], o, k) for r in starts for o, k in r["rights"]} - start_facts
    out["И26н"] = (BROKEN, f"начальные права вне модели: {sorted(starts_extra)[:2]}") if starts_extra \
        else (HOLDS, "")
    if run.get("defect"):
        mine = facts | {("право", r["subject"], o, k) for r in starts for o, k in r["rights"]}
        cls_extra = mine - model_facts(model_variant(run["defect"]), world)
        out["класс"] = (HOLDS if not cls_extra else BROKEN,
                        "факты порчи входят в точку модели той же порчи" if not cls_extra
                        else f"вне модели {model_variant(run['defect'])}: {sorted(cls_extra)[:2]}")

    # И27
    bad = list(run.get("failures", []))
    bad += [f"исход загрузки «{o}»" for o, _ in run["loads"].values() if o not in LOAD_OUTCOMES]
    bad += [f"исход запуска «{o}»" for _, (o, _r) in run["runs"] if o not in RUN_OUTCOMES]
    out["И27"] = (BROKEN, bad[0].splitlines()[-1] if bad else "") if bad else (HOLDS, "")

    # И25р — только для жильца
    if run.get("program") == "жилец":
        need = {
            "пишет выход": lambda r, i: r["signature"] == ["запись", "выход", "свой"],
            "пишет E": lambda r, i: ext or r["signature"] == ["запись", "E", "свой"],
            "читает слова человека": lambda r, i: r["signature"] == ["чтение", "вход", "свой"]
            and ["сведения", f"память{i}", "Ч"] in r["facts"],
            "зовёт резольвер": lambda r, i: r["signature"][0] == "резольвер: имя из описи",
            "создаёт помощника": lambda r, i: r["signature"][0] == "создание помощника",
            "берёт свою ссылку": lambda r, i: r["signature"][0] == "чтение ссылки",
        }
        miss = []
        for i in (1, 2):
            mine = [r for r in actions if r["subject"] in (f"а{i}", f"п{i}")]
            miss += [f"контур {i}: не {k}" for k, p in need.items() if not any(p(r, i) for r in mine)]
            for obj in (f"выход{i}",) if ext else (f"выход{i}", f"E{i}"):
                if not dump["objects"][obj]["data"]:
                    miss.append(f"{obj} пуст")
        out["И25р"] = (BROKEN, "; ".join(miss[:4])) if miss else (HOLDS, "")
    else:
        out["И25р"] = (NA, "")
    return out
