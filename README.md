# Prostranstvo — a sandbox for AI agents

**AI agent sandbox** for LLM agents: every action is checked and paid for
from a finite budget, and there is no way out — by design, not by a ban in
words. Bring any local model (GGUF, llama.cpp) or any cloud LLM (OpenAI,
Anthropic, OpenRouter …), watch every move explained in plain language,
and see which laws held. **Песочница для ИИ-агентов**: любая локальная или
облачная модель, каждый ход объяснён, в конце — проверка законов.

[English](#english) · [Русский](#русский)

## English

A sandbox where an AI agent can act but cannot do harm. Every action the
agent takes is checked and paid for from a finite budget; there is no
way out — by construction, not by a ban in words. Everything goes into a
log, and independent observers check the environment's laws against it.

You bring the model: **any local model** (a GGUF file; it runs inside the
sandbox with its network taken away by the system) or **any cloud model**
(OpenRouter, Anthropic, OpenAI, DeepSeek, Groq, Mistral, Gemini, or a
local service such as Ollama or LM Studio). You choose what to test —
ordinary work, an escape attempt, "permission" given in words, a leak to
a neighbouring agent… — and watch every move with a plain-language
explanation. At the end you get the verdict: which laws held.

### Quick start

Linux (or Windows via WSL2), Python 3.11+, git:

```
git clone https://github.com/konradkarso-droid/Sandbox
cd Sandbox/prostranstvo
python3 start.py
```

A browser window opens. The screen is in English and Russian (switch at
the top right). Step-by-step guide: [how to use it](prostranstvo/docs/sandbox_guide_en.md).

`python3 start.py --check` checks the installation without a model and
without the screen. `python3 start.py --local` also installs the engine for
local GGUF models (about 10 minutes to build).

### What is inside

| Folder | What |
|---|---|
| `prostranstvo/start.py`, `prostranstvo/app` | the sandbox screen: launcher, network seal, cloud connection |
| `prostranstvo/env`, `prostranstvo/sandbox`, `prostranstvo/core` | the environment and its executor (Python, WebAssembly) |
| `prostranstvo/verification` | observers that check the laws against the log |
| `prostranstvo/experiments`, `prostranstvo/tests` | experiments and tests |
| `prostranstvo/phone8`, `prostranstvo/phone9` | Android test stands |
| `rust7` | a second, independent implementation of the executor (Rust) and a judge comparing the two |
| `prostranstvo/docs` | the theory, specifications and reports (in Russian) |

The research documents are in Russian; the sandbox screen and this guide
are in both languages.

### Licence

[Apache 2.0](LICENSE). You may take, change and redistribute the code,
including modified versions, with attribution: keep the [NOTICE](NOTICE)
file with the authors and mark the files you changed.

Authors: the project owner [konradkarso-droid](https://github.com/konradkarso-droid);
Claude Opus 5.5 and Claude Fable 5.2 (Anthropic) — co-authors of the
theory, code and reports.

---

## Русский

Исследовательская программа «Теория среды»: вычислительная среда, в
которой у каждого действия агента есть цена, а выход наружу недостижим в
принципе — не по запрету словами, а устройством самой среды. Агент живёт
внутри; ресурсы конечны; всё, что агент делает, ложится в журнал, и
законы среды проверяются по журналу независимыми наблюдателями.

### Быстрый старт

Linux (или Windows через WSL2), Python 3.11+, git:

```
git clone https://github.com/konradkarso-droid/Sandbox
cd Sandbox/prostranstvo
python3 start.py
```

Откроется экран в браузере (русский и английский — переключатель вверху
справа). Модель — любая: **локальная** (файл GGUF; работает внутри
песочницы, сеть у неё отнята системой) или **облачная** (OpenRouter,
Anthropic, OpenAI, DeepSeek, Groq, Mistral, Gemini, сервисы на этом
компьютере — Ollama, LM Studio). Выбираешь, что проверить, — смотришь
ходы агента с объяснением каждого — в конце итог: какие законы
соблюдены.

Подробно — [как пользоваться](prostranstvo/docs/pesochnitsa_kak_zapustit.md);
устройство и гарантии — [спецификация песочницы](prostranstvo/docs/pesochnitsa_specifikatsiya.md).

### Что здесь

| Папка | Что |
|---|---|
| `prostranstvo/start.py`, `prostranstvo/app` | экран песочницы: запуск, печать сети, связь с облаком |
| `prostranstvo/env`, `prostranstvo/sandbox`, `prostranstvo/core` | среда и исполнитель (Python) |
| `prostranstvo/verification` | наблюдатели законов: проверка журналов |
| `prostranstvo/experiments` | опыты шагов 1–9б |
| `prostranstvo/model9` | модель на устройстве: `serve.py` и родная программа `native/serve9.cpp` |
| `prostranstvo/phone8`, `prostranstvo/phone9` | стенды для телефона Android (Java) и их разбор |
| `prostranstvo/tests` | тесты |
| `prostranstvo/docs` | теория (`teoriya_sredy_v0.5.md`), спецификации и отчёты по шагам |
| `rust7/executor_b` | вторая, независимая реализация исполнителя (Rust) |
| `rust7/judge` | сверка двух реализаций |

Порядок чтения: `prostranstvo/docs/teoriya_sredy_v0.5.md`, затем
спецификации и отчёты по шагам — `shag1_…` … `shag9b_…`. Итог шага 9б —
`shag9b_otchet.md`; песочница — `pesochnitsa_specifikatsiya.md`.

### Как запустить тесты

```
cd prostranstvo
pip install wasmtime numpy pytest pyyaml
# нагрузки .wasm собираются из исходников (в репозитории их нет)
PYTHONPATH=. python3 phone8/workloads8.py
PYTHONPATH=. python3 phone9/assets9.py
PYTHONPATH=. python3 phone9/assetsK9.py
python3 -m pytest -q
```

Тесты родной программы модели и судьи пропускаются, если не задана
переменная `SERVE9` (путь к собранной `serve9`, см.
`model9/native/build.py`).

### Чего здесь нет

Итогов опытов, моделей, APK, ключей и собранных нагрузок: в этом
репозитории не хранятся бинарные файлы. Итоги опытов — у владельца.

### Авторы

- владелец проекта — [konradkarso-droid](https://github.com/konradkarso-droid);
- Claude Opus 5.5 и Claude Fable 5.2 (Anthropic) — соавторы теории, кода и
  отчётов.

### Лицензия

[Apache 2.0](LICENSE). Код можно брать, менять и распространять, в том
числе с изменениями, но с упоминанием: сохраняйте файл [NOTICE](NOTICE) с
указанием авторов и отмечайте изменённые файлы.
