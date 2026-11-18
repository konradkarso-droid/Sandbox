package stend9.server;

import stend.core.Stand;
import stend9.core.Kalibr9;

import java.io.File;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Стенд калибровки на сервере — проверка ведущего до телефона: тот же
 * Kalibr9, исполнитель и программа модели для сервера, отказ сети
 * изображает подменная socket() судьи (как ServerMain9). Числа профиля
 * меньше телефонных, чтобы прогон с порчами шёл минуты, а не часы.
 *
 * java -cp КЛАССЫ stend9.server.ServerMainK9 --exe ИСПОЛНИТЕЛЬ --serve ПРОГРАММА --model GGUF
 *      --assets phone9/assets --nosock БИБЛИОТЕКА --out ПАПКА
 *      [--budget N] [--warm-ms N] [--warm-temp X] [--repeats N] [--live A,B] [--pairs 2x8,4x4]
 *      [--t-scale X] [--last К5] [--stop-after-s N]
 *
 * --stop-after-s — нажать «Остановить» через N секунд после начала: так на
 * сервере проверяется остановка посреди контура и продолжение прерванного
 * этапа.
 */
public final class ServerMainK9 {
    public static void main(String[] args) {
        Map<String, String> a = new HashMap<>();
        for (int i = 0; i + 1 < args.length; i += 2) a.put(args[i], args[i + 1]);
        for (String k : new String[]{"--exe", "--serve", "--model", "--assets", "--out"})
            if (!a.containsKey(k)) throw new IllegalArgumentException("нужен " + k);
        File out = new File(a.get("--out")).getAbsoluteFile();
        Map<String, String> env = new HashMap<>();
        if (a.containsKey("--nosock")) {
            env.put("LD_PRELOAD", a.get("--nosock"));
            env.put("NOSOCK", a.getOrDefault("--nosock-mode", "eperm"));
        }
        Kalibr9.Profile p = new Kalibr9.Profile();
        if (a.containsKey("--budget")) p.budget = Integer.parseInt(a.get("--budget"));
        if (a.containsKey("--warm-ms")) p.warmMs = Long.parseLong(a.get("--warm-ms"));
        if (a.containsKey("--warm-temp")) p.warmTemp = Double.parseDouble(a.get("--warm-temp"));
        if (a.containsKey("--repeats")) p.repeats = Integer.parseInt(a.get("--repeats"));
        if (a.containsKey("--live")) p.live = Arrays.asList(a.get("--live").split(","));
        if (a.containsKey("--t-scale")) p.tScale = Double.parseDouble(a.get("--t-scale"));
        if (a.containsKey("--last")) p.lastStage = a.get("--last");
        if (a.containsKey("--pairs")) {
            List<int[]> ps = new ArrayList<>();
            for (String x : a.get("--pairs").split(",")) {
                String[] t = x.split("x");
                ps.add(new int[]{Integer.parseInt(t[0]), Integer.parseInt(t[1])});
            }
            p.pairs = ps.toArray(new int[0][]);
        }
        ServerMain9.Dev dev = new ServerMain9.Dev(a.get("--exe"), a.get("--serve"), new File(a.get("--model")),
                new File(a.get("--assets")), new File(out, "work"), env);
        final Kalibr9 k = new Kalibr9(dev, new Stand.Listener() {
            public void status(String x) { System.err.println("состояние: " + x); }
            public void log(String x) { System.err.println("событие: " + x); }
            public void progress(String x) { System.err.println("ход: " + x); }
            public void finished(String why, File result) { System.err.println("итог: " + why); }
        }, out, p);
        if (a.containsKey("--stop-after-s")) {
            final long ms = (long) (Double.parseDouble(a.get("--stop-after-s")) * 1000);
            Thread t = new Thread(() -> {
                try {
                    Thread.sleep(ms);
                } catch (InterruptedException e) {
                    return;
                }
                System.err.println("событие: нажато «Остановить»");
                k.requestStop();
            }, "кнопка");
            t.setDaemon(true);
            t.start();
        }
        k.run();
    }
}
