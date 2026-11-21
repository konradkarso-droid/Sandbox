# Родная программа процесса модели (serve9) поверх llama.cpp.
#
#   cmake -S model9/native -B ПАПКА -DLLAMA_SRC=ИСХОДНИКИ_LLAMA_CPP
#
# Исходники llama.cpp — те, что лежат в пакете llama-cpp-python 0.3.37
# (vendor/llama.cpp): с ним serve.py сверяется байт в байт, а числа модели
# зависят от версии библиотеки. Как их достать и собрать для сервера и для
# телефона — build.py рядом.
#
# Библиотека собирается статически: на телефоне программа — один файл. Ключи
# сборки для сервера и телефона — в build.py.

cmake_minimum_required(VERSION 3.21)
project(serve9 C CXX)

set(LLAMA_SRC "" CACHE PATH "исходники llama.cpp (vendor/llama.cpp из llama-cpp-python 0.3.37)")
if (NOT EXISTS "${LLAMA_SRC}/include/llama.h")
    message(FATAL_ERROR "LLAMA_SRC не указывает на исходники llama.cpp: ${LLAMA_SRC}")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(ANDROID)
    set(CMAKE_POSITION_INDEPENDENT_CODE ON)
endif()
if (NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(BUILD_SHARED_LIBS     OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_COMMON    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS     OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS     OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES  OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_APP       OFF CACHE BOOL "" FORCE)
set(LLAMA_OPENSSL         OFF CACHE BOOL "" FORCE)
add_subdirectory(${LLAMA_SRC} llama EXCLUDE_FROM_ALL)

# Из общей части llama.cpp нужен только движок Jinja (шаблон беседы) и то, на
# чём он стоит; сеть, загрузки и прочее общей части в программу не входят.
set(JINJA
    ${LLAMA_SRC}/common/jinja/lexer.cpp
    ${LLAMA_SRC}/common/jinja/parser.cpp
    ${LLAMA_SRC}/common/jinja/runtime.cpp
    ${LLAMA_SRC}/common/jinja/value.cpp
    ${LLAMA_SRC}/common/jinja/string.cpp
    ${LLAMA_SRC}/common/jinja/caps.cpp
    ${LLAMA_SRC}/common/unicode.cpp
    ${LLAMA_SRC}/common/json.cpp)

if(ANDROID)
    add_library(serve9 SHARED serve9.cpp ${JINJA})
    set_target_properties(serve9 PROPERTIES PREFIX "" SUFFIX "")
else()
    add_executable(serve9 serve9.cpp ${JINJA})
endif()
target_include_directories(serve9 PRIVATE ${LLAMA_SRC}/common ${LLAMA_SRC}/vendor)
target_link_libraries(serve9 PRIVATE llama)

# Прибор сверки шаблона беседы (experiments/native9/render9.cpp); собирается
# только по явной просьбе: cmake --build ПАПКА --target render9.
add_executable(render9 EXCLUDE_FROM_ALL ${CMAKE_CURRENT_SOURCE_DIR}/../../experiments/native9/render9.cpp ${JINJA})
target_include_directories(render9 PRIVATE ${LLAMA_SRC}/common ${LLAMA_SRC}/vendor)
target_link_libraries(render9 PRIVATE llama)
