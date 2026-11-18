package stend9.server;

import stend.core.Battery;
import stend.core.Json;
import stend.core.Stand;
import stend9.core.Device9;
import stend9.core.Stand9;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Малый стенд на сервере — проверка ведущего до телефона: тот же Stand9,
 * исполнитель и программа модели для сервера. Отказ сети Android здесь
 * изображает подменная socket() судьи (rust7/judge/model9/nosock.c) через
 * LD_PRELOAD — у исполнителя и у программы модели.
 *
 * java -cp КЛАССЫ stend9.server.ServerMain9 --exe ИСПОЛНИТЕЛЬ --serve SERVE9 --model GGUF
 *      --assets phone9/assets --nosock БИБЛИОТЕКА [--nosock-mode eperm|eacces] --out ФАЙЛ
 */
public final class ServerMain9 {
    public static void main(String[] args) throws Exception {
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
        Device9 dev = new Dev(a.get("--exe"), a.get("--serve"), new File(a.get("--model")), new File(a.get("--assets")),
                new File(out.getParentFile(), "stend9_work"), env);
        new Stand9(dev, new Stand.Listener() {
            public void status(String x) { System.err.println("состояние: " + x); }
            public void log(String x) { System.err.println("событие: " + x); }
            public void progress(String x) { System.err.println(x); }
            public void finished(String why, File result) { System.err.println("итог: " + why); }
        }, out).run();
    }

    static final class Dev implements Device9 {
        final String exe, serve;
        final File model, assets, work;
        final Map<String, String> env;

        Dev(String exe, String serve, File model, File assets, File work, Map<String, String> env) {
            this.exe = exe;
            this.serve = serve;
            this.model = model;
            this.assets = assets;
            this.work = work;
            this.env = env;
        }

        public long nanoTime() { return System.nanoTime(); }
        public Battery battery() { return Battery.NONE; }
        public boolean foreground() { return true; }
        public boolean isPhone() { return false; }
        public String exe() { return exe; }
        public File workDir() { work.mkdirs(); return work; }
        public byte[] asset(String name) throws IOException { return Files.readAllBytes(new File(assets, name).toPath()); }
        public List<String[]> serves() { return Collections.singletonList(new String[]{"сервер", serve}); }
        public File modelFile() { return model; }
        public Map<String, String> extraEnv() { return env; }

        public Json.M describe() {
            return new Json.M().with("where", "сервер").with("os", System.getProperty("os.name"))
                    .with("arch", System.getProperty("os.arch")).with("cpus", Runtime.getRuntime().availableProcessors());
        }
    }
}
