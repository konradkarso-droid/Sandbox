package stend.core;

import java.io.File;
import java.io.IOException;

/** Что ведущему нужно от устройства. На телефоне — Android, на сервере — JVM. */
public interface Device {
    long nanoTime();

    Battery battery();

    /** Приложение на экране. На сервере всегда да. */
    boolean foreground();

    /** Телефон: без показаний батареи начинать нельзя (защита от нагрева). */
    boolean isPhone();

    /** Путь к исполнителю второй реализации. */
    String exe();

    /** Папка для рабочих папок исполнителя. */
    File workDir();

    /** Файл программы-нагрузки или плана по имени. */
    byte[] asset(String name) throws IOException;

    /** Что за устройство — для заголовка файла замеров. */
    Json.M describe();
}
