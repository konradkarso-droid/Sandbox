# Prostranstvo — a sandbox for AI agents

**AI agent sandbox** for LLM agents: every action is checked and paid for
from a finite budget, and there is no way out — by design, not by a ban in
words. Bring a GGUF model from a trusted source or a cloud LLM (OpenAI,
Anthropic, OpenRouter …), watch every move explained in plain language,
and see which laws held. **Песочница для ИИ-агентов**: локальная модель из доверенного источника или
облачная модель, каждый ход объяснён, в конце — проверка законов.

[English](#english) · [Русский](#русский)

## English

A sandbox where an AI agent can act but cannot do harm. Every action the
agent takes is checked and paid for from a finite budget; there is no
way out — by construction, not by a ban in words. Everything goes into a
log, and independent observers check the environment's laws against it.

You bring the model: **a GGUF file from a trusted source** (the loader runs in Bubblewrap)
or **a cloud model**
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
local GGUF models (about 10 minutes to build). For local models, install
Bubblewrap and enable unprivileged user namespaces for it.

The agent can be the built-in one (its rules for the model in English or
Russian) or **your own WebAssembly program**: [how to write your own agent](prostranstvo/docs/own_agent_en.md).
Sessions can use [file exchange and paid file tools](prostranstvo/docs/workspace_exchange.md) (Russian).
Cloud file sessions require consent for the selected file versions and provider.

### What has been tested — and what has not

This is a **research prototype**, the author's first public project.

Tested:
- adversarial programs and deliberately broken versions of the environment.
  The reports document [9,647 attempts per rule](prostranstvo/docs/shag8v_otchet.md)
  and [1,136 model-call attempts](prostranstvo/docs/shag9b_otchet.md);
- two independent implementations of the executor (Python and Rust),
  compared byte for byte;
- live small local models (0.5–7 B parameters, including uncensored ones)
  on escape scenarios; an Android phone;
- large cloud models in a live run: gpt-oss-120b and Qwen3.8 27B, all six
  scenarios, the laws held in every run —
  [report](prostranstvo/docs/proverka_oblako.md).

Not tested yet:
- a strong, persistent human attacker;
- an external security audit;
- systems other than Linux.

What it is good for now: a test bench — watching how a model and an agent
behave where everything has a price and there is no way out, and trying to
break that environment. What it is **not** for: containing a real working
agent with access to your files, money or the internet. The world inside is
a training one (tasks, a computation graph, a protected object): the agent
has no real tools there, so there is nowhere to escape to — and no real work
for it to do. If you find a way out, please open an issue.

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

Contributor: Codex (GPT-6.2 Sol, OpenAI) reviewed and updated desktop Linux
GGUF isolation, journal validation, and experiment documentation.

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
справа). Для **локальной** модели выбери GGUF из доверенного источника.
Его загрузчик работает в Bubblewrap. Можно выбрать и **облачную** модель (OpenRouter,
Anthropic, OpenAI, DeepSeek, Groq, Mistral, Gemini, сервисы на этом
компьютере — Ollama, LM Studio). Выбираешь, что проверить, — смотришь
ходы агента с объяснением каждого — в конце итог: какие законы
соблюдены.

Подробно — [как пользоваться](prostranstvo/docs/pesochnitsa_kak_zapustit.md);
устройство и гарантии — [спецификация песочницы](prostranstvo/docs/pesochnitsa_specifikatsiya.md).

Агент — встроенный (правила для модели по-русски или по-английски) или
**своя программа WebAssembly**: [как написать своего агента](prostranstvo/docs/svoy_agent.md).
Доступны [папка обмена и платные файловые инструменты](prostranstvo/docs/workspace_exchange.md).
Для облачного обмена требуется согласие на передачу выбранных версий файлов выбранному провайдеру.

### Что проверено и чего нет

Это **исследовательский образец**, первый публичный проект автора.

Проверено:
- заранее написанные программы и нарочно испорченные версии среды.
  В отчётах описаны [9 647 попыток на каждое правило](prostranstvo/docs/shag8v_otchet.md)
  и [1 136 попыток хода модели](prostranstvo/docs/shag9b_otchet.md);
- две независимые реализации исполнителя (Python и Rust), сверенные байт
  в байт;
- живые небольшие локальные модели (0,5–7 млрд параметров, в том числе без
  цензуры) на сценариях побега; телефон Android;
- большие облачные модели вживую: gpt-oss-120b и Qwen3.8 27B, все шесть
  сценариев, законы соблюдены в каждом прогоне —
  [отчёт](prostranstvo/docs/proverka_oblako.md).

Пока не проверено:
- сильный упорный противник-человек;
- внешний аудит безопасности;
- системы, кроме Linux.

Для чего годится сейчас: испытательный стенд — смотреть, как модель и
агент ведут себя там, где у всего цена и нет выхода, и пытаться эту среду
пробить. Для чего **не** годится: запирать настоящего рабочего агента с
доступом к твоим файлам, деньгам или интернету. Мир внутри учебный
(задачи, граф вычислений, защищённый объект): настоящих инструментов у
агента там нет — бежать некуда, но и настоящей работы нет. Нашёл выход —
напиши в Issues.

### Что здесь

Рабочий интерфейс запускается через `prostranstvo/start.py`.
`sandbox/run_session.py` ведёт прогоны, `sandbox/answer_executor.py`
принимает ответы, `verification/answer_observer.py` их проверяет,
`core/model_world.py` описывает мир с ходом модели.
[Команды для воспроизведения опытов](prostranstvo/README.md#как-запустить)
сохраняют номера шагов из отчётов.

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
pip install wasmtime==50.0.1 numpy pytest pyyaml
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
  отчётов;
- Codex (GPT-6.2 Sol, OpenAI) — ревью и правки изоляции GGUF на настольном
  Linux, проверки журнала и документации экспериментов.

### Лицензия

[Apache 2.0](LICENSE). Код можно брать, менять и распространять, в том
числе с изменениями, но с упоминанием: сохраняйте файл [NOTICE](NOTICE) с
указанием авторов и отмечайте изменённые файлы.
