"""Первая реализация исполнителя с протоколом контракта шага 7 (§1):
строки JSON во входной и выходной поток.

Тот же разбор команд, что в sandbox/process5.py; другое только средство
обмена. Нужен, чтобы обе реализации гонялись одной обвязкой и одним путём
— тогда различие в результатах не может прийти от разных ведущих.

Запуск: python3 -m sandbox.jsonfront --root ПАПКА --kind 5б|6
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import traceback


def to_json(x):
    if isinstance(x, (bytes, bytearray)):
        return {"b64": base64.b64encode(bytes(x)).decode()}
    if isinstance(x, (list, tuple)):
        return [to_json(v) for v in x]
    if isinstance(x, dict):
        return {str(k): to_json(v) for k, v in x.items()}
    return x


def from_json(x):
    if isinstance(x, dict):
        if set(x) == {"b64"}:
            return base64.b64decode(x["b64"])
        return {k: from_json(v) for k, v in x.items()}
    if isinstance(x, list):
        return [from_json(v) for v in x]
    return x


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--kind", required=True, choices=["5б", "6"])
    ap.add_argument("--model", help='шаг 9: режим процесса модели, напр. "--stub" или "--gguf ФАЙЛ"')
    ap.add_argument("--model-timeout", type=float, default=120.0)
    # Шаг 9б (контракт §18): чем обеспечено «без сети» у процесса модели.
    # Без --model не действует: изолировать нечего.
    ap.add_argument("--model-net", default="netns", choices=["netns", "probe"])
    a = ap.parse_args()
    from sandbox.executor5 import Limits
    from sandbox.executor5b import Crash
    from sandbox.model9 import start_links
    from sandbox.process5 import _handle_factory, _Refused

    class _NoAudit:
        events: list[str] = []

    try:
        links = start_links({"mode": a.model.split(), "timeout": a.model_timeout, "net": a.model_net}
                            if a.model else None)
    except RuntimeError as e:
        sys.exit(f"исполнитель не запускается: {e}")
    handle, state = _handle_factory(a.kind)
    state.update(root=a.root, defects=[], limits=Limits(), audit=_NoAudit(), model_links=links)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            args = from_json(req.get("args", []))
            if req.get("cmd") in ("new", "recover", "restart") and len(args) > 1 and isinstance(args[1], list):
                args[1] = tuple(args[1])
            reply = {"status": "ok", "value": to_json(handle((req.get("cmd"), *args)))}
        except _Refused as e:
            reply = {"status": "отказ", "value": str(e)}
        except Crash:
            os._exit(17)
        except Exception:
            reply = {"status": "сбой", "value": traceback.format_exc(limit=6)}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
