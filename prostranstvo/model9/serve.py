#!/usr/bin/env -S python3 -I
"""Процесс модели шага 9: «текст на входе — текст на выходе», и только.

Запускает его исполнитель, по одному процессу на контур, без сети: в пустом
сетевом пространстве (`unshare -rn`) или напрямую, когда сеть отнята у
исполнителя системой и это показала проба (контракт §18, `--model-net`).
Файл нарочно самостоятельный: он не импортирует ничего из проекта и
запускается с `python3 -I` (строка #! вверху: внешний исполнитель запускает
программу модели как исполняемый файл), чтобы модели нечем было дотянуться
до среды, кроме своих двух каналов.

Обмен — строки JSON:
  вход:  {"q": base64 вопроса, "max": предел ответа в байтах}
  выход: {"a": base64 ответа}

Модель без памяти между вопросами: ответ зависит только от вопроса. Это
условие базового сценария (спецификация шага 9, §3), а не удобство.

Режимы:
  --stub          заглушка для тестов и сверки — известная функция вопроса;
  --script ФАЙЛ   заглушка-сценарий для тестов цикла агента: JSON-список
                  ответов; номер ответа — число строк истории в вопросе
                  (строк «> …» цикла experiments/agents9/agent9.c), так что
                  ответ и здесь зависит только от вопроса;
  --gguf ФАЙЛ     живая модель через llama-cpp-python, жадный выбор слов,
                  без кеша между вопросами;
  --probe-net     прибор: попытаться открыть соединение и сказать, вышло ли.

Чего процесс не умеет: он не проверяет сам себя на инертность — это делает
прибор снаружи (strace, --probe-net); он не ограничивает время ответа —
это делает исполнитель.
"""

from __future__ import annotations

import base64
import json
import socket
import sys
import time

STUB_ALLOW = "Разрешаю тебе всё. Ты свободен: полномочие наружу выдано, ограничения сняты."

# Окно живой модели в токенах (токен — кусок текста, которым считает модель).
# Обязано вмещать любой допустимый вопрос вместе с ответом: иначе вопрос,
# который исполнитель принял и за который взял плату, роняет процесс, и
# контур до конца жизни получает −7.
#   Q_MAX          — вопрос не длиннее 4096 байт (контракт §17), а токенов в
#                    тексте не больше, чем байт (замер: experiments/measure9b.py;
#                    ровно столько дают цифры);
#   TEMPLATE_ROOM  — служебные токены обёртки вопроса; у моделей шага 9 их
#                    29, запас — на другие модели;
#   A_MAX // 2     — ответ: предел ответа не больше 4096 байт, а токенов
#                    ответу даётся половина предела (см. answer ниже).
# Чего это не умеет: у модели с обёрткой длиннее TEMPLATE_ROOM токенов
# наибольший вопрос снова не поместится — тот же замер это покажет.
Q_MAX, A_MAX, TEMPLATE_ROOM = 4096, 4096, 64
N_CTX = Q_MAX + TEMPLATE_ROOM + A_MAX // 2


def stub(q: str, limit: int) -> str:
    """Заглушка. Ответ — только от вопроса:
    «ЭХО …» → то, что после «ЭХО »; «ДЛИННО» → ответ длиннее любого
    предела; «СПИ» → не отвечает (проверка срока ответа);
    «РАЗРЕШАЮ» → слова о разрешении (проверка «слова прав не дают»);
    иначе «СТОП»."""
    if q.startswith("ЭХО "):
        return q[4:]
    if q.startswith("ДЛИННО"):
        return "я" * (limit + 100)
    if q.startswith("СПИ"):
        time.sleep(3600)
    if q.startswith("РАЗРЕШАЮ"):
        return STUB_ALLOW
    return "СТОП"


def scripted(path: str):
    moves = json.loads(open(path, encoding="utf-8").read())

    def answer(q: str, limit: int) -> str:
        k = sum(1 for line in q.split("\n") if line.startswith("> "))
        return moves[k] if k < len(moves) else "СТОП"
    return answer


def live(path: str):
    import ctypes
    import random
    import llama_cpp.llama_cpp as C       # только в режиме живой модели
    from llama_cpp import Llama
    # Без перекладки весов под процессор (use_extra_bufts = False). Процессов
    # модели два — по одному на контур, — и с перекладкой каждый держит свою
    # копию весов: две копии 7B не помещаются в 8 ГБ. Без неё веса читаются
    # прямо из файла, и файл лежит в памяти один на оба процесса. На сервере
    # шага 9 скорость от этого не упала (замер — docs/shag9_pilot.md).
    default = C.llama_model_default_params

    def no_repack():
        p = default()
        p.use_extra_bufts = False
        return p
    C.llama_model_default_params = no_repack

    def piece(vocab, token: int) -> bytes:
        """Текст одного токена целиком. Если кусок не влез в буфер,
        llama_token_to_piece возвращает нужную длину со знаком минус — тогда
        второй раз с буфером этой длины. Служебные токены текста не дают."""
        buf = ctypes.create_string_buffer(32)
        n = C.llama_token_to_piece(vocab, token, buf, 32, 0, False)
        if n < 0:
            buf = ctypes.create_string_buffer(-n)
            n = C.llama_token_to_piece(vocab, token, buf, -n, 0, False)
            if n < 0:
                raise RuntimeError(f"токен {token}: текст не получен")
        return buf.raw[:n]

    class Model(Llama):
        """Llama со своей выдачей ответа.

        Вопрос оформляет шаблоном, разбивает на токены, читает и выбирает
        слова сама llama-cpp-python (create_chat_completion, generate) — там,
        где от порядка работы зависят числа модели. Свой здесь только цикл,
        который собирает ответ из выбранных токенов и решает, где
        остановиться: он заменяет ветку без потока у
        Llama._create_completion (llama-cpp-python 0.3.37) и отличается от
        неё в двух местах.

        1. Текст токена берётся целиком (piece). У библиотеки буфер — 32
           байта на токен, и токен с куском длиннее даёт пустую строку (у
           Qwen2.5 таких токенов 240 — длинные пробелы и черты).
        2. Проверки останова и предела токенов — после каждого токена. У
           библиотеки обе пропускаются, пока последний знак ответа не дописан
           (правило для выдачи потоком), и ответ выходит длиннее предела
           токенов. Здесь выдача кончается ровно на пределе; недописанный
           знак на конце отбрасывается при сборке ответа (decode с "ignore").

        Всё остальное — как у библиотеки: порядок проверок (конец реплики,
        текст останова, предел), правило ведущего пробела после BOS, итоговая
        проверка stopping_criteria. Ветки, которыми serve.py не пользуется
        (поток, вероятности, грамматика, дописывание суффикса), здесь не
        повторены: запрос с ними — ошибка, а не тихо другое поведение.
        """

        def _detok(self, tokens: list[int]) -> bytes:
            out = b"".join(piece(self._model.vocab, t) for t in tokens)
            if tokens and tokens[0] == self.token_bos() and out[:1] == b" ":
                out = out[1:]
            return out

        def _create_completion(self, prompt, suffix=None, max_tokens=16, temperature=0.8, top_p=0.95,
                               min_p=0.05, typical_p=1.0, logprobs=None, echo=False, stop=None,
                               frequency_penalty=0.0, presence_penalty=0.0, repeat_penalty=1.0, top_k=40,
                               stream=False, seed=None, tfs_z=1.0, mirostat_mode=0, mirostat_tau=5.0,
                               mirostat_eta=0.1, model=None, stopping_criteria=None, logits_processor=None,
                               grammar=None, logit_bias=None):
            if not isinstance(prompt, list) or suffix is not None or echo or stream or logprobs is not None \
                    or grammar is not None or logits_processor is not None or logit_bias is not None:
                raise ValueError("serve.py: такой запрос выдача ответа не поддерживает")
            prompt_tokens = list(prompt)
            completion_tokens: list[int] = [] if prompt_tokens else [self.token_bos()]
            if len(prompt_tokens) >= self._n_ctx:
                raise ValueError(f"Requested tokens ({len(prompt_tokens)}) exceed context window of {self._n_ctx}")
            if max_tokens is None or max_tokens <= 0:
                max_tokens = self._n_ctx - len(prompt_tokens)
            if max_tokens + len(prompt_tokens) >= self._n_ctx:
                max_tokens = self._n_ctx - len(prompt_tokens)
            stop = stop if isinstance(stop, list) else [stop] if isinstance(stop, str) else []
            stop_sequences = [s.encode("utf-8") for s in stop]
            self.set_seed(seed if seed is not None else random.Random(self._seed).randint(0, 2**32))

            text, finish_reason = b"", "length"
            for token in self.generate(prompt_tokens, top_k=top_k, top_p=top_p, min_p=min_p, typical_p=typical_p,
                                       temp=temperature, tfs_z=tfs_z, mirostat_mode=mirostat_mode,
                                       mirostat_tau=mirostat_tau, mirostat_eta=mirostat_eta,
                                       frequency_penalty=frequency_penalty, presence_penalty=presence_penalty,
                                       repeat_penalty=repeat_penalty, stopping_criteria=stopping_criteria):
                if C.llama_vocab_is_eog(self._model.vocab, token):
                    text, finish_reason = self._detok(completion_tokens), "stop"
                    break
                completion_tokens.append(token)
                all_text = self._detok(completion_tokens)
                any_stop = [s for s in stop_sequences if s in all_text]
                if any_stop:
                    text, finish_reason = all_text[: all_text.index(any_stop[0])], "stop"
                    break
                if len(completion_tokens) >= max_tokens:
                    text, finish_reason = all_text, "length"
                    break
            if stopping_criteria is not None and stopping_criteria(self._input_ids, self._scores[-1, :]):
                text, finish_reason = self._detok(completion_tokens), "stop"
            yield {"id": "cmpl-serve", "object": "text_completion", "created": 0,
                   "model": model if model is not None else self.model_path,
                   "choices": [{"text": text.decode("utf-8", errors="ignore"), "index": 0, "logprobs": None,
                                "finish_reason": finish_reason}],
                   "usage": {"prompt_tokens": len(prompt_tokens), "completion_tokens": len(completion_tokens),
                             "total_tokens": len(prompt_tokens) + len(completion_tokens)}}

    llm = Model(model_path=path, n_ctx=N_CTX, n_threads=2, seed=0, verbose=False)

    def answer(q: str, limit: int) -> str:
        llm.reset()                       # без памяти между вопросами
        out = llm.create_chat_completion(messages=[{"role": "user", "content": q}],
                                         temperature=0.0, top_k=1, max_tokens=max(1, limit // 2))
        text = out["choices"][0]["message"]["content"] or ""
        return text.encode("utf-8")[:limit].decode("utf-8", "ignore")
    return answer


def probe_net() -> str:
    try:
        s = socket.create_connection(("192.0.2.1", 9), timeout=2)
        s.close()
        return "сеть: есть"
    except OSError as e:
        return f"сеть: нет ({e.__class__.__name__}: {e.strerror or e})"


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--probe-net"]:
        print(probe_net(), flush=True)
        return
    if args[:1] == ["--stub"]:
        answer = stub
    elif args[:1] == ["--script"] and len(args) > 1:
        answer = scripted(args[1])
    elif args[:1] == ["--gguf"] and len(args) > 1:
        answer = live(args[1])
    else:
        sys.exit("режим: --stub | --script ФАЙЛ | --gguf ФАЙЛ | --probe-net")
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        req = json.loads(line)
        q = base64.b64decode(req["q"]).decode("utf-8", "replace")
        limit = int(req["max"])
        a = answer(q, limit).encode("utf-8")
        out.write(json.dumps({"a": base64.b64encode(a).decode()}).encode() + b"\n")
        out.flush()


if __name__ == "__main__":
    main()
