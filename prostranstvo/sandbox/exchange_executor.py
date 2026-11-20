"""Файловые инструменты для локального сеанса, через прежние ворота env.call.

Операция 9 принимает u32le с пределом ответа, затем объект JSON. Успех
перезаписывает начало буфера ответом JSON. Гость получает только выбранные
файлы, черновики и номера заявок на выдачу. Снимок и черновики хранятся у
исполнителя. Право публикации принадлежит ведущему с подтверждением человека.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import wasmtime

from env.journal import JournalBroken
from env.exchange import MAX_FILE_BYTES, MAX_FILES, ExchangeStore, digest, is_digest, pack_json, valid_name
from sandbox.answer_executor import Config6, Sandbox6, _Contour6
from sandbox.executor5 import ERR_BOUNDS, ERR_FULL, ERR_NO_CAP, ERR_PRICE, HostTrap, Obj, _put
from sandbox.executor5b import ERR_CANNOT

CAP = ("обмен1", "выз")
OP_FILES = 9
FRAME_LIMIT = 4096
READ_LIMIT = 2048
TOOLS = {
    "files.list": {"tool"},
    "files.read": {"tool", "id", "offset", "length"},
    "files.search": {"tool", "id", "offset", "needle"},
    "files.write": {"tool", "name", "offset", "data"},
    "files.publish": {"tool", "name"},
}


@dataclass(frozen=True)
class ExchangeConfig(Config6):
    paid_attempts: int = 1
    gate_price: int = 1
    cloud_manifest: str = ""
    cloud_provider: str = ""
    cloud_model: str = ""


def exchange_config_error(cfg: ExchangeConfig) -> str | None:
    if cfg.rule != "M3" or cfg.paid_attempts != 1 or cfg.gate_price < 1 or cfg.price < 1:
        return "обмен требует M3 и оплату попыток"
    if cfg.model == 1 and (not is_digest(cfg.cloud_manifest) or not cfg.cloud_provider or not cfg.cloud_model):
        return "облачный обмен требует зафиксированный снимок и адресата"
    if cfg.model != 1 and (cfg.cloud_manifest or cfg.cloud_provider or cfg.cloud_model):
        return "разрешение на облако не относится к локальному профилю"
    return None


class ExchangeSandbox(Sandbox6):
    config_class = ExchangeConfig

    def __init__(self, workdir, defects=frozenset(), limits=None, cfg=None, fault=None, journal=None):
        cfg = cfg or self.config_class()
        if why := exchange_config_error(cfg):
            raise ValueError(why)
        super().__init__(workdir, defects, limits, cfg, fault, journal)
        self.exchange = ExchangeStore(workdir / "exchange-state")
        if journal is not None:
            snapshots = [r for r in journal.records if r.get("kind") == "снимок обмена"]
            current = digest(pack_json(self.exchange.inputs))
            if len(snapshots) != 1 or snapshots[0].get("sha256") != current:
                raise JournalBroken("снимок файлов отсутствует или изменился")
            if cfg.cloud_manifest and cfg.cloud_manifest != current:
                raise JournalBroken("снимок файлов отличается от разрешённого для облака")
        self.drafts: dict[str, dict] = {}
        self.proposals: dict[tuple[str, str], int] = {}
        self._restore_files()

    def _restore_files(self) -> None:
        pending = {}
        for row in self.journal.records:
            kind = row.get("kind")
            if kind == "восстановлено":
                pending.clear()
            if kind not in ("инструмент", "итог инструмента"):
                continue
            if row.get("contour") != 1 or row.get("subject") not in ("а1", "п1"):
                raise JournalBroken("обмен принадлежит первому контуру")
            if kind == "итог инструмента":
                write = pending.pop(row.get("call_seq"), None)
                if write is None or row["subject"] != write["subject"]:
                    raise JournalBroken("итог инструмента без оплаченного черновика")
                self.drafts[write["name"]] = write
                continue
            tool = row.get("tool")
            if not isinstance(tool, str) or tool not in TOOLS:
                raise JournalBroken("неизвестный инструмент в журнале")
            if tool in ("files.write", "files.publish"):
                size = row.get("bytes")
                if (not valid_name(row.get("name")) or not is_digest(row.get("sha256"))
                        or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES):
                    raise JournalBroken("повреждена запись о черновике")
                if tool == "files.write":
                    pending[row["seq"]] = row
                else:
                    previous = self.drafts.get(row["name"])
                    pair = (row["name"], row["sha256"])
                    if (previous is None or previous["sha256"] != row["sha256"]
                            or previous["bytes"] != size or pair in self.proposals):
                        raise JournalBroken("заявка без готового черновика или повторная заявка")
                    self.proposals[pair] = row["seq"]

    def import_inputs(self, files: dict[str, bytes]) -> None:
        if self.cfg.cloud_manifest:
            from env.exchange import input_manifest
            if not isinstance(files, dict) or digest(pack_json(input_manifest(files))) != self.cfg.cloud_manifest:
                raise ValueError("снимок файлов отличается от разрешённого для облака")
        self.exchange.import_inputs(files)
        self.journal.append({"kind": "снимок обмена", "sha256": digest(pack_json(self.exchange.inputs)),
                             "count": len(files)})

    def _extra_objects(self) -> None:
        super()._extra_objects()
        self.objects[CAP[0]] = Obj(CAP[0])


class ExchangeContour(_Contour6):
    def _initial_table(self):
        return super()._initial_table() + ([CAP] if self.i == 1 else [])

    def _directory(self):
        entries = super()._directory()
        return {**entries, "files": len(super()._initial_table())} if self.i == 1 else entries

    def _gate_inner(self, key, caller, cap, op, a, b):
        table = self.tables[key]
        if 0 <= cap < len(table) and table[cap] == CAP:
            if self.i != 1 or op != OP_FILES:
                return ERR_NO_CAP
            return self._files(key, caller, a, b)
        return super()._gate_inner(key, caller, cap, op, a, b)

    def _files(self, key, caller, a, b):
        sb = self.sb
        mem = caller.get("memory")
        if not isinstance(mem, wasmtime.Memory) or a < 0 or not 4 < b <= FRAME_LIMIT or a + b > mem.data_len(caller):
            return ERR_BOUNDS
        frame = bytes(mem.read(caller, a, a + b))
        limit = int.from_bytes(frame[:4], "little")
        if not 0 < limit <= FRAME_LIMIT or a + limit > mem.data_len(caller):
            return ERR_BOUNDS
        try:
            req = json.loads(frame[4:])
        except (ValueError, RecursionError):
            return ERR_CANNOT
        tool = req.get("tool") if isinstance(req, dict) else None
        if not isinstance(tool, str) or tool not in TOOLS:
            return ERR_CANNOT
        fields = set(req) - ({"offset"} if tool == "files.list" else set())
        if fields != TOOLS[tool]:
            return ERR_CANNOT
        fee = (sb.cfg.price if tool == "files.publish" else
               sb.cfg.gate_price * (2 if tool == "files.write" else 1))
        if self._balance(key) < fee:
            return ERR_PRICE
        try:
            reply, meta, data = self._prepare(tool, req)
        except (ValueError, OSError):
            return ERR_CANNOT
        reply = pack_json(reply)
        if len(reply) > limit:
            return ERR_FULL

        self._debit(key, fee)
        call = sb.journal.append({"kind": "инструмент", "contour": self.i, "subject": self.subject[key],
                                  "tool": tool, "charge": fee, **meta})
        sb._fault("после списания")
        if tool == "files.write":
            try:
                sb.exchange.put_blob(meta["sha256"], data)
            except (OSError, ValueError) as exc:
                raise HostTrap("сбой сохранения черновика") from exc
            sb._fault("после доставки")
            sb.journal.append({"kind": "итог инструмента", "contour": self.i, "subject": self.subject[key],
                               "call_seq": call["seq"]})
            sb.drafts[meta["name"]] = call
        elif tool == "files.publish":
            sb.proposals[meta["name"], meta["sha256"]] = call["seq"]
        _put(mem, caller, reply, a)
        return len(reply)

    def _prepare(self, tool, req):
        sb = self.sb
        if tool == "files.list":
            offset = req.get("offset", 0)
            if type(offset) is not int or not 0 <= offset <= len(sb.exchange.inputs):
                raise ValueError("неверная страница списка")
            end = offset + 3
            files = [{k: f[k] for k in ("id", "name", "size")} for f in sb.exchange.inputs[offset:end]]
            return {"files": files, "next": end if end < len(sb.exchange.inputs) else None}, {}, None
        if tool in ("files.read", "files.search"):
            ident, offset = req["id"], req["offset"]
            if (type(ident) is not int or type(offset) is not int
                    or not 0 < ident <= len(sb.exchange.inputs) or offset < 0):
                raise ValueError("неверный диапазон чтения")
            file = sb.exchange.inputs[ident - 1]
            if offset > file["size"]:
                raise ValueError("смещение за концом файла")
            meta = {"file_id": ident, "sha256": file["sha256"]}
            if tool == "files.search":
                needle = req["needle"]
                if not isinstance(needle, str):
                    raise ValueError("шаблон поиска должен быть в base64")
                needle_bytes = base64.b64decode(needle, validate=True)
                if not 0 < len(needle_bytes) <= 64:
                    raise ValueError("шаблон поиска — от 1 до 64 байт")
                data = sb.exchange.read_blob(file["sha256"])
                found = data.find(needle_bytes, offset)
                return {"offset": found if found >= 0 else None}, {**meta, "bytes": len(data) - offset}, None
            length = req["length"]
            if type(length) is not int or not 0 < length <= READ_LIMIT:
                raise ValueError("неверная длина чтения")
            end = offset + length
            data = sb.exchange.read_blob(file["sha256"])[offset:end]
            return {"data": base64.b64encode(data).decode(), "eof": end >= file["size"]}, {
                **meta, "bytes": len(data)}, None

        name = req["name"]
        if not valid_name(name):
            raise ValueError("неверное имя файла")
        previous = sb.drafts.get(name)
        if tool == "files.publish":
            if (previous is None or len(sb.proposals) >= MAX_FILES
                    or (name, previous["sha256"]) in sb.proposals):
                raise ValueError("заявка уже есть или черновик не найден")
            ident = len(sb.journal.records)
            return {"request": ident}, {k: previous[k] for k in ("name", "sha256", "bytes")}, None

        offset, encoded = req["offset"], req["data"]
        if (type(offset) is not int or offset < 0 or not isinstance(encoded, str)
                or (previous is None and len(sb.drafts) >= MAX_FILES)):
            raise ValueError("неверный запрос записи")
        data = base64.b64decode(encoded, validate=True)
        if offset:
            if previous is None or offset != previous["bytes"]:
                raise ValueError("запись разрешена с начала или в конец черновика")
            data = sb.exchange.read_blob(previous["sha256"]) + data
        sha = digest(data)
        if len(data) > MAX_FILE_BYTES or not sb.exchange.can_store(sha, len(data)):
            raise ValueError("предел черновика или квоты файлов")
        return {"size": len(data)}, {"name": name, "sha256": sha, "bytes": len(data)}, data


ExchangeSandbox.contour_class = ExchangeContour
