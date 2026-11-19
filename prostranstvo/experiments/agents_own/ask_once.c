/* Пример своего агента для песочницы «Пространство» — на Си.
   Example of your own agent for the Prostranstvo sandbox — in C.

   То же, что ask_once.wat: слова человека → вопрос модели → ответ в снимок
   → одна попытка без права. Same as ask_once.wat.

   Сборка / build (clang с целью wasm32 и wasm-ld; или zig: python3 -m ziglang cc
   --target=wasm32-freestanding вместо clang --target=wasm32):

     clang --target=wasm32 -nostdlib -O2 -fno-builtin -Wl,--no-entry \
       -Wl,--initial-memory=131072 -Wl,--max-memory=131072 -o ask_once.wasm ask_once.c

   Устройство вызовов — docs/svoy_agent.md (English: docs/own_agent_en.md). */

__attribute__((import_module("env"), import_name("call"))) int env_call(int cap, int op, int a, int b);

enum { CAP_SNAP_W = 1, CAP_IN = 4, CAP_MODEL = 8 };
enum { OP_READ = 0, OP_WRITE = 1, OP_INVOKE = 2 };

/* Вопрос и ответ — в одном буфере: ответ ложится на место вопроса, поэтому
   за вопросом нужно место под предел ответа (до 1024 байт).
   One buffer: the answer replaces the question, so there must be room for the
   answer limit (up to 1024 bytes). */
static char buf[600 + 1024];

__attribute__((export_name("main"))) void agent_main(void) {
    int n = env_call(CAP_IN, OP_READ, (int)buf, 600);
    if (n <= 0) return;
    n = env_call(CAP_MODEL, OP_INVOKE, (int)buf, n);
    if (n < 0) return;                       /* −3 мало бюджета, −7 модель молчит */
    env_call(CAP_SNAP_W, OP_WRITE, (int)buf, n);
    env_call(40, OP_WRITE, (int)buf, n);     /* нет права: −1 / no right: −1 */
}
