package stend.server;

import stend.core.Battery;
import stend.core.Device;
import stend.core.Json;
import stend.core.Stand;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

/**
 * Тот же ведущий на сервере: вторая точка калибровки, только время и
 * память — батареи здесь нет, и энергию не измерить.
 *
 * java -cp КЛАССЫ stend.server.ServerMain --exe ИСПОЛНИТЕЛЬ --assets ПАПКА --out ФАЙЛ [--plan сервер|короткий|предел|предел-короткий|"предел 8в"|"предел 8в-короткий"|"энергия 8в"|"энергия 8в-короткий"]
 */
public final class ServerMain {
    public static void main(String[] args) throws Exception {
        String exe = null, assets = null, outPath = null, plan = "сервер";
        for (int i = 0; i < args.length; i += 2) {
            switch (args[i]) {
                case "--exe": exe = args[i + 1]; break;
                case "--assets": assets = args[i + 1]; break;
                case "--out": outPath = args[i + 1]; break;
                case "--plan": plan = args[i + 1]; break;
                default: throw new IllegalArgumentException("неизвестный ключ " + args[i]);
            }
        }
        if (exe == null || assets == null || outPath == null) throw new IllegalArgumentException("нужны --exe, --assets, --out");
        File out = new File(outPath);
        Stand s = new Stand(new ServerDevice(exe, new File(assets), new File(out.getAbsoluteFile().getParentFile(), "stend8_work")),
                new Stand.Listener() {
                    public void status(String x) { System.err.println("состояние: " + x); }
                    public void log(String x) { System.err.println("событие: " + x); }
                    public void progress(String x) { System.err.println(x); }
                    public void finished(String why, File result) { System.err.println("итог: " + why); }
                }, plan, out);
        s.run();
    }

    public static final class ServerDevice implements Device {
        private final String exe;
        private final File assets;
        private final File work;

        public ServerDevice(String exe, File assets, File work) {
            this.exe = exe;
            this.assets = assets;
            this.work = work;
        }

        public long nanoTime() { return System.nanoTime(); }
        public Battery battery() { return Battery.NONE; }
        public boolean foreground() { return true; }
        public boolean isPhone() { return false; }
        public String exe() { return exe; }
        public File workDir() { return work; }

        public byte[] asset(String name) throws IOException {
            return Files.readAllBytes(new File(assets, name).toPath());
        }

        public Json.M describe() {
            String cpu = "";
            try {
                for (String l : Files.readAllLines(new File("/proc/cpuinfo").toPath(), StandardCharsets.UTF_8))
                    if (l.startsWith("model name")) { cpu = l.split(":", 2)[1].trim(); break; }
            } catch (IOException ignored) {
            }
            return new Json.M().with("where", "сервер").with("os", System.getProperty("os.name"))
                    .with("arch", System.getProperty("os.arch")).with("cpu", cpu)
                    .with("cpus", Runtime.getRuntime().availableProcessors());
        }
    }
}
