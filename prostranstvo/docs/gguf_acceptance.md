# Положительная приёмка Bubblewrap и GGUF

На Linux под обычной учётной записью установите Bubblewrap и закреплённый
`llama-cpp-python` из зависимостей проекта. Из каталога `prostranstvo`
запустите:

```sh
.venv/bin/python3 -m experiments.gguf_acceptance /path/to/trusted.gguf
```

Приёмка выводит фактическую версию `llama-cpp-python`. Если пакет уже был
установлен в `.venv`, `start.py --local` использует эту копию.

Первая проверка использует те же параметры Bubblewrap, что экран.
Она проверяет отдельную сеть и PID-пространство, доступность весов на
чтение, скрытие двух тестовых файлов и переменной окружения. Вторая
запускает штатный `serve.py` и проверяет живой ответ и завершение процесса.

Проверено в гостевой Debian x86-64 с ядром `6.12.117+deb13-cloud-amd64`,
Bubblewrap 0.12.0 и llama-cpp-python 0.3.37. Гость загружался в QEMU 10.0.13
через TCG, без сетевого устройства. Для оценки производительности или
для других версий ядра требуется самостоятельный замер. [QEMU TCG](https://www.qemu.org/docs/master/system/introduction.html)

Использована тестовая модель [stories260K.gguf](https://huggingface.co/ggml-org/models-moved/blob/main/tinyllamas/stories260K.gguf)
с SHA-256 `270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d`.

Вывод последнего прогона — `BWRAP_BOUNDARY_OK`,
`BWRAP_LIVE_GGUF_OK 83 bytes`, код завершения 0.

Пакет `llama-cpp-python` и встроенный `llama.cpp` учитываются отдельно.
CVE‑2024‑34359 в шаблонах чата Python-пакета затрагивала версии
0.2.30–0.2.71. Исправление выпущено в 0.2.72. Нативный разбор GGUF
проверяется по бюллетеням поставляемой ревизии `llama.cpp`. Прохождение
приёмки подтверждает заявленные границы запуска на проверенном Linux.
[Python-пакет](https://github.com/advisories/GHSA-56xg-wfcc-g829) ·
[Нативный движок](https://github.com/ggml-org/llama.cpp/security/advisories)
