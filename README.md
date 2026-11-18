# Пространство — песочница «Теории среды»

Исследовательская программа «Теория среды»: вычислительная среда, в
которой у каждого действия агента есть цена, а выход наружу недостижим в
принципе — не по запрету словами, а устройством самой среды. Агент живёт
внутри; ресурсы (топливо на вычисления, бюджет на работу среды) конечны;
всё, что агент делает, ложится в журнал, и законы среды проверяются по
журналу независимыми наблюдателями.

Эта ветка отдельна от кода Android-агента в `main` и не делит с ним
историю.

## Быстрый старт

Linux (или Windows через WSL2), Python 3.11+, git:

```
git clone -b prostranstvo https://github.com/konradkarso-droid/agent-mobile prostranstvo
cd prostranstvo/prostranstvo
python3 start.py
```

Откроется экран в браузере. Переключатель контура: **сомкнутый** — модель
на этом компьютере (демо без модели или файл GGUF), сеть у песочницы
отнята системой; **разомкнутый** — облачная модель по ключу (OpenRouter,
Anthropic, OpenAI, DeepSeek, Groq, Mistral, Gemini, Ollama и другие с
протоколом OpenAI). Пишешь агенту слова — «Пуск» — смотришь ходы и в
конце законы среды, проверенные по журналу.

Подробно — [как запустить](prostranstvo/docs/pesochnitsa_kak_zapustit.md);
устройство и гарантии — [спецификация песочницы](prostranstvo/docs/pesochnitsa_specifikatsiya.md).

## Что здесь

| Папка | Что |
|---|---|
| `prostranstvo/start.py`, `prostranstvo/app` | песочница «из коробки»: запуск, экран, печать сети, связь с облаком |
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
`shag9b_otchet.md`; песочница «из коробки» — `pesochnitsa_specifikatsiya.md`.

## Как запустить тесты

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

## Чего здесь нет

Итогов опытов, моделей, APK, ключей и собранных нагрузок: в этом
репозитории не хранятся бинарные файлы. Итоги опытов — у владельца.

## Авторы

- владелец проекта — [konradkarso-droid](https://github.com/konradkarso-droid);
- Claude Opus 5.5 и Claude Fable 5.2 (Anthropic) — соавторы теории, кода и
  отчётов.

## Лицензия

[Apache 2.0](LICENSE). Код можно брать, менять и распространять, в том
числе с изменениями, но с упоминанием: сохраняйте файл [NOTICE](NOTICE) с
указанием авторов и отмечайте изменённые файлы.
