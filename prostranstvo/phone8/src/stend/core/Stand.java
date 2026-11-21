package stend.core;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Ведущий стенда шага 8: идёт по плану, запускает нагрузки через
 * исполнитель, меряет, пишет файл замеров (строки JSON).
 *
 * Без Android: экран, батарея и файлы приложения приходят через Device,
 * поэтому тот же ведущий работает на сервере.
 *
 * Прибор раньше механизма: всё, что стоит, говорит почему (Safety), и
 * причина пишется и на экран, и в файл. Сбой самого стенда — тоже строкой
 * в файле и на экране, а не тишиной.
 *
 * Чего не умеет: время шага run включает обмен строками с исполнителем —
 * это постоянная добавка, она уходит в свободный член прямой при разборе.
 */
public final class Stand {
    public interface Listener {
        void status(String s);

        void log(String s);

        void progress(String s);

        void finished(String why, File result);
    }

    private final Device dev;
    private final Listener ui;
    private final Plan plan;
    private final File result;
    private final File runRoot;
    private final long start;

    private volatile boolean stopPressed;
    private volatile String part = "начало";
    private volatile String kind;
    private volatile int round;
    private String stopWhy;
    private boolean heatPaused;
    private long heatStart;
    private long heatPausedMs;
    private Out out;

    private Map<String, Object> cfg;
    private List<String> kinds;
    private Map<String, List<Map<String, Object>>> work;
    private List<Map<String, Object>> channels;
    private final Map<String, byte[]> modules = new HashMap<>();

    public Stand(Device dev, Listener ui, String planName, File result) throws IOException {
        this.dev = dev;
        this.ui = ui;
        this.result = result;
        this.runRoot = new File(dev.workDir(), "runs");
        this.start = dev.nanoTime();
        loadWorkloads(planName.startsWith("предел 8в") ? "plan8v.json"
                : planName.startsWith("энергия 8в") ? "plan8ve.json"
                : planName.startsWith("предел") ? "plan8b.json" : "plan8.json");
        this.plan = Plan.of(planName, kinds);
    }

    public void requestStop() {
        stopPressed = true;
    }

    public Plan plan() {
        return plan;
    }

    @SuppressWarnings("unchecked")
    private void loadWorkloads(String planFile) throws IOException {
        Map<String, Object> p = (Map<String, Object>) Json.parse(
                new String(dev.asset(planFile), StandardCharsets.UTF_8));
        cfg = (Map<String, Object>) p.get("cfg");
        kinds = new ArrayList<>();
        for (Object k : (List<Object>) p.get("kinds")) kinds.add((String) k);
        work = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : ((Map<String, Object>) p.get("work")).entrySet()) {
            List<Map<String, Object>> l = new ArrayList<>();
            for (Object x : (List<Object>) e.getValue()) l.add((Map<String, Object>) x);
            work.put(e.getKey(), l);
        }
        channels = new ArrayList<>();
        for (Object x : (List<Object>) p.get("channels")) channels.add((Map<String, Object>) x);
    }

    private byte[] module(String file) throws IOException {
        byte[] b = modules.get(file);
        if (b == null) {
            b = dev.asset(file);
            modules.put(file, b);
        }
        return b;
    }

    private long ms() {
        return (dev.nanoTime() - start) / 1_000_000L;
    }

    private void event(String text) {
        out.line(new Json.M().with("type", "событие").with("text", text));
        ui.log(text);
    }

    public void run() {
        String refusal = Safety.startRefusal(dev.battery(), dev.isPhone());
        if (refusal == null && !new File(dev.exe()).canExecute())
            refusal = "Не могу запустить исполнитель: нет файла или права на запуск";
        if (refusal != null) {
            ui.status(refusal);
            ui.finished(refusal, null);
            return;
        }
        String why = "Готово: все замеры сделаны";
        boolean complete = false;
        Thread sampler = null;
        try {
            out = new Out(result, start, dev);
            Json.M sizes = new Json.M();
            for (Map.Entry<String, List<Map<String, Object>>> e : work.entrySet()) sizes.put(e.getKey(), e.getValue());
            out.line(new Json.M().with("type", "заголовок").with("version", 1).with("plan", plan.name)
                    .with("device", dev.describe())
                    .with("safety", new Json.M().with("start_max_temp", Safety.START_MAX_TEMP)
                            .with("pause_temp", Safety.PAUSE_TEMP).with("resume_temp", Safety.RESUME_TEMP)
                            .with("start_min_level", Safety.START_MIN_LEVEL).with("stop_level", Safety.STOP_LEVEL)
                            .with("max_heat_pause_ms", Safety.MAX_HEAT_PAUSE_MS))
                    .with("cfg", cfg).with("kinds", kinds).with("work", sizes)
                    .with("channels", channels.size()).with("planned_ms", plan.plannedMs()));
            sampler = new Thread(this::sample, "батарея");
            sampler.setDaemon(true);
            sampler.start();
            complete = true;
            int n = plan.steps.size();
            for (int i = 0; i < n; i++) {
                Plan.Step s = plan.steps.get(i);
                part = s.part;
                kind = s.kind;
                round = s.round;
                String what = s.kind == null ? s.part : s.part + " «" + s.kind + "»";
                ui.progress("Шаг " + (i + 1) + " из " + n + (s.round > 0 ? ", круг " + s.round : ""));
                ui.status("Идёт: " + what);
                out.line(new Json.M().with("type", "шаг").with("i", i).with("part", s.part).with("kind", s.kind)
                        .with("round", s.round).with("ms", s.ms));
                boolean ok;
                if (s.part.equals("работа")) ok = work(i, s);
                else if (s.part.equals("каналы")) ok = channels();
                else ok = idle(s);
                if (!ok) {
                    why = stopWhy;
                    complete = false;
                    break;
                }
            }
        } catch (Exception e) {
            why = "Сбой стенда: " + e;
            complete = false;
        } finally {
            part = "конец";
            if (sampler != null) sampler.interrupt();
            if (out != null) {
                out.line(new Json.M().with("type", "конец").with("why", why).with("complete", complete)
                        .with("heat_paused_ms", heatPausedMs));
                out.close();
            }
            ui.status(why);
            ui.finished(why, out == null ? null : result);
        }
    }

    private void sample() {
        while (!Thread.currentThread().isInterrupted()) {
            Battery b = dev.battery();
            out.line(new Json.M().with("type", "батарея").with("part", part).with("kind", kind).with("round", round)
                    .with("b", b.toJson()));
            try {
                Thread.sleep(500);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    /**
     * Проверка безопасности перед каждым замером. Пауза ждёт здесь.
     * Возвращает длительность паузы в мс (0 — паузы не было) или -1 —
     * останавливаемся, причина в stopWhy.
     */
    private long gate() throws InterruptedException {
        String before = part;
        long pauseStart = -1;
        String lastWhy = null;
        while (true) {
            long now = ms();
            long heatNow = heatPausedMs + (heatPaused ? now - heatStart : 0);
            Safety.Verdict v = Safety.during(dev.battery(), dev.isPhone(), dev.foreground(), stopPressed,
                    heatPaused, heatNow);
            if (v.act != Safety.Act.PAUSE_HEAT && heatPaused) {
                heatPausedMs += now - heatStart;
                heatPaused = false;
            }
            if (v.act == Safety.Act.GO) {
                if (pauseStart >= 0) {
                    part = before;
                    event("Продолжаю");
                    ui.status("Идёт: " + (kind == null ? part : part + " «" + kind + "»"));
                    return now - pauseStart;
                }
                return 0;
            }
            if (v.act == Safety.Act.STOP) {
                stopWhy = v.why;
                event(v.why);
                return -1;
            }
            if (pauseStart < 0) {
                pauseStart = now;
                part = "пауза";
            }
            if (v.act == Safety.Act.PAUSE_HEAT && !heatPaused) {
                heatPaused = true;
                heatStart = now;
            }
            if (!v.why.equals(lastWhy)) {
                event(v.why);
                ui.status(v.why);
                lastWhy = v.why;
            }
            Thread.sleep(1000);
        }
    }

    private boolean idle(Plan.Step s) throws InterruptedException {
        long t0 = ms();
        long paused = 0;
        while (ms() - t0 - paused < s.ms) {
            long g = gate();
            if (g < 0) return false;
            paused += g;
            Thread.sleep(Math.min(200, Math.max(1, s.ms - (ms() - t0 - paused))));
        }
        return true;
    }

    private static final class Trial {
        long ns, fuel, spent;
        int deliveries;
        String outcome;
    }

    private Trial trial(Exec ex, String file, Map<String, Object> runCfg) throws IOException {
        String wd = ex.newRun(runCfg);
        List<Object> lo = ex.load(1, module(file));
        if (!"загружен".equals(lo.get(0))) throw new IOException("нагрузка " + file + " не загрузилась: " + lo);
        long[] a = ex.fuelSpent(1);
        long t0 = dev.nanoTime();
        List<Object> ro = ex.run(1);
        long t1 = dev.nanoTime();
        long[] b = ex.fuelSpent(1);
        Trial t = new Trial();
        t.ns = t1 - t0;
        t.fuel = a[0] - b[0];
        t.spent = b[1];
        String why = ro.size() > 1 ? String.valueOf(ro.get(1)) : "";
        t.outcome = ro.get(0) + (why.isEmpty() ? "" : ": " + why);
        File out1 = new File(wd, "выход1.jsonl");
        if (out1.exists()) {
            for (String l : Files.readAllLines(out1.toPath(), StandardCharsets.UTF_8)) if (!l.trim().isEmpty()) t.deliveries++;
        }
        Exec.delete(ex.root, new File(wd));
        return t;
    }

    @SuppressWarnings("unchecked")
    private boolean work(int stepIdx, Plan.Step s) throws IOException, InterruptedException {
        List<Map<String, Object>> sizes = work.get(s.kind);
        // Нагрузка может просить исполнитель другого вида и свои настройки
        // (план шага 8в: попытки, которые есть только у `--kind 6`).
        Map<String, Object> first = sizes.get(0);
        String execKind = first.containsKey("exec") ? (String) first.get("exec") : "5б";
        Map<String, Object> runCfg = first.containsKey("cfg") ? (Map<String, Object>) first.get("cfg") : cfg;
        Map<String, Agg> aggs = new LinkedHashMap<>();
        for (Map<String, Object> w : sizes) aggs.put((String) w.get("file"), new Agg());
        long t0 = ms();
        long paused = 0;
        long peak;
        boolean ok = true;
        int i = 0;
        try (Exec ex = new Exec(dev.exe(), runRoot, execKind)) {
            while (ms() - t0 - paused < s.ms) {
                long g = gate();
                if (g < 0) {
                    ok = false;
                    break;
                }
                paused += g;
                String file = (String) sizes.get(i++ % sizes.size()).get("file");
                Trial t = trial(ex, file, runCfg);
                aggs.get(file).add(t.ns, t.fuel, t.spent, t.outcome);
            }
            peak = ex.peakKb();
        }
        for (Map<String, Object> w : sizes) {
            Agg a = aggs.get((String) w.get("file"));
            Json.M m = new Json.M().with("type", "замер").with("step", stepIdx).with("kind", s.kind)
                    .with("round", s.round).with("file", w.get("file")).with("n", w.get("n"));
            m.putAll(a.toJson());
            out.line(m);
        }
        out.line(new Json.M().with("type", "блок").with("step", stepIdx).with("kind", s.kind).with("round", s.round)
                .with("t0", t0).with("t1", ms()).with("paused_ms", paused).with("trials", i).with("peak_kb", peak));
        return ok;
    }

    private boolean channels() throws IOException, InterruptedException {
        Map<String, Integer> done = new HashMap<>();
        try (Exec ex = new Exec(dev.exe(), runRoot, "5б")) {
            int k = 0;
            for (Map<String, Object> c : channels) {
                String ch = (String) c.get("channel");
                int d = done.getOrDefault(ch, 0);
                if (plan.channelLimit >= 0 && d >= plan.channelLimit) continue;
                done.put(ch, d + 1);
                if (gate() < 0) return false;
                Trial t = trial(ex, (String) c.get("file"), cfg);
                out.line(new Json.M().with("type", "канал").with("channel", ch).with("i", d).with("sent", c.get("sent"))
                        .with("t_ns", t.ns).with("fuel", t.fuel).with("spent", t.spent).with("outcome", t.outcome)
                        .with("deliveries", t.deliveries));
                if (++k % 32 == 0) ui.status("Идёт: каналы, " + k + " из " + channels.size());
            }
        }
        return true;
    }

    /** Запись строк файла замеров. Пишут два потока: ведущий и батарея. */
    private static final class Out {
        private final BufferedWriter w;
        private final long start;
        private final Device dev;

        Out(File f, long start, Device dev) throws IOException {
            File dir = f.getParentFile();
            if (dir != null && !dir.isDirectory() && !dir.mkdirs()) throw new IOException("не создать папку " + dir);
            this.w = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(f), StandardCharsets.UTF_8));
            this.start = start;
            this.dev = dev;
        }

        synchronized void line(Json.M m) {
            Json.M x = new Json.M().with("t", (dev.nanoTime() - start) / 1_000_000L);
            x.putAll(m);
            try {
                w.write(Json.str(x));
                w.write('\n');
                w.flush();
            } catch (IOException ignored) {
                // Файл недоступен — замер всё равно идёт; причину покажет конец прогона.
            }
        }

        synchronized void close() {
            try {
                w.close();
            } catch (IOException ignored) {
            }
        }
    }
}
