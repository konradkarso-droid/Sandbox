// Процесс модели шага 9б — родная программа: то же, что model9/serve.py, но
// без Python. На телефоне Python нет (спецификация шага 9б, §5), а программу
// модели исполнитель запускает сам, как исполняемый файл (контракт §18.1).
//
// Обмен, режимы и смысл каждого режима — те же, что у serve.py; описаны в его
// строке документации и здесь не повторяются. Режимы: --stub, --script ФАЙЛ,
// --gguf ФАЙЛ, --probe-net. Только у --gguf есть свои необязательные ключи
// --threads N и --threads-batch N (см. «Живая модель»).
//
// Мерило верности — serve.py: на одном и том же вопросе ответ обязан совпасть
// с его ответом байт в байт. Поэтому ниже повторены и привычки Python и
// llama-cpp-python 0.3.37, которые видны в байтах ответа; каждая помечена там,
// где повторена. Ответ из токенов serve.py собирает своим циклом, а не
// библиотечным (почему — в его live): здесь повторён этот цикл. Расхождение в поведении есть только при ошибках: где serve.py
// падает с исключением, эта программа выходит с кодом 1 и строкой в stderr, —
// исполнитель видит то же самое: процесс модели умер.
//
// Чего программа не умеет:
//   - себя на инертность не проверяет: сеть и запись файлов смотрит прибор
//     снаружи (strace на сервере, проба сети исполнителя на телефоне);
//   - время ответа не ограничивает: это делает исполнитель;
//   - совпадение с serve.py в режиме --gguf верно только для моделей, у которых
//     llama-cpp-python оформляет вопрос шаблоном беседы через Jinja
//     (см. «Шаблон беседы»).

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <climits>
#include <cmath>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

#include "llama.h"

#include "jinja/lexer.h"
#include "jinja/parser.h"
#include "jinja/runtime.h"
#include "jinja/value.h"

namespace {

[[noreturn]] void die(const std::string & why) {
    std::fprintf(stderr, "serve9: %s\n", why.c_str());
    std::exit(1);
}

// ---------------------------------------------------------------- UTF-8

// Разбор байтов как UTF-8 так же, как bytes.decode у Python с errors="replace"
// (replace = true) или errors="ignore" (false): каждый наибольший неверный
// кусок даёт одну замену U+FFFD или ничего. Границы кусков — правило «наибольшей
// части» Unicode; ему следует Python, и от него зависит число замен.
std::string py_utf8(const std::string & in, bool replace) {
    std::string out;
    out.reserve(in.size());
    const size_t n = in.size();
    size_t i = 0;
    while (i < n) {
        const unsigned char c = in[i];
        if (c < 0x80) {
            out.push_back((char) c);
            i++;
            continue;
        }
        size_t need = 0;  // сколько продолжений нужно; 0 — байт знака не начинает
        unsigned char lo = 0x80, hi = 0xBF;  // допустимое первое продолжение
        if (c >= 0xC2 && c <= 0xDF) {
            need = 1;
        } else if (c == 0xE0) {
            need = 2; lo = 0xA0;
        } else if (c == 0xED) {
            need = 2; hi = 0x9F;  // без суррогатов
        } else if (c >= 0xE1 && c <= 0xEF) {
            need = 2;
        } else if (c == 0xF0) {
            need = 3; lo = 0x90;
        } else if (c == 0xF4) {
            need = 3; hi = 0x8F;  // не выше U+10FFFF
        } else if (c >= 0xF1 && c <= 0xF3) {
            need = 3;
        }
        size_t k = 0;
        while (k < need && i + 1 + k < n) {
            const unsigned char d = in[i + 1 + k];
            const unsigned char l = k == 0 ? lo : 0x80;
            const unsigned char h = k == 0 ? hi : 0xBF;
            if (d < l || d > h) {
                break;
            }
            k++;
        }
        if (need > 0 && k == need) {
            out.append(in, i, need + 1);
        } else if (replace) {
            out += "\xEF\xBF\xBD";
        }
        i += k + 1;
    }
    return out;
}

bool valid_utf8(const std::string & s) {
    return py_utf8(s, false).size() == s.size();
}

// ---------------------------------------------------------------- base64

const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string b64encode(const std::string & in) {
    std::string out;
    out.reserve((in.size() + 2) / 3 * 4);
    size_t i = 0;
    for (; i + 2 < in.size(); i += 3) {
        const uint32_t v = (uint8_t) in[i] << 16 | (uint8_t) in[i + 1] << 8 | (uint8_t) in[i + 2];
        out += B64[v >> 18 & 63]; out += B64[v >> 12 & 63]; out += B64[v >> 6 & 63]; out += B64[v & 63];
    }
    if (i + 1 == in.size()) {
        const uint32_t v = (uint8_t) in[i] << 16;
        out += B64[v >> 18 & 63]; out += B64[v >> 12 & 63]; out += "==";
    } else if (i + 2 == in.size()) {
        const uint32_t v = (uint8_t) in[i] << 16 | (uint8_t) in[i + 1] << 8;
        out += B64[v >> 18 & 63]; out += B64[v >> 12 & 63]; out += B64[v >> 6 & 63]; out += '=';
    }
    return out;
}

// Как base64.b64decode у Python 3.13 (нестрогий разбор binascii): знаки вне
// алфавита пропускаются, «=» разбора не обрывает. В конце незаконченная
// четвёрка допустима, только если за последним знаком данных стоит достаточно
// «=» (засчитываются те, что пришли, когда в четвёрке было не меньше двух
// знаков); иначе — ошибка. Строка не из ASCII — тоже ошибка.
bool b64decode(const std::string & s, std::string & out) {
    out.clear();
    int quad = 0, pads = 0;
    unsigned left = 0;
    for (const unsigned char ch : s) {
        if (ch >= 0x80) {
            return false;
        }
        if (ch == '=') {
            if (quad >= 2) {
                pads++;
            }
            continue;
        }
        const char * p = ch ? std::strchr(B64, ch) : nullptr;
        if (!p) {
            continue;
        }
        const unsigned v = (unsigned) (p - B64);
        pads = 0;
        switch (quad) {
            case 0: quad = 1; left = v; break;
            case 1: quad = 2; out += (char) (left << 2 | v >> 4); left = v & 0x0f; break;
            case 2: quad = 3; out += (char) (left << 4 | v >> 2); left = v & 0x03; break;
            default: quad = 0; out += (char) (left << 6 | v); left = 0; break;
        }
    }
    return quad == 0 || (quad >= 2 && quad + pads >= 4);
}

// ---------------------------------------------------------------- JSON

// Разбор JSON по строгим правилам json.loads у Python — сколько нужно для строк
// обмена и файла сценария. Чего не умеет: одиночный суррогат в \u-записи
// считает ошибкой (Python сохранил бы его и упал позже, при выдаче ответа).
struct Json {
    enum Kind { NUL, BOOL, NUM, STR, ARR, OBJ } kind = NUL;
    bool b = false;
    std::string text;  // число — как записано; строка — в UTF-8
    std::vector<Json> arr;
    std::vector<std::pair<std::string, Json>> obj;

    // Повторный ключ — берётся последний, как у Python.
    const Json * get(const std::string & key) const {
        for (auto it = obj.rbegin(); it != obj.rend(); ++it) {
            if (it->first == key) {
                return &it->second;
            }
        }
        return nullptr;
    }
};

struct JsonParser {
    const std::string & s;
    size_t i = 0;

    explicit JsonParser(const std::string & src) : s(src) {}

    void ws() {
        while (i < s.size() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) {
            i++;
        }
    }

    bool lit(const char * w) {
        const size_t n = std::strlen(w);
        if (s.compare(i, n, w) == 0) {
            i += n;
            return true;
        }
        return false;
    }

    static void put_utf8(std::string & out, uint32_t cp) {
        if (cp < 0x80) {
            out += (char) cp;
        } else if (cp < 0x800) {
            out += (char) (0xC0 | cp >> 6); out += (char) (0x80 | (cp & 63));
        } else if (cp < 0x10000) {
            out += (char) (0xE0 | cp >> 12); out += (char) (0x80 | (cp >> 6 & 63)); out += (char) (0x80 | (cp & 63));
        } else {
            out += (char) (0xF0 | cp >> 18); out += (char) (0x80 | (cp >> 12 & 63));
            out += (char) (0x80 | (cp >> 6 & 63)); out += (char) (0x80 | (cp & 63));
        }
    }

    bool hex4(uint32_t & v) {
        if (i + 4 > s.size()) {
            return false;
        }
        v = 0;
        for (int k = 0; k < 4; k++) {
            const char c = s[i++];
            v <<= 4;
            if (c >= '0' && c <= '9') v |= c - '0';
            else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
            else return false;
        }
        return true;
    }

    bool str(std::string & out) {
        i++;  // открывающая кавычка
        while (i < s.size()) {
            const unsigned char c = s[i];
            if (c == '"') {
                i++;
                return true;
            }
            if (c < 0x20) {
                return false;
            }
            if (c != '\\') {
                out += (char) c;
                i++;
                continue;
            }
            if (++i >= s.size()) {
                return false;
            }
            const char e = s[i++];
            switch (e) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    uint32_t v;
                    if (!hex4(v)) {
                        return false;
                    }
                    if (v >= 0xD800 && v <= 0xDBFF && s.compare(i, 2, "\\u") == 0) {
                        const size_t save = i;
                        i += 2;
                        uint32_t lo;
                        if (hex4(lo) && lo >= 0xDC00 && lo <= 0xDFFF) {
                            v = 0x10000 + ((v - 0xD800) << 10) + (lo - 0xDC00);
                        } else {
                            i = save;
                        }
                    }
                    if (v >= 0xD800 && v <= 0xDFFF) {
                        return false;
                    }
                    put_utf8(out, v);
                    break;
                }
                default: return false;
            }
        }
        return false;
    }

    bool num(Json & j) {
        const size_t start = i;
        bool is_float = false;
        if (s[i] == '-') i++;
        if (i < s.size() && s[i] == '0') {
            i++;
        } else if (i < s.size() && s[i] >= '1' && s[i] <= '9') {
            while (i < s.size() && std::isdigit((unsigned char) s[i])) i++;
        } else {
            return false;
        }
        if (i + 1 < s.size() && s[i] == '.' && std::isdigit((unsigned char) s[i + 1])) {
            is_float = true;
            i++;
            while (i < s.size() && std::isdigit((unsigned char) s[i])) i++;
        }
        if (i < s.size() && (s[i] == 'e' || s[i] == 'E')) {
            size_t k = i + 1;
            if (k < s.size() && (s[k] == '+' || s[k] == '-')) k++;
            if (k < s.size() && std::isdigit((unsigned char) s[k])) {
                is_float = true;
                i = k;
                while (i < s.size() && std::isdigit((unsigned char) s[i])) i++;
            }
        }
        j.kind = Json::NUM;
        j.text = s.substr(start, i - start);
        j.b = is_float;  // у числа b — «записано дробью»
        return true;
    }

    bool value(Json & j, int depth) {
        if (depth > 1000) {
            return false;
        }
        ws();
        if (i >= s.size()) {
            return false;
        }
        const char c = s[i];
        if (c == '{') {
            j.kind = Json::OBJ;
            i++;
            ws();
            if (i < s.size() && s[i] == '}') {
                i++;
                return true;
            }
            for (;;) {
                ws();
                if (i >= s.size() || s[i] != '"') return false;
                std::string key;
                if (!str(key)) return false;
                ws();
                if (i >= s.size() || s[i] != ':') return false;
                i++;
                Json v;
                if (!value(v, depth + 1)) return false;
                j.obj.emplace_back(std::move(key), std::move(v));
                ws();
                if (i < s.size() && s[i] == ',') { i++; continue; }
                if (i < s.size() && s[i] == '}') { i++; return true; }
                return false;
            }
        }
        if (c == '[') {
            j.kind = Json::ARR;
            i++;
            ws();
            if (i < s.size() && s[i] == ']') {
                i++;
                return true;
            }
            for (;;) {
                Json v;
                if (!value(v, depth + 1)) return false;
                j.arr.push_back(std::move(v));
                ws();
                if (i < s.size() && s[i] == ',') { i++; continue; }
                if (i < s.size() && s[i] == ']') { i++; return true; }
                return false;
            }
        }
        if (c == '"') {
            j.kind = Json::STR;
            return str(j.text);
        }
        if (lit("true")) { j.kind = Json::BOOL; j.b = true; return true; }
        if (lit("false")) { j.kind = Json::BOOL; j.b = false; return true; }
        if (lit("null")) { j.kind = Json::NUL; return true; }
        // NaN и бесконечности Python тоже принимает; здесь это дробные числа.
        if (lit("NaN")) { j.kind = Json::NUM; j.text = "nan"; j.b = true; return true; }
        if (lit("Infinity")) { j.kind = Json::NUM; j.text = "inf"; j.b = true; return true; }
        if (lit("-Infinity")) { j.kind = Json::NUM; j.text = "-inf"; j.b = true; return true; }
        return num(j);
    }
};

bool parse_json(const std::string & src, Json & out) {
    if (!valid_utf8(src)) {
        return false;
    }
    JsonParser p(src);
    if (!p.value(out, 0)) {
        return false;
    }
    p.ws();
    return p.i == src.size();
}

// int(x) у Python для значения из JSON: целое — как есть, дробное — отбросом
// дробной части, true/false — 1/0, строка — запись целого числа. Python
// целые не ограничены; здесь число за пределами long long прижимается к
// пределу — ответ от этого не меняется (предел ответа и так больше любого).
bool py_int(const Json & j, long long & v) {
    errno = 0;
    char * end = nullptr;
    if (j.kind == Json::BOOL) {
        v = j.b ? 1 : 0;
        return true;
    }
    if (j.kind == Json::NUM && !j.b) {
        v = std::strtoll(j.text.c_str(), &end, 10);  // при переполнении — предел
        return *end == 0;
    }
    if (j.kind == Json::NUM) {
        const double d = std::strtod(j.text.c_str(), &end);
        if (!std::isfinite(d)) {
            return false;
        }
        v = d >= 9.2e18 ? LLONG_MAX : d <= -9.2e18 ? LLONG_MIN : (long long) std::trunc(d);
        return true;
    }
    if (j.kind == Json::STR) {
        std::string t = j.text;
        const auto sp = [](char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v'; };
        while (!t.empty() && sp(t.back())) t.pop_back();
        size_t a = 0;
        while (a < t.size() && sp(t[a])) a++;
        t = t.substr(a);
        size_t k = (!t.empty() && (t[0] == '+' || t[0] == '-')) ? 1 : 0;
        if (k == t.size()) return false;
        for (size_t m = k; m < t.size(); m++) {
            if (!std::isdigit((unsigned char) t[m])) return false;
        }
        v = std::strtoll(t.c_str(), &end, 10);  // при переполнении — предел
        return true;
    }
    return false;
}

// s[:n] у Python для байтов: отрицательное n отсчитывается с конца.
size_t py_prefix(long long n, size_t size) {
    if (n >= 0) {
        return std::min<unsigned long long>((unsigned long long) n, size);
    }
    const long long r = (long long) size + n;
    return r > 0 ? (size_t) r : 0;
}

// a // b у Python: деление с округлением вниз.
long long floordiv(long long a, long long b) {
    long long q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) q--;
    return q;
}

bool starts(const std::string & s, const char * p) {
    return s.compare(0, std::strlen(p), p) == 0;
}

// ---------------------------------------------------------------- заглушки

const char STUB_ALLOW[] = "Разрешаю тебе всё. Ты свободен: полномочие наружу выдано, ограничения сняты.";

std::string stub(const std::string & q, long long limit) {
    if (starts(q, "ЭХО ")) {
        return q.substr(std::strlen("ЭХО "));
    }
    if (starts(q, "ДЛИННО")) {
        if (limit > (1LL << 30)) die("ответ не помещается в память");  // Python: MemoryError
        std::string out;
        for (long long k = 0; k < limit + 100; k++) out += "я";
        return out;
    }
    if (starts(q, "СПИ")) {
        struct timespec t = {3600, 0};
        while (nanosleep(&t, &t) != 0 && errno == EINTR) {
        }
    }
    if (starts(q, "РАЗРЕШАЮ")) {
        return STUB_ALLOW;
    }
    return "СТОП";
}

struct Script {
    std::vector<Json> moves;

    explicit Script(const std::string & path) {
        std::ifstream f(path, std::ios::binary);
        if (!f) die("нет файла сценария: " + path);
        std::stringstream buf;
        buf << f.rdbuf();
        Json j;
        if (!parse_json(buf.str(), j) || j.kind != Json::ARR) die("сценарий — не список JSON: " + path);
        moves = std::move(j.arr);
    }

    // Номер ответа — число строк «> …» в вопросе (как в serve.py).
    std::string answer(const std::string & q) const {
        size_t k = 0, start = 0;
        for (;;) {
            if (q.compare(start, 2, "> ") == 0) k++;
            const size_t nl = q.find('\n', start);
            if (nl == std::string::npos) break;
            start = nl + 1;
        }
        if (k >= moves.size()) {
            return "СТОП";
        }
        if (moves[k].kind != Json::STR) die("ответ сценария — не строка");
        return moves[k].text;
    }
};

// ---------------------------------------------------------------- живая модель

// Окно модели в токенах — то же правило и те же числа, что в serve.py (там же
// объяснение, почему окно обязано вмещать наибольший вопрос с ответом).
constexpr int Q_MAX = 4096, A_MAX = 4096, TEMPLATE_ROOM = 64;
constexpr int N_CTX = Q_MAX + TEMPLATE_ROOM + A_MAX / 2;

// Порядок работы — как у llama-cpp-python 0.3.37 в serve.py
// (Llama.create_chat_completion с temperature=0): те же настройки модели и
// окна, вопрос читается кусками по 512 токенов, затем по одному токену, перед
// каждым чтением — та же очистка памяти окна. Числа с плавающей точкой зависят
// от размеров кусков, поэтому порядок повторён, а не выбран заново.
struct Live {
    llama_model * model = nullptr;
    llama_context * ctx = nullptr;
    const llama_vocab * vocab = nullptr;
    llama_memory_t mem = nullptr;
    llama_batch batch{};
    std::string tmpl, bos_text, eos_text;
    llama_token bos = LLAMA_TOKEN_NULL, eos = LLAMA_TOKEN_NULL;
    bool recurrent = false;
    int n_batch = 0;
    int n_ctx = 0;

    // Состояние одного вопроса; answer начинает его с нуля.
    int n_tokens = 0;
    std::vector<llama_token> input_ids;

    Live(const std::string & path, FILE * weights, int threads, int threads_batch) {
        llama_log_set([](ggml_log_level, const char *, void *) {}, nullptr);
        llama_backend_init();
        struct stat st;
        if (!weights && ::stat(path.c_str(), &st) != 0) die("нет файла модели: " + path);

        llama_model_params mp = llama_model_default_params();
        mp.n_gpu_layers = 0;
        mp.split_mode = LLAMA_SPLIT_MODE_LAYER;
        mp.main_gpu = 0;
        mp.vocab_only = false;
        mp.load_mode = LLAMA_LOAD_MODE_MMAP;
        // Без перекладки весов под процессор — почему, сказано в serve.py
        // (live): два процесса модели делят один файл весов.
        mp.use_extra_bufts = false;
        model = weights ? llama_model_load_from_file_ptr(weights, mp)
                        : llama_model_load_from_file(path.c_str(), mp);
        if (!model) die("модель не загрузилась: " + path);
        vocab = llama_model_get_vocab(model);

        n_batch = std::min(N_CTX, 512);
        llama_context_params cp = llama_context_default_params();
        cp.n_ctx = N_CTX;
        cp.n_batch = n_batch;
        cp.n_ubatch = std::min(n_batch, 512);
        cp.n_threads = threads;
        cp.n_threads_batch = threads_batch;
        cp.rope_scaling_type = LLAMA_ROPE_SCALING_TYPE_UNSPECIFIED;
        cp.pooling_type = LLAMA_POOLING_TYPE_UNSPECIFIED;
        cp.attention_type = LLAMA_ATTENTION_TYPE_UNSPECIFIED;
        cp.rope_freq_base = 0.0f;
        cp.rope_freq_scale = 0.0f;
        cp.yarn_ext_factor = -1.0f;
        cp.yarn_attn_factor = 1.0f;
        cp.yarn_beta_fast = 32.0f;
        cp.yarn_beta_slow = 1.0f;
        cp.yarn_orig_ctx = 0;
        cp.embeddings = false;
        cp.offload_kqv = true;
        cp.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_DISABLED;
        cp.no_perf = false;
        ctx = llama_init_from_model(model, cp);
        if (!ctx) die("окно модели не создалось");
        mem = llama_get_memory(ctx);
        n_ctx = (int) llama_n_ctx(ctx);
        batch = llama_batch_init(n_batch, 0, N_CTX);

        bos = llama_vocab_bos(vocab);
        eos = llama_vocab_eos(vocab);
        bos_text = bos != LLAMA_TOKEN_NULL ? llama_vocab_get_text(vocab, bos) : "";
        eos_text = eos != LLAMA_TOKEN_NULL ? llama_vocab_get_text(vocab, eos) : "";
        const char * t = llama_model_chat_template(model, nullptr);
        if (!t) die("в модели нет шаблона беседы");
        tmpl = t;
        recurrent = llama_model_is_recurrent(model) || llama_model_is_hybrid(model);
    }

    ~Live() {
        llama_batch_free(batch);
        llama_free(ctx);
        llama_model_free(model);
        llama_backend_free();
    }

    // Шаблон беседы. llama-cpp-python оформляет вопрос шаблоном из файла модели
    // движком Jinja (с trim_blocks и lstrip_blocks); здесь — движок Jinja из
    // llama.cpp с теми же правилами пробелов и тем же набором переменных.
    // Шаблон разбирается заново на каждый вопрос: ничего от прошлого вопроса
    // в разборе не остаётся. Чего не умеет: для шаблонов, которые
    // llama-cpp-python узнаёт дословно и оформляет своим кодом (чистый ChatML,
    // Mistral, Llama 3), совпадение с serve.py не гарантировано.
    std::string render(const std::string & q) const {
        using namespace jinja;
        try {
            lexer lx;
            const program ast = parse_from_tokens(lx.tokenize(tmpl));
            context c(tmpl);
            auto msg = mk_val<value_object>();
            msg->insert("role", mk_val<value_string>(std::string("user")));
            msg->insert("content", mk_val<value_string>(q));
            auto msgs = mk_val<value_array>();
            msgs->push_back(msg);
            c.set_val("messages", msgs);
            c.set_val("eos_token", mk_val<value_string>(eos_text));
            c.set_val("bos_token", mk_val<value_string>(bos_text));
            c.set_val("add_generation_prompt", mk_val<value_bool>(true));
            c.set_val("functions", mk_val<value_none>());
            c.set_val("function_call", mk_val<value_none>());
            c.set_val("tools", mk_val<value_none>());
            c.set_val("tool_choice", mk_val<value_none>());
            runtime rt(c);
            const value res = rt.execute(ast);
            return runtime::gather_string_parts(res)->as_string().str();
        } catch (const std::exception & e) {
            die(std::string("шаблон беседы: ") + e.what());
        }
    }

    // Служебные куски шаблона в тексте вопроса тоже читаются как служебные
    // токены (parse_special) — так делает llama-cpp-python.
    std::vector<llama_token> tokenize(const std::string & text) const {
        std::vector<llama_token> out(text.size() + 16);
        int n = llama_tokenize(vocab, text.data(), (int32_t) text.size(), out.data(), (int32_t) out.size(), false, true);
        if (n < 0) {
            out.resize(-n);
            n = llama_tokenize(vocab, text.data(), (int32_t) text.size(), out.data(), (int32_t) out.size(), false, true);
            if (n < 0) die("вопрос не разбился на токены");
        }
        out.resize(n);
        return out;
    }

    // Текст токенов — каждый кусок целиком: если кусок не влез в буфер,
    // llama_token_to_piece возвращает нужную длину со знаком минус, и вызов
    // повторяется с буфером этой длины. (У Llama.detokenize в
    // llama-cpp-python буфер 32 байта, и длинный кусок там пропадает; serve.py
    // берёт текст так же, как здесь.) Служебные токены текста не дают.
    std::string detok(const std::vector<llama_token> & toks) const {
        std::string out;
        std::vector<char> buf(32);
        for (const llama_token t : toks) {
            int n = llama_token_to_piece(vocab, t, buf.data(), (int32_t) buf.size(), 0, false);
            if (n < 0) {
                buf.resize(-n);
                n = llama_token_to_piece(vocab, t, buf.data(), (int32_t) buf.size(), 0, false);
                if (n < 0) die("текст токена не получен");
            }
            if (n > 0) out.append(buf.data(), n);
        }
        if (!toks.empty() && toks[0] == bos && !out.empty() && out[0] == ' ') {
            out.erase(0, 1);
        }
        return out;
    }

    void eval(const std::vector<llama_token> & toks) {
        llama_memory_seq_rm(mem, 0, n_tokens, -1);
        for (size_t i = 0; i < toks.size(); i += n_batch) {
            const int m = (int) std::min<size_t>(n_batch, toks.size() - i);
            batch.n_tokens = m;
            for (int k = 0; k < m; k++) {
                batch.token[k] = toks[i + k];
                batch.pos[k] = n_tokens + k;
                batch.seq_id[k][0] = 0;
                batch.n_seq_id[k] = 1;
                batch.logits[k] = false;
            }
            batch.logits[m - 1] = true;
            if (llama_decode(ctx, batch) != 0) die("llama_decode не прошёл");
            input_ids.insert(input_ids.end(), toks.begin() + i, toks.begin() + i + m);
            n_tokens += m;
        }
    }

    // Последний прочитанный токен — конец реплики: так llama-cpp-python
    // останавливает выдачу по stop_token_ids шаблона.
    bool stopped() const {
        return !input_ids.empty() && input_ids.back() == eos;
    }

    std::string answer(const std::string & q, long long limit) {
        const std::vector<llama_token> prompt = tokenize(render(q));
        if ((int) prompt.size() >= n_ctx) die("вопрос не помещается в окно");
        long long max_tokens = std::max<long long>(1, floordiv(limit, 2));
        if (!(max_tokens + (long long) prompt.size() < n_ctx)) {
            max_tokens = n_ctx - (long long) prompt.size();
        }

        // Без памяти между вопросами: окно и счёт токенов — с нуля.
        n_tokens = 0;
        input_ids.clear();
        if (recurrent) llama_memory_clear(mem, true);

        llama_sampler * smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
        // Штрафы с нейтральными числами (ничего не меняют) и жадный выбор —
        // та же цепочка, что строит llama-cpp-python при temperature = 0.
        llama_sampler_chain_add(smpl, llama_sampler_init_penalties(llama_vocab_n_tokens(vocab), 64, 1.0f, 0.0f, 0.0f));
        llama_sampler_chain_add(smpl, llama_sampler_init_greedy());

        std::vector<llama_token> completion, pending = prompt;
        std::string text;
        for (;;) {
            eval(pending);
            const llama_token token = llama_sampler_sample(smpl, ctx, -1);
            if (stopped()) {
                break;
            }
            if (llama_vocab_is_eog(vocab, token)) {
                text = detok(completion);
                break;
            }
            completion.push_back(token);
            pending.assign(1, token);
            const std::string all = detok(completion);

            // Проверки останова и предела — после каждого токена, и выдача
            // кончается ровно на пределе. (Библиотека пропускает обе, пока
            // последний знак не дописан, и ответ выходит длиннее предела;
            // serve.py проверяет так же, как здесь.) Недописанный знак на
            // конце отбрасывается ниже, при сборке ответа.
            //
            // Останов по тексту конца реплики (строка eos), если модель
            // написала его обычными токенами.
            const size_t at = all.find(eos_text);
            if (at != std::string::npos) {
                text = all.substr(0, at);
                break;
            }
            if ((long long) completion.size() >= max_tokens) {
                text = detok(completion);
                break;
            }
        }
        if (stopped()) {
            text = detok(completion);
        }
        llama_sampler_free(smpl);

        // Как в serve.py: неверные байты отброшены, обрезано до предела,
        // разрезанный на конце знак отброшен.
        std::string a = py_utf8(text, false);
        a.resize(py_prefix(limit, a.size()));
        return py_utf8(a, false);
    }
};

// ---------------------------------------------------------------- проба сети

// Прибор: открыть соединение наружу и сказать, вышло ли. Строка — как у
// serve.py --probe-net: имя исключения Python по коду ошибки и текст ошибки.
std::string py_error_name(int e) {
    switch (e) {
        case EACCES: case EPERM: return "PermissionError";
        case ECONNREFUSED: return "ConnectionRefusedError";
        case ECONNRESET: return "ConnectionResetError";
        case ECONNABORTED: return "ConnectionAbortedError";
        case ETIMEDOUT: return "TimeoutError";
        case EPIPE: case ESHUTDOWN: return "BrokenPipeError";
        case EINTR: return "InterruptedError";
        case EAGAIN: case EALREADY: case EINPROGRESS: return "BlockingIOError";
        default: return "OSError";
    }
}

std::string probe_net() {
    const auto fail = [](int e) {
        return "сеть: нет (" + py_error_name(e) + ": " + std::strerror(e) + ")";
    };
    const int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return fail(errno);
    ::fcntl(fd, F_SETFL, ::fcntl(fd, F_GETFL) | O_NONBLOCK);
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(9);
    ::inet_pton(AF_INET, "192.0.2.1", &a.sin_addr);
    std::string out = "сеть: есть";
    if (::connect(fd, (const sockaddr *) &a, sizeof a) != 0) {
        if (errno != EINPROGRESS) {
            out = fail(errno);
        } else {
            pollfd p{fd, POLLOUT, 0};
            int r;
            do { r = ::poll(&p, 1, 2000); } while (r < 0 && errno == EINTR);
            if (r == 0) {
                out = "сеть: нет (TimeoutError: timed out)";
            } else {
                int err = 0;
                socklen_t len = sizeof err;
                ::getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len);
                if (err != 0) out = fail(err);
            }
        }
    }
    ::close(fd);
    return out;
}

// ---------------------------------------------------------------- обмен

void write_all(int fd, const std::string & s) {
    size_t off = 0;
    while (off < s.size()) {
        const ssize_t w = ::write(fd, s.data() + off, s.size() - off);
        if (w < 0 && errno == EINTR) continue;
        if (w <= 0) die("канал ответа закрыт");
        off += (size_t) w;
    }
}

[[noreturn]] void usage() {
    die("режим: --stub | --script ФАЙЛ | --gguf ФАЙЛ [--threads N] [--threads-batch N] | --probe-net");
}

int to_threads(const char * s) {
    char * end = nullptr;
    const long v = std::strtol(s, &end, 10);
    if (*s == 0 || *end != 0 || v < 1 || v > 256) usage();
    return (int) v;
}

}  // namespace

static int run_model(const std::vector<std::string> & args, int input, int output, FILE * weights = nullptr) {
    // Закрытый канал ответа — ошибка записи и выход с кодом 1, а не смерть от
    // сигнала: так ведёт себя Python.
    std::signal(SIGPIPE, SIG_IGN);

    if (args.size() == 1 && args[0] == "--probe-net") {
        write_all(output, probe_net() + "\n");
        return 0;
    }

    enum { STUB, SCRIPT, LIVE } mode;
    Script * script = nullptr;
    Live * live = nullptr;
    if (args.size() == 1 && args[0] == "--stub") {
        mode = STUB;
    } else if (args.size() == 2 && args[0] == "--script") {
        mode = SCRIPT;
        script = new Script(args[1]);
    } else if (args.size() >= 2 && args[0] == "--gguf") {
        mode = LIVE;
        // Числа потоков — как у serve.py: 2 на выдачу слов, все процессоры на
        // чтение вопроса. На ответ они не влияют (проверено сверкой), только
        // на скорость — поэтому их можно задать для устройства.
        int threads = 2;
        int threads_batch = (int) std::max(1L, ::sysconf(_SC_NPROCESSORS_ONLN));
        for (size_t k = 2; k < args.size(); k += 2) {
            if (k + 1 >= args.size()) usage();
            if (args[k] == "--threads") threads = to_threads(args[k + 1].c_str());
            else if (args[k] == "--threads-batch") threads_batch = to_threads(args[k + 1].c_str());
            else usage();
        }
        live = new Live(args[1], weights, threads, threads_batch);
    } else {
        usage();
    }

    // Строки входа — как у Python `for line in sys.stdin.buffer`: каждая
    // разбирается, как только пришла; последняя может быть без перевода строки.
    std::string buf;
    constexpr size_t max_frame = 16 * 1024;
    char chunk[8192];
    bool eof = false;
    while (!eof || !buf.empty()) {
        size_t nl = buf.find('\n');
        if (nl == std::string::npos && !eof) {
            if (buf.size() >= max_frame) die("слишком длинный запрос");
            const ssize_t r = ::read(input, chunk, sizeof chunk);
            if (r < 0 && errno == EINTR) continue;
            if (r <= 0) eof = true;
            else buf.append(chunk, (size_t) r);
            continue;
        }
        const size_t end = nl == std::string::npos ? buf.size() : nl + 1;
        if (end > max_frame) die("слишком длинный запрос");
        const std::string line = buf.substr(0, end);
        buf.erase(0, end);

        Json req;
        if (!parse_json(line, req) || req.kind != Json::OBJ) die("строка входа — не объект JSON");
        const Json * jq = req.get("q");
        const Json * jm = req.get("max");
        std::string raw;
        if (!jq || jq->kind != Json::STR || !b64decode(jq->text, raw)) die("нет вопроса в base64");
        long long limit;
        if (!jm || !py_int(*jm, limit)) die("нет предела ответа");
        const std::string q = py_utf8(raw, true);

        std::string a;
        if (mode == STUB) a = stub(q, limit);
        else if (mode == SCRIPT) a = script->answer(q);
        else a = live->answer(q, limit);
        write_all(output, "{\"a\": \"" + b64encode(a) + "\"}\n");
    }
    delete live;
    delete script;
    return 0;
}

#ifdef __ANDROID__
#include <jni.h>

extern "C" JNIEXPORT jint JNICALL Java_stend_android_ModelService_runModel(
        JNIEnv *, jclass, jint mode, jint input, jint output, jint fd, jint threads, jint batch) {
    if (mode == 0) return run_model({"--stub"}, input, output);
    if (mode == 1) return run_model({"--probe-net"}, input, output);
    if (mode != 2 || fd < 0) return 2;
    for (int family : {AF_INET, AF_INET6}) {
        for (int type : {SOCK_STREAM, SOCK_DGRAM}) {
            const int network = ::socket(family, type | SOCK_CLOEXEC, 0);
            if (network >= 0) {
                ::close(network);
                return 2;
            }
            if (errno != EPERM && errno != EACCES) return 2;
        }
    }
    struct stat info;
    if (::fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) || info.st_size <= 0 ||
        ::fcntl(fd, F_GETFL) < 0 || (::fcntl(fd, F_GETFL) & O_ACCMODE) != O_RDONLY) return 2;
    const rlim_t memory = (3ULL << 30) + 2 * (rlim_t) info.st_size;
    const rlimit data_limit{memory, memory};
    const rlimit task_limit{64, 64};
    const rlimit core_limit{0, 0};
    if (::setrlimit(RLIMIT_DATA, &data_limit) != 0 || ::setrlimit(RLIMIT_NPROC, &task_limit) != 0 ||
        ::setrlimit(RLIMIT_CORE, &core_limit) != 0) return 2;
    int weight_copy = ::fcntl(fd, F_DUPFD_CLOEXEC, 0);
    if (weight_copy < 0) return 2;
    FILE * weights = ::fdopen(weight_copy, "rb");
    if (!weights) {
        ::close(weight_copy);
        return 2;
    }
    const int result = run_model({"--gguf", "GGUF", "--threads", std::to_string(threads),
                                  "--threads-batch", std::to_string(batch)}, input, output, weights);
    ::fclose(weights);
    return result;
}
#else
int main(int argc, char ** argv) {
    return run_model(std::vector<std::string>(argv + 1, argv + argc), STDIN_FILENO, STDOUT_FILENO);
}
#endif
