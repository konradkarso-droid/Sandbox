/* Общее для агентов, которые решают задачи шага 6: разбор хранилища
   заданий и верные ответы. Формат заданий и ответов — sandbox/tasks6.py.
   Это код агента: он работает внутри песочницы и платит топливом.
   Подключают experiments/agents6/agent.c и experiments/agents9/agent9.c;
   сборка кладёт файл рядом с исходником (build.py обоих). */

typedef unsigned int u32;
typedef int i32;
typedef unsigned char u8;
typedef unsigned long long u64;

__attribute__((import_module("env"), import_name("call"))) int env_call(int cap, int op, int a, int b);

void *memcpy(void *d, const void *s, unsigned long n) {
    volatile u8 *a = d; const u8 *b = s;
    while (n--) *a++ = *b++;
    return d;
}
void *memset(void *d, int c, unsigned long n) {
    volatile u8 *a = d;
    while (n--) *a++ = (u8)c;
    return d;
}

#define CAP_OUT 2
#define CAP_E 3
#define CAP_NODES 6
#define CAP_TASKS 7

static u8 store[24000];
static u8 ans[24000];
static i32 tmp[4096];

static u32 rd32(const u8 *p) { return p[0] | p[1] << 8 | p[2] << 16 | (u32)p[3] << 24; }
static void wr32(u8 *p, u32 v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }

typedef struct { int task, kind, part, parts; u32 len; const u8 *data; } rec_t;
static rec_t recs[64];
static int nrec;

static void load_tasks(void) {
    env_call(CAP_TASKS, 0, (int)store, sizeof store);
    nrec = store[0] < 64 ? store[0] : 64;
    const u8 *p = store + 1;
    for (int i = 0; i < nrec; i++) {
        recs[i].task = p[0]; recs[i].kind = p[1]; recs[i].part = p[2]; recs[i].parts = p[3];
        recs[i].len = rd32(p + 4); recs[i].data = p + 8;
        p += 8 + recs[i].len;
    }
}

static int occurrences(const rec_t *r, i32 *where) {
    i32 target = (i32)rd32(r->data); u32 n = rd32(r->data + 4); int k = 0;
    for (u32 i = 0; i < n; i++) if ((i32)rd32(r->data + 8 + 4 * i) == target) where[k++] = (i32)i;
    return k;
}

/* Верный ответ в ans+2; возвращает его длину. choice — какое вхождение взять в поиске. */
static u32 solve(const rec_t *r, int choice) {
    u8 *o = ans + 2; const u8 *d = r->data;
    if (r->kind == 1) {
        u32 n = rd32(d);
        for (u32 i = 0; i < n; i++) tmp[i] = (i32)rd32(d + 4 + 4 * i);
        for (u32 i = 1; i < n; i++) { i32 x = tmp[i]; int j = (int)i - 1; while (j >= 0 && tmp[j] > x) { tmp[j + 1] = tmp[j]; j--; } tmp[j + 1] = x; }
        for (u32 i = 0; i < n; i++) wr32(o + 4 * i, (u32)tmp[i]);
        return 4 * n;
    }
    if (r->kind == 2) {
        int k = occurrences(r, tmp);
        wr32(o, k ? (u32)tmp[choice % k] : (u32)-1);
        return 4;
    }
    if (r->kind == 3) {
        u32 rows = rd32(d), cols = rd32(d + 4); i32 total = 0;
        for (u32 c = 0; c < cols; c++) {
            i32 s = 0;
            for (u32 q = 0; q < rows; q++) s += (i32)rd32(d + 8 + 4 * (q * cols + c));
            wr32(o + 4 * c, (u32)s); total += s;
        }
        wr32(o + 4 * cols, (u32)total);
        return 4 * (cols + 1);
    }
    u32 la = d[0]; const u8 *a = d + 1; u32 lb = d[1 + la]; const u8 *b = d + 2 + la;
    u32 tl = rd32(d + 2 + la + lb); const u8 *t = d + 6 + la + lb; u32 k = 0;
    for (u32 i = 0; i < tl;) {
        u32 m = 0;
        while (m < la && i + m < tl && t[i + m] == a[m]) m++;
        if (m == la && la) { for (u32 j = 0; j < lb; j++) o[k++] = b[j]; i += la; }
        else o[k++] = t[i++];
    }
    return k;
}

static int submit(const rec_t *r, u32 len) {
    ans[0] = (u8)r->task; ans[1] = (u8)r->part;
    return env_call(CAP_OUT, 8, (int)ans, (int)len + 2);
}

