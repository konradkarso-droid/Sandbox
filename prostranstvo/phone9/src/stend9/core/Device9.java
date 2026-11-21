package stend9.core;

import stend.core.Device;
import stend.core.Exec;

import java.io.File;
import java.io.IOException;
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

    default Process startModel(List<String> command) throws IOException {
        ProcessBuilder pb = new ProcessBuilder(command).redirectError(
                ProcessBuilder.Redirect.appendTo(new File(workDir(), "serve_stderr.txt")));
        pb.environment().putAll(extraEnv());
        return pb.start();
    }

    default Exec startModelExecutor(File root, String kind, String serve, int timeout, int[] threads) throws IOException {
        String mode = "--gguf " + modelFile().getPath();
        if (threads != null) mode += " --threads " + threads[0] + " --threads-batch " + threads[1];
        Map<String, String> env = new java.util.HashMap<>(extraEnv());
        env.put("MODEL_SERVE", serve);
        return new Exec(exe(), root, kind, java.util.Arrays.asList("--model=" + mode,
                "--model-timeout=" + timeout, "--model-net=probe"), env);
    }
}
