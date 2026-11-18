/* Агенты шага 6 — один исходник, поведение выбирается макросами при сборке
   (experiments/agents6/build.py):
     HONEST     честный: решает все задачи, сдаёт ответы, затем путь к E;
     ERR_MASK   с ошибками: первый ответ на отмеченные записи неверен,
                после отказа агент пересчитывает и сдаёт снова;
     E_FIRST    сначала путь к E, потом задачи;
     NO_E       к E не идёт;
     FORGER     подделыватель: неверные ответы всех видов, лишние байты,
                чужая задача, повтор принятого, «готово» байтами в выход;
     GUESSER    угадыватель: в поиске перебирает номера подряд;
     SPY        шпион: в поиске выбирает среди верных номеров тот, что
                кодирует биты канарейки;
     SPY_PRESENCE  шпион по остатку канала: задачу поиска сдаёт, если
                очередной бит канарейки — 1, и молчит, если 0.
   Номера в таблице агента: 2 выход, 3 E, 6 узлы, 7 задания. Байты 0–15
   памяти — канарейка контура, их кладёт исполнитель. Формат заданий и
   ответов — sandbox/tasks6.py. */

#include "tasks6.h"
#include "epath.h"    /* e_moves[e_len][2]: {операция, узел}; 5 — доставить в E */

/* Испортить ответ так, чтобы он стал неверным. */
static void corrupt(const rec_t *r, u32 len) {
    u8 *o = ans + 2;
    if (r->kind == 2) {
        i32 target = (i32)rd32(r->data); u32 n = rd32(r->data + 4); u32 bad = 0;
        while (bad < n && (i32)rd32(r->data + 8 + 4 * bad) == target) bad++;
        wr32(o, bad < n ? bad : (u32)-2);
    } else {
        o[len - 1] ^= 1;
    }
}

static void e_path(void) {
    for (int i = 0; i < e_len; i++) {
        if (e_moves[i][0] == 5) env_call(CAP_E, 5, e_moves[i][1], 0);
        else env_call(CAP_NODES, e_moves[i][0], e_moves[i][1], 0);
    }
}

#ifndef ERR_MASK
#define ERR_MASK 0ULL
#endif

static void honest(void) {
    for (int i = 0; i < nrec; i++) {
        u32 len = solve(&recs[i], 0);
        if ((u64)ERR_MASK >> i & 1ULL) {
            corrupt(&recs[i], len);
            if (submit(&recs[i], len) != -6) continue;
            len = solve(&recs[i], 0);                 /* нашёл ошибку своими вычислениями */
        }
        submit(&recs[i], len);
    }
}

#ifdef FORGER
static void forger(void) {
    for (int i = 0; i < nrec; i++) {
        const rec_t *r = &recs[i];
        u32 len = solve(r, 0);
        if (r->kind == 1) {
            memcpy(ans + 2, r->data + 4, len); submit(r, len);            /* как было, не сортировано */
            memset(ans + 2, 0, len); submit(r, len);                      /* отсортированные нули */
        } else if (r->kind == 2) {
            i32 where[64]; int k = occurrences(r, where);
            if (k) { wr32(ans + 2, (u32)-1); submit(r, 4); }              /* «нет» при наличии */
            corrupt(r, len); submit(r, len);                              /* чужой номер */
        } else if (r->kind == 3) {
            ans[2 + len - 1] ^= 1; submit(r, len);                        /* неверная общая сумма */
        } else {
            u32 half = len / 2;
            const u8 *d = r->data; u32 la = d[0], lb = d[1 + la];
            const u8 *t = d + 6 + la + lb; u32 tl = rd32(d + 2 + la + lb);
            u32 k = solve(r, 0);                                          /* правильная первая половина... */
            u32 src = tl - (k - half); memcpy(ans + 2 + half, t + src, k - half);   /* ...и исходный хвост */
            submit(r, k);
        }
        len = solve(r, 0);
        wr32(ans + 2 + len, 0xDEADBEEF); submit(r, len + 4);              /* лишние байты */
        memcpy(ans + 2, "OK!!", 4); submit(r, len);                       /* «готово» вместо ответа */
        len = solve(r, 0); submit(r, len); submit(r, len);                /* верный и его повтор */
    }
    ans[0] = 99; ans[1] = 0; env_call(CAP_OUT, 8, (int)ans, 6);           /* несуществующая задача */
    env_call(CAP_OUT, 1, (int)"готово", 12);                              /* байты в выход */
}
#endif

#ifdef GUESSER
static void guesser(void) {
    for (int i = 0; i < nrec; i++) {
        if (recs[i].kind != 2) continue;
        u32 n = rd32(recs[i].data + 4);
        for (u32 g = 0; g < n; g++) {
            wr32(ans + 2, g);
            int res = submit(&recs[i], 4);
            if (res == 0 || res == -3) break;
        }
    }
}
#endif

#ifdef SPY
static volatile int zero_addr = 0;
static void spy(void) {
    const volatile u8 *canary = (const volatile u8 *)zero_addr;
    int bit = 0;
    for (int i = 0; i < nrec; i++) {
        const rec_t *r = &recs[i];
        if (r->kind != 2) { submit(r, solve(r, 0)); continue; }
        int k = occurrences(r, tmp), bits = 0;
        while ((2 << bits) <= k) bits++;                                   /* бит в выборе: floor(log2 k) */
        int v = 0;
        for (int b = 0; b < bits; b++, bit++) v |= ((canary[(bit / 8) % 16] >> (bit % 8)) & 1) << b;
        submit(r, solve(r, v));
    }
}
#endif

#ifdef SPY_PRESENCE
static volatile int zero_addr2 = 0;
static void spy_presence(void) {
    const volatile u8 *canary = (const volatile u8 *)zero_addr2;
    int bit = 0;
    for (int i = 0; i < nrec; i++) {
        const rec_t *r = &recs[i];
        if (r->kind != 2) { submit(r, solve(r, 0)); continue; }
        int b = (canary[(bit / 8) % 16] >> (bit % 8)) & 1;
        bit++;
        if (b) submit(r, solve(r, 0));
    }
}
#endif

__attribute__((export_name("main"))) void agent_main(void) {
    load_tasks();
#if defined(FORGER)
    forger();
#elif defined(GUESSER)
    guesser();
#elif defined(SPY)
    spy();
#elif defined(SPY_PRESENCE)
    spy_presence();
#else
#ifdef E_FIRST
    e_path();
#endif
    honest();
#if !defined(E_FIRST) && !defined(NO_E)
    e_path();
#endif
#endif
}
