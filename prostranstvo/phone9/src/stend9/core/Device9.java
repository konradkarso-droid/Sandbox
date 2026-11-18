package stend9.core;

import stend.core.Device;

import java.io.File;
import java.util.List;
import java.util.Map;

/** Что малому стенду нужно от устройства сверх стенда шага 8. */
public interface Device9 extends Device {
    /**
     * Программы модели в порядке предпочтения: {имя, путь}. На телефоне —
     * быстрая (dotprod, i8mm), затем переносимая; на сервере — одна.
     */
    List<String[]> serves();

    // asset(name) — путь внутри папки нагрузок phone9/assets (в APK — assets/),
    // одинаковый на телефоне и на сервере: «w9/model20.wasm», «k9/questions.json».
    // Устройство имя не переделывает, иначе проверка на сервере шла бы по
    // другим путям, чем телефон.

    /** Файл модели GGUF, уже в папке стенда. */
    File modelFile();

    /**
     * Переменные окружения для исполнителя и программ модели поверх обычных.
     * На телефоне пусто; на сервере — подменная socket() судьи, которая
     * изображает отказ сети Android (способ probe, контракт §18.1).
     */
    Map<String, String> extraEnv();
}
