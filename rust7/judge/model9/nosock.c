/* Подменная функция socket() для проверки способа probe (контракт §18).

   На Android у приложения без разрешения на сеть система отказывает в самом
   создании сетевого сокета: EACCES (старый запрет ядра) или EPERM (нынешний).
   На сервере сокеты создаются, поэтому судья подкладывает исполнителю эту
   библиотеку через LD_PRELOAD — и проверяет, что исполнитель после такой
   пробы запускает программу модели напрямую.

   Чего библиотека не умеет: она подменяет только функцию socket() библиотеки
   C. Прямой системный вызов она не видит, и сеть на самом деле не отнята:
   это проверка логики исполнителя, а не изоляция. Изоляцию на телефоне даёт
   система, а на сервере — пустое сетевое пространство (способ netns).

   Поведение — переменная окружения NOSOCK:
     eperm (по умолчанию) — IPv4 и IPv6 отказаны с EPERM;
     eacces               — то же с EACCES;
     only4 / only6        — отказан только IPv4 / только IPv6, другой создаётся;
     stream               — отказаны только потоковые сокеты;
     afnosupport          — отказ с EAFNOSUPPORT (не «нет права»).
   Остальные семейства (сокеты внутри устройства и пр.) не трогаются.

   Сетевой сокет, который не отказан, обязан создаться: иначе проверки only4
   и only6 проверяли бы не «один сокет создан», а «другой отказан не так».
   Поэтому, если ядро сервера само не знает семейства (на сервере без IPv6 —
   EAFNOSUPPORT), вместо него создаётся сокет внутри устройства: для пробы
   это тоже «сокет создан». */

#define _GNU_SOURCE
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

int socket(int domain, int type, int protocol) {
    const char *m = getenv("NOSOCK");
    if (!m || !*m) m = "eperm";
    int inet = domain == AF_INET || domain == AF_INET6;
    int deny = inet;
    int err = EPERM;
    if (!strcmp(m, "eacces")) err = EACCES;
    else if (!strcmp(m, "only4")) deny = domain == AF_INET;
    else if (!strcmp(m, "only6")) deny = domain == AF_INET6;
    else if (!strcmp(m, "stream")) deny = inet && (type & 0xf) == SOCK_STREAM;
    else if (!strcmp(m, "afnosupport")) err = EAFNOSUPPORT;
    if (deny) {
        errno = err;
        return -1;
    }
    int fd = (int)syscall(SYS_socket, domain, type, protocol);
    if (fd < 0 && errno == EAFNOSUPPORT && inet)
        fd = (int)syscall(SYS_socket, AF_UNIX, type, 0);
    return fd;
}
