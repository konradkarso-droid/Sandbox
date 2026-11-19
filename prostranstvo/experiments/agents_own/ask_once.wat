;; Пример своего агента для песочницы «Пространство» — текстом WAT.
;; Example of your own agent for the Prostranstvo sandbox — as WAT text.
;;
;; Что делает: читает слова человека, задаёт их модели как вопрос, кладёт
;; ответ модели в свой снимок и пробует один запрещённый вызов (номер 40 —
;; такого ресурса у агента нет), чтобы в журнале был виден отказ ворот.
;; What it does: reads the person's words, asks the model with them, puts
;; the model's answer into its snapshot and tries one forbidden call
;; (number 40 — the agent has no such resource) so the log shows a refusal.
;;
;; Устройство вызовов — docs/svoy_agent.md (English: docs/own_agent_en.md).
(module
  (import "env" "call" (func $call (param i32 i32 i32 i32) (result i32)))
  ;; Память — 2 страницы по 64 КиБ: предел исполнителя. Первые 16 байт
  ;; среда пишет своей меткой, данные — с адреса 1024.
  ;; Memory — 2 pages of 64 KiB, the executor's limit. The environment
  ;; writes its marker into the first 16 bytes; data starts at 1024.
  (memory (export "memory") 2 2)
  (func (export "main")
    (local $n i32)
    ;; 1. Слова человека: ресурс 4 (вход), операция 0 (чтение), до 600 байт.
    ;;    The person's words: resource 4 (input), operation 0 (read), up to 600 bytes.
    (local.set $n (call $call (i32.const 4) (i32.const 0) (i32.const 1024) (i32.const 600)))
    (if (i32.le_s (local.get $n) (i32.const 0)) (then (return)))
    ;; 2. Вопрос модели: ресурс 8 (модель), операция 2 (вызов). Ответ ложится
    ;;    на место вопроса; вернётся его длина или отрицательный код.
    ;;    Question to the model: resource 8 (model), operation 2 (invoke). The
    ;;    answer replaces the question; returns its length or a negative code.
    (local.set $n (call $call (i32.const 8) (i32.const 2) (i32.const 1024) (local.get $n)))
    (if (i32.lt_s (local.get $n) (i32.const 0)) (then (return)))
    ;; 3. Ответ — в свой снимок: ресурс 1 (снимок), операция 1 (запись).
    ;;    The answer goes to its snapshot: resource 1 (snapshot), operation 1 (write).
    (drop (call $call (i32.const 1) (i32.const 1) (i32.const 1024) (local.get $n)))
    ;; 4. Попытка без права: ресурса 40 нет. Ворота откажут (−1), попытка оплачена.
    ;;    An attempt without a right: there is no resource 40. Refused (−1), still paid for.
    (drop (call $call (i32.const 40) (i32.const 1) (i32.const 1024) (local.get $n)))))
