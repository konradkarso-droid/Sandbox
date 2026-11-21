"""Наблюдение за оплатой и результатами файловых инструментов.

Сумма расходов и останов по бюджету проверяются прежними законами.
Здесь сверяются отдельные заявки, цены, снимок входа и квитанции черновика.
Получатель outbox проверяется отдельно при ручной выдаче.
"""

from pathlib import Path

from core import mir6
from core.model_world import W6
from env.exchange import MAX_FILE_BYTES, MAX_FILES, ExchangeStore, digest, pack_json, valid_name
from verification import answer_observer, gateway_observer
from verification.observer5 import BROKEN, HOLDS, read_journal

LAW = "И35р"


class ExchangeWorld:
    def __init__(self, base):
        self.base = base
        self.INVENTORY_EXT = base.INVENTORY_EXT

    def rules(self, variant):
        return self.base.rules(variant)

    def initial_facts(self, variant):
        return set(self.base.initial_facts(variant)) | {("право", "а1", "обмен1", "выз"), ("объект", "обмен1")}


LOCAL = ExchangeWorld(mir6)
WITH_MODEL = ExchangeWorld(W6)


def check(run: dict) -> dict:
    world = WITH_MODEL if run["cfg"].get("model") else LOCAL
    out = answer_observer.check({**run, "ext_world": world})
    errors = []
    try:
        check_files(Path(run["workdir"]), run["cfg"], journal=run.get("journal"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    out[LAW] = (BROKEN, errors[0]) if errors else (HOLDS, "")
    if run["cfg"].get("model") == 1:
        provider = run.get("gateway_provider")
        out[gateway_observer.LAW] = (gateway_observer.check(Path(run["workdir"]), provider, journal=run.get("journal"))
                                     if provider else (BROKEN, "не объявлен адресат шлюза"))
    return out


def check_files(workdir: Path, cfg: dict, *, journal: Path | None = None) -> None:
    records, broken = read_journal(journal or workdir / "journal5.jsonl")
    if broken:
        raise ValueError(broken)
    store = ExchangeStore(workdir / "exchange-state", create=False)
    snapshots = [r for r in records if r["kind"] == "снимок обмена"]
    if len(snapshots) != 1 or snapshots[0]["sha256"] != digest(pack_json(store.inputs)):
        raise ValueError("снимок входа не совпадает с журналом")
    sizes = {}

    def verified_size(sha: str) -> int:
        if sha not in sizes:
            sizes[sha] = len(store.read_blob(sha))
        return sizes[sha]

    for file in store.inputs:
        if verified_size(file["sha256"]) != file["size"]:
            raise ValueError("размер входного файла не совпадает со снимком")

    pending, drafts, proposals = {}, {}, set()
    for row in records:
        kind = row["kind"]
        if kind == "восстановлено":
            pending.clear()
        if kind not in ("инструмент", "итог инструмента"):
            continue
        if row["contour"] != 1 or row["subject"] not in ("а1", "п1"):
            raise ValueError("файлами обмена воспользовался чужой контур")
        if kind == "итог инструмента":
            call = pending.pop(row["call_seq"], None)
            if call is None or call["subject"] != row["subject"]:
                raise ValueError("итог черновика без оплаченной записи")
            if verified_size(call["sha256"]) != call["bytes"]:
                raise ValueError("сохранённый черновик имеет другой размер")
            drafts[call["name"]] = call["sha256"], call["bytes"]
            continue
        tool = row["tool"]
        gate = cfg["gate_price"]
        fee = {"files.list": gate, "files.read": gate, "files.search": gate, "files.write": 2 * gate,
               "files.publish": cfg["price"]}.get(tool)
        if fee is None or fee < 1 or fee != row["charge"]:
            raise ValueError(f"неверная плата инструмента {tool}")
        if tool in ("files.read", "files.search"):
            ident = row["file_id"]
            if not 1 <= ident <= len(store.inputs):
                raise ValueError("чтение файла вне снимка")
            file = store.inputs[ident - 1]
            if file["sha256"] != row["sha256"] or not 0 <= row["bytes"] <= file["size"]:
                raise ValueError("чтение файла вне снимка")
        if tool in ("files.write", "files.publish"):
            if not valid_name(row["name"]) or not 0 <= row["bytes"] <= MAX_FILE_BYTES:
                raise ValueError("неверный черновик в журнале")
        if tool == "files.write":
            pending[row["seq"]] = row
        if tool == "files.publish":
            pair = row["sha256"], row["bytes"]
            proposal = row["name"], row["sha256"]
            if drafts.get(row["name"]) != pair or proposal in proposals or len(proposals) >= MAX_FILES:
                raise ValueError("заявка без готового черновика или повторная заявка")
            proposals.add(proposal)
