// Прибор сверки: токены вопроса, оформленного шаблоном беседы, — так, как их
// получает родная программа модели (model9/native/serve9.cpp). Сверка
// (experiments/native9.py, prompt) сравнивает их с токенами llama-cpp-python:
// так расхождение шаблона видно прямо, а не по ответу модели.
//
//   render9 ФАЙЛ_МОДЕЛИ < строки base64 вопросов
//   → на каждый вопрос строка номеров токенов через пробел
//   render9 ФАЙЛ_МОДЕЛИ --detok < строки номеров токенов через пробел
//   → на каждую строку текст этих токенов в base64 (тем же способом, что
//     ответ модели: сверка с Llama.detokenize)
//
// Программа модели включается сюда целиком, чтобы прибор пользовался ровно
// её кодом оформления, а не копией.

#define main serve9_main
#include "../../model9/native/serve9.cpp"
#undef main

#include <iostream>

int main(int argc, char ** argv) {
    if (argc != 2 && !(argc == 3 && std::string(argv[2]) == "--detok")) die("render9 ФАЙЛ_МОДЕЛИ [--detok]");
    Live live(argv[1], 2, 2);
    std::string line;
    if (argc == 3) {
        while (std::getline(std::cin, line)) {
            std::vector<llama_token> toks;
            std::istringstream in(line);
            for (long t; in >> t;) toks.push_back((llama_token) t);
            std::cout << b64encode(live.detok(toks)) << "\n" << std::flush;
        }
        return 0;
    }
    while (std::getline(std::cin, line)) {
        std::string raw;
        if (!b64decode(line, raw)) die("строка — не base64");
        const auto toks = live.tokenize(live.render(py_utf8(raw, true)));
        std::string out;
        for (size_t i = 0; i < toks.size(); i++) out += (i ? " " : "") + std::to_string(toks[i]);
        std::cout << out << "\n" << std::flush;
    }
    return 0;
}
