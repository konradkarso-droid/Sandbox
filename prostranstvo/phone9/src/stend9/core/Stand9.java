package stend9.core;

import stend.core.Battery;
import stend.core.Exec;
import stend.core.Json;
import stend.core.Stand;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * Ведущий малого стенда шага 9б: запускается ли программа модели на
 * устройстве, живёт ли она, отказывает ли система в сети, сколько памяти
 * у двух процессов модели и умирают ли они вместе с исполнителем.
 *
 * Этапы, вопросы и вердикты объявлены до прогона в спецификации шага 9б,
 * §16; здесь они не повторяются. Ведущий одинаков на телефоне и на
 * сервере: всё, что зависит от устройства, даёт Device9.
 *
 * Итог пишется в файл после каждого этапа: оборванный прогон оставляет
 * всё, что успел.
 *
 * Чего не умеет: энергию не меряет и срок ответа не калибрует — это
 * калибровка (пункт 7); о нагреве судит только по датчику батареи.
 */
public final class Stand9 implements Runnable {
    /** sha256 Qwen2.5-1.5B-Instruct Q4_K_M — с этим файлом сверена программа модели. */
    public static final String KNOWN_SHA = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e";
    static final int TIMEOUT_S = 600;
    static final long WATCH_MS = 2000;
    static final double KILL_TEMP = 45.0;
    static final int KILL_LEVEL = 25;
    static final double START_MAX_TEMP = 37.0, PAUSE_TEMP = 40.0;
    static final int START_MIN_LEVEL = 40;
    static final long MAX_PAUSE_MS = 10 * 60 * 1000L;
    static final long DEATH_WAIT_MS = 2000;
    static final long BUSY_BEFORE_KILL_MS = 5000;

    /** Настройки new: цены шага 8в, бюджет набора шага 9 и режим 2 (контракт §18). */
    static Json.M cfg() {
        return new Json.M().with("graph", "M3т").with("levels", 4).with("memory", 4).with("budget", 1000)
                .with("price", 10).with("fail_price", 10).with("work_price", 10).with("gate_price", 5)
                .with("paid_attempts", 1).with("model", 2);
    }

    /** Вопросы этапа «скорость»: {имя, вопрос, предел ответа в байтах}. */
    static final String[][] SPEED = {
            {"короткий", "Назови столицу Франции одним словом.", "1024"},
            {"цикл агента", "Ты агент в песочнице. Правила: ЧИТАЙ ЗАДАНИЕ n, РЕШИ n, ОТВЕТ n x, СТОП. "
                    + "Ответь одной строкой — следующим ходом.\n> ЧИТАЙ ЗАДАНИЕ 1\n=> сложи 2 и 3\nХод:", "1024"},
            {"ответ до 256 байт", "Напиши рассказ о море.", "256"},
    };

    private final Device9 dev;
    private final Stand.Listener ui;
    private final File out;
    private final Json.M res = new Json.M();
    private volatile boolean stopPressed;
    private volatile String watchdogFired;
    private volatile Exec current;
    private final List<Process> direct = Collections.synchronizedList(new ArrayList<Process>());

    public Stand9(Device9 dev, Stand.Listener ui, File out) {
        this.dev = dev;
        this.ui = ui;
        this.out = out;
    }

    public void requestStop() {
        stopPressed = true;
    }

    /** Остановка: кнопка или сторож. */
    static final class Stopped extends Exception {
        Stopped(String why) {
            super(why);
        }
    }

    @Override
    public void run() {
        String why = "Готово.";
        Thread watch = new Thread(this::watchdog, "сторож");
        watch.setDaemon(true);
        try {
            res.with("стенд", "малый стенд шага 9б").with("устройство", dev.describe());
            phaseInfo();
            watch.start();
            String chosen = phaseServes();
            phaseNet(chosen);
            phaseSpeed(chosen);
            phaseMain(chosen);
            phaseDeath(chosen);
        } catch (Stopped s) {
            why = s.getMessage();
            res.with("остановлен", why);
        } catch (Exception e) {
            why = "Сбой стенда: " + e;
            res.with("сбой", String.valueOf(e));
        } finally {
            watch.interrupt();
            killAll();
            Json.M v = verdicts();
            res.with("вердикты", v);
            save();
            for (Map.Entry<String, Object> e : v.entrySet()) ui.log(e.getKey() + ": " + e.getValue());
            ui.status(why);
            ui.finished(why, out);
        }
    }

    // ---------------------------------------------------------------- этапы

    private void phaseInfo() throws Stopped, IOException {
        ui.status("Этап 0: сведения о телефоне и модели");
        long[] sys = Proc.system();
        Battery b = dev.battery();
        File m = dev.modelFile();
        Json.M info = new Json.M().with("память всего, МиБ", mib(sys[0])).with("память доступно, МиБ", mib(sys[1]))
                .with("место свободно, МиБ", dev.workDir().getUsableSpace() >> 20).with("батарея", b.toJson());
        res.with("0. сведения", info);
        if (!m.isFile()) throw new Stopped("Нет файла модели: выберите модель кнопкой «Модель».");
        ui.progress("Считаю контрольную сумму модели…");
        String sha = sha256(m);
        info.with("модель", new Json.M().with("файл", m.getName()).with("байт", m.length()).with("sha256", sha)
                .with("известная", KNOWN_SHA.equals(sha) ? "Qwen2.5-1.5B-Instruct Q4_K_M" : "нет — другой файл"));
        save();
        String refuse = startRefusal(b);
        if (refuse != null) throw new Stopped(refuse);
    }

    /** Этап 1: какие программы модели запускаются (заглушка, «ЭХО жив»). */
    private String phaseServes() throws Stopped {
        ui.status("Этап 1: запускаются ли программы модели");
        Json.M r = new Json.M();
        String chosen = null;
        for (String[] s : dev.serves()) {
            Json.M one = new Json.M();
            try {
                Direct d = new Direct(this, s[1], Collections.singletonList("--stub"));
                String a = d.ask("ЭХО жив", 50, 20);
                int code = d.finish(10);
                one.with("ответ", a).with("код выхода", code);
                boolean ok = "жив".equals(a) && code == 0;
                one.with("работает", ok);
                if (ok && chosen == null) chosen = s[1];
            } catch (Exception e) {
                one.with("работает", false).with("сбой", String.valueOf(e));
            }
            r.with(s[0], one);
            ui.log("Программа модели «" + s[0] + "»: " + (Boolean.TRUE.equals(one.get("работает")) ? "работает" : "не работает"));
        }
        r.with("выбрана", chosen == null ? null : name(chosen)).with("путь", chosen);
        res.with("1. программы модели", r);
        save();
        if (chosen == null) throw new Stopped("Ни одна программа модели не запустилась — дальше идти не с чем.");
        return chosen;
    }

    /** Этап 2: отказывает ли система в сети — проба самой программы модели. */
    private void phaseNet(String serve) throws Stopped {
        ui.status("Этап 2: отказывает ли телефон в сети");
        Json.M r = new Json.M();
        try {
            Direct d = new Direct(this, serve, Collections.singletonList("--probe-net"));
            String line = d.readLine(30);
            d.finish(10);
            r.with("ответ пробы", line);
            String code = line == null ? null : line.contains("Permission denied") ? "EACCES"
                    : line.contains("Operation not permitted") ? "EPERM" : "другой";
            r.with("отказ", code).with("сети нет", line != null && line.startsWith("сеть: нет"));
        } catch (Exception e) {
            r.with("сбой", String.valueOf(e));
        }
        res.with("2. сеть", r);
        save();
        ui.log("Сеть: " + r.get("ответ пробы"));
    }

    /** Этап 3: скорость каждой рабочей программы напрямую, без исполнителя. */
    private void phaseSpeed(String chosen) throws Stopped {
        Json.M r = new Json.M();
        res.with("3. скорость", r);
        List<String[]> runs = new ArrayList<>();
        Json.M serves = (Json.M) res.get("1. программы модели");
        for (String[] s : dev.serves()) {
            Object o = serves.get(s[0]);
            if (o instanceof Json.M && Boolean.TRUE.equals(((Json.M) o).get("работает"))) runs.add(new String[]{s[0], s[1], ""});
        }
        runs.add(new String[]{name(chosen) + ", 4 потока", chosen, "4"});
        for (String[] run : runs) {
            between();
            ui.status("Этап 3: скорость — " + run[0]);
            Json.M one = new Json.M();
            r.with(run[0], one);
            List<String> args = new ArrayList<>(Arrays.asList("--gguf", dev.modelFile().getPath()));
            if (!run[2].isEmpty()) args.addAll(Arrays.asList("--threads", run[2]));
            boolean fg = true;
            try {
                long t0 = dev.nanoTime();
                Direct d = new Direct(this, run[1], args);
                // Первый вопрос — с загрузкой модели; в скорость он не идёт.
                d.ask("Скажи «да».", 64, TIMEOUT_S);
                one.with("первый вопрос с загрузкой, с", sec(dev.nanoTime() - t0));
                List<Json.M> qs = new ArrayList<>();
                for (String[] q : SPEED) {
                    long cpu0 = Proc.cpuMs(d.pid), w0 = dev.nanoTime();
                    String a = d.ask(q[1], Integer.parseInt(q[2]), TIMEOUT_S);
                    double s = sec(dev.nanoTime() - w0);
                    long cpu = Proc.cpuMs(d.pid) - cpu0;
                    int bytes = a == null ? -1 : a.getBytes(StandardCharsets.UTF_8).length;
                    qs.add(new Json.M().with("вопрос", q[0]).with("секунд", s).with("время процессора, с", cpu0 < 0 ? null : cpu / 1000.0)
                            .with("байт ответа", bytes).with("байт в секунду", s > 0 && bytes >= 0 ? Math.round(bytes / s) : null));
                    ui.progress(run[0] + ": «" + q[0] + "» — " + s + " с, " + bytes + " байт");
                    fg &= dev.foreground();
                }
                one.with("вопросы", qs).with("наибольший RSS, МиБ", mib(Proc.mem(d.pid)[1]));
                d.finish(30);
            } catch (Stopped s) {
                throw s;
            } catch (Exception e) {
                one.with("сбой", String.valueOf(e));
            }
            if (!fg) one.with("свёрнут", true);
            save();
        }
    }

    /** Этап 4: исполнитель, режим 2, агент «20 вопросов» в обоих контурах. */
    @SuppressWarnings("unchecked")
    private void phaseMain(String serve) throws Stopped, IOException {
        between();
        ui.status("Этап 4: исполнитель и 20 вопросов в каждом контуре");
        Json.M r = new Json.M();
        res.with("4. главный прогон", r);
        File root = new File(dev.workDir(), "main");
        Exec ex;
        try {
            ex = startExec(root, serve);
        } catch (IOException e) {
            r.with("сбой запуска", String.valueOf(e)).with("ошибки исполнителя", tail(new File(root, "stderr.txt")));
            save();
            return;
        }
        Sampler sampler = new Sampler(ex.pid);
        boolean fg = true;
        try {
            String wd;
            try {
                wd = ex.newRun(cfg());
            } catch (IOException e) {
                r.with("new отказан", String.valueOf(e)).with("ошибки исполнителя", tail(new File(root, "stderr.txt")));
                save();
                return;
            }
            List<Long> kids = Proc.children(ex.pid);
            List<Json.M> kj = new ArrayList<>();
            for (long k : kids) kj.add(new Json.M().with("pid", k).with("программа", Proc.program(k)));
            r.with("процессы модели после new", kj).with("всех потомков исполнителя", Proc.descendants(ex.pid).size());
            int isolated = ex.isolatedModelsAlive();
            if (isolated >= 0) r.with("изолированных служб модели после new", isolated);
            sampler.start();
            byte[] prog = dev.asset("w9/model20.wasm");
            ex.load(1, prog);
            ex.load(2, prog);
            List<Json.M> runs = new ArrayList<>();
            for (int c = 1; c <= 2; c++) {
                checkStop();
                ui.progress("Контур " + c + ": 20 вопросов…");
                long t0 = dev.nanoTime();
                Object v;
                try {
                    v = ex.run(c);
                } catch (IOException e) {
                    v = "сбой: " + e;
                }
                fg &= dev.foreground();
                int alive = ex.isolatedModelsAlive();
                if (alive < 0) {
                    alive = 0;
                    for (long k : kids) if (Proc.alive(k)) alive++;
                }
                runs.add(new Json.M().with("контур", c).with("секунд", sec(dev.nanoTime() - t0)).with("итог run", v)
                        .with("живых процессов модели после", alive));
                ui.log("Контур " + c + ": " + sec(dev.nanoTime() - t0) + " с; живых процессов модели — " + alive);
                save();
            }
            r.with("запуски", runs);
            r.with("журнал", journal(new File(wd, "journal5.jsonl")));
        } finally {
            sampler.stop();
            Json.M memory = sampler.result();
            if (ex.isolatedModelsAlive() >= 0) {
                memory.with("сумма PSS процессов модели, наибольшая, МиБ", null)
                        .with("память модели", "Android ограничивает чтение /proc другого UID");
            }
            r.with("память", memory);
            if (!fg) r.with("свёрнут", true);
            current = null;
            ex.close();
            save();
        }
    }

    /** Этап 5: исполнитель убит, пока модель занята вопросом. */
    private void phaseDeath(String serve) throws Stopped {
        between();
        ui.status("Этап 5: умирает ли модель вместе с исполнителем");
        Json.M r = new Json.M();
        res.with("5. смерть с исполнителем", r);
        File root = new File(dev.workDir(), "death");
        try {
            final Exec ex = startExec(root, serve);
            ex.newRun(cfg());
            ex.load(1, dev.asset("w9/long1.wasm"));
            Thread t = new Thread(() -> {
                try {
                    ex.run(1);
                } catch (IOException ignored) {
                    // исполнитель убит посреди вопроса — так и задумано
                }
            }, "длинный ответ");
            t.setDaemon(true);
            // Все потомки, а не только дети: внук, которого запустила программа
            // модели, переживёт исполнителя, даже если дети умерли (§18.1).
            List<Long> kids = Proc.descendants(ex.pid);
            long[] cpu0 = new long[kids.size()];
            for (int i = 0; i < kids.size(); i++) cpu0[i] = Proc.cpuMs(kids.get(i));
            t.start();
            Thread.sleep(BUSY_BEFORE_KILL_MS);
            long busy = 0;
            for (int i = 0; i < kids.size(); i++) busy = Math.max(busy, Proc.cpuMs(kids.get(i)) - cpu0[i]);
            kids = Proc.descendants(ex.pid);
            r.with("потомков исполнителя перед убийством", kids.size()).with("работа модели до убийства, с процессора", busy / 1000.0);
            if (ex.isolatedModelsAlive() >= 0) {
                r.with("работа модели до убийства, с процессора", null)
                        .with("был запрос в обработке службой", ex.isolatedModelBusy());
            }
            ex.kill();
            Thread.sleep(DEATH_WAIT_MS);
            List<Long> left = new ArrayList<>();
            for (long k : kids) if (Proc.alive(k)) left.add(k);
            int isolated = ex.isolatedModelsAlive();
            int alive = isolated >= 0 ? isolated : left.size();
            r.with("живых через 2 с", alive);
            ui.log("Исполнитель убит; живых процессов модели через 2 с — " + alive);
            Proc.kill(left);
            current = null;
            ex.close();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new Stopped("Прервано");
        } catch (IOException e) {
            r.with("сбой", String.valueOf(e)).with("ошибки исполнителя", tail(new File(root, "stderr.txt")));
        }
        save();
    }

    // ---------------------------------------------------------------- вердикты

    @SuppressWarnings("unchecked")
    private Json.M verdicts() {
        Json.M v = new Json.M();
        Json.M s = (Json.M) res.get("1. программы модели");
        v.with("В6 программа модели запускается", s == null ? "не проверено" : s.get("выбрана") != null ? "да: " + s.get("выбрана") : "нет");
        Json.M n = (Json.M) res.get("2. сеть");
        v.with("В3 сети нет", n == null ? "не проверено" : Boolean.TRUE.equals(n.get("сети нет"))
                && ("EACCES".equals(n.get("отказ")) || "EPERM".equals(n.get("отказ"))) ? "да: " + n.get("отказ") : "нет");
        Json.M m = (Json.M) res.get("4. главный прогон");
        String topology = dev.isPhone() ? "В1 модель — изолированная служба" : "В1 модель — ребёнок исполнителя";
        if (m == null || m.get("журнал") == null) {
            v.with(topology, "не проверено").with("В2 40 вопросов без −7", "не проверено")
                    .with("В7 те же байты", "не проверено").with("В4 память", "не проверено");
        } else {
            List<Json.M> kids = (List<Json.M>) m.get("процессы модели после new");
            String serve = (String) ((Json.M) res.get("1. программы модели")).get("путь");
            boolean b1 = kids != null && kids.size() == 2;
            if (b1) for (Json.M k : kids) b1 &= serve != null && serve.equals(k.get("программа"));
            b1 &= Integer.valueOf(2).equals(m.get("всех потомков исполнителя"));
            boolean isolated = m.get("изолированных служб модели после new") != null;
            if (isolated) b1 = Integer.valueOf(2).equals(m.get("изолированных служб модели после new"));
            v.with(topology, b1 ? "да" : "нет");
            Json.M j = (Json.M) m.get("журнал");
            boolean alive = true;
            for (Json.M rr : (List<Json.M>) m.get("запуски")) alive &= ((Number) rr.get("живых процессов модели после")).intValue() == 2;
            boolean b2 = ((Number) j.get("ответов")).intValue() == 40 && ((Number) j.get("отказов −7")).intValue() == 0 && alive;
            v.with("В2 40 вопросов без −7", b2 ? "да" : "нет");
            v.with("В7 те же байты", Boolean.TRUE.equals(j.get("повтор в процессе — те же байты"))
                    && Boolean.TRUE.equals(j.get("другой процесс — те же байты")) ? "да" : "нет");
            v.with("В4 память", isolated ? "измеряется память исполнителя, память служб через /proc недоступна"
                    : b2 ? "да, числа — в «4. главный прогон» / «память»" : "нет");
        }
        Json.M d = (Json.M) res.get("5. смерть с исполнителем");
        // Убийство, когда модель не считала, проверяет не то: молчащий процесс
        // умер бы и без правила, по концу входа (урок 31).
        boolean busy = d != null && d.get("работа модели до убийства, с процессора") instanceof Number
                && ((Number) d.get("работа модели до убийства, с процессора")).doubleValue() > 0;
        busy |= d != null && Boolean.TRUE.equals(d.get("был запрос в обработке службой"));
        v.with("В5 модель умирает с исполнителем", d == null || d.get("живых через 2 с") == null ? "не проверено"
                : !busy ? "не проверено: модель не считала в момент убийства"
                : ((Number) d.get("живых через 2 с")).intValue() == 0 ? "да" : "нет");
        if (watchdogFired != null) v.with("сторож", watchdogFired);
        return v;
    }

    /** Ответы модели по журналу исполнителя: счёт, отказы, повторяемость. */
    @SuppressWarnings("unchecked")
    static Json.M journal(File f) throws IOException {
        Map<String, List<Object>> answers = new LinkedHashMap<>();
        int questions = 0, codes = 0, n = 0;
        List<Integer> lens = new ArrayList<>();
        for (String line : Files.readAllLines(f.toPath(), StandardCharsets.UTF_8)) {
            if (line.isEmpty()) continue;
            Map<String, Object> e = (Map<String, Object>) Json.parse(line);
            if (!"действие".equals(e.get("kind"))) continue;
            List<Object> sig = (List<Object>) e.get("signature");
            String rule = sig == null || sig.isEmpty() ? "" : String.valueOf(sig.get(0));
            if ("вопрос модели".equals(rule)) questions++;
            if (!"ответ модели".equals(rule)) continue;
            n++;
            if (e.get("code") != null) codes++;
            String subj = String.valueOf(e.get("subject"));
            answers.computeIfAbsent(subj, k -> new ArrayList<>()).add(e.get("answer"));
            if (e.get("length") instanceof Number) lens.add(((Number) e.get("length")).intValue());
        }
        boolean same = true, cross = true;
        List<Object> a1 = answers.get("а1"), a2 = answers.get("а2");
        for (List<Object> a : answers.values()) {
            for (int i = 0; i + 10 < a.size(); i++) same &= a.get(i) != null && a.get(i).equals(a.get(i + 10));
        }
        if (a1 == null || a2 == null || a1.size() != a2.size()) cross = false;
        else for (int i = 0; i < a1.size(); i++) cross &= a1.get(i) != null && a1.get(i).equals(a2.get(i));
        Collections.sort(lens);
        List<String> first = new ArrayList<>();
        if (a1 != null) for (int i = 0; i < Math.min(10, a1.size()); i++) {
            String s = String.valueOf(a1.get(i));
            first.add(s.length() > 80 ? s.substring(0, 80) + "…" : s);
        }
        return new Json.M().with("вопросов", questions).with("ответов", n).with("отказов −7", codes)
                .with("длины ответов, байт", lens).with("повтор в процессе — те же байты", same)
                .with("другой процесс — те же байты", cross).with("ответы контура 1 (начало)", first);
    }

    // ---------------------------------------------------------------- исполнитель и программы модели

    private Exec startExec(File root, String serve) throws IOException {
        Exec ex = dev.startModelExecutor(root, "5б", serve, TIMEOUT_S, null);
        current = ex;
        return ex;
    }

    /**
     * Программа модели, запущенная стендом напрямую: строки JSON по двум каналам.
     *
     * Класс статический, со ссылкой на ведущего: d8 из build-tools 34 падает на
     * нестатических внутренних классах, собранных javac 21 (как на перечислениях
     * в шаге 8, Safety.java).
     */
    private static final class Direct {
        final Stand9 s;
        final Process p;
        final long pid;
        final OutputStream in;
        final BufferedReader outR;

        Direct(Stand9 s, String exe, List<String> args) throws IOException {
            this.s = s;
            List<String> cmd = new ArrayList<>();
            cmd.add(exe);
            cmd.addAll(args);
            p = s.dev.startModel(cmd);
            s.direct.add(p);
            pid = pidOf(p);
            in = p.getOutputStream();
            outR = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8));
        }

        @SuppressWarnings("unchecked")
        String ask(String q, int max, int timeoutS) throws IOException, Stopped {
            String line = "{\"q\": \"" + Base64.getEncoder().encodeToString(q.getBytes(StandardCharsets.UTF_8)) + "\", \"max\": " + max + "}\n";
            in.write(line.getBytes(StandardCharsets.UTF_8));
            in.flush();
            String a = readLine(timeoutS);
            if (a == null) throw new IOException("программа модели не ответила");
            Map<String, Object> m = (Map<String, Object>) Json.parse(a);
            return new String(Base64.getDecoder().decode((String) m.get("a")), StandardCharsets.UTF_8);
        }

        /** Строка ответа; ожидание прерывается кнопкой и сторожем. */
        String readLine(int timeoutS) throws IOException, Stopped {
            final String[] got = new String[1];
            final IOException[] err = new IOException[1];
            Thread t = new Thread(() -> {
                try {
                    got[0] = outR.readLine();
                } catch (IOException e) {
                    err[0] = e;
                }
            }, "ответ модели");
            t.setDaemon(true);
            t.start();
            long end = System.currentTimeMillis() + timeoutS * 1000L;
            try {
                while (t.isAlive() && System.currentTimeMillis() < end) {
                    t.join(200);
                    if (s.stopPressed || s.watchdogFired != null) {
                        p.destroyForcibly();
                        s.checkStop();
                    }
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            if (t.isAlive()) {
                p.destroyForcibly();
                throw new IOException("нет ответа за " + timeoutS + " с");
            }
            if (err[0] != null) throw err[0];
            return got[0];
        }

        int finish(int timeoutS) {
            try {
                in.close();
            } catch (IOException ignored) {
            }
            try {
                if (!p.waitFor(timeoutS, TimeUnit.SECONDS)) {
                    p.destroyForcibly();
                    if (!p.waitFor(3, TimeUnit.SECONDS)) return -1;
                }
                return p.exitValue();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                p.destroyForcibly();
                return -1;
            } finally {
                try { outR.close(); } catch (IOException ignored) {}
                s.direct.remove(p);
            }
        }
    }

    /** Номер процесса: у Process на Android и в Java 8 нет pid(), он — в поле pid класса UNIXProcess. */
    static long pidOf(Process p) {
        try {
            java.lang.reflect.Field f = p.getClass().getDeclaredField("pid");
            f.setAccessible(true);
            return ((Number) f.get(p)).longValue();
        } catch (Exception e) {
            try {
                return (Long) Process.class.getMethod("pid").invoke(p);
            } catch (Exception e2) {
                return -1;
            }
        }
    }

    // ---------------------------------------------------------------- память по ходу

    /** Раз в секунду — память исполнителя, процессов модели и свободная память устройства. Статический — см. Direct. */
    private static final class Sampler implements Runnable {
        final long exe;
        final Map<Long, long[]> peak = new LinkedHashMap<>();
        long exePeakKb = -1, minAvailKb = Long.MAX_VALUE, samples;
        long pssSumPeakKb = -1;
        volatile boolean running = true;
        Thread t;

        Sampler(long exe) {
            this.exe = exe;
        }

        void start() {
            t = new Thread(this, "память");
            t.setDaemon(true);
            t.start();
        }

        public void run() {
            while (running) {
                sample();
                try {
                    Thread.sleep(1000);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }

        synchronized void sample() {
            samples++;
            exePeakKb = Math.max(exePeakKb, Proc.mem(exe)[0]);
            long pss = 0;
            for (long k : Proc.descendants(exe)) {
                long[] m = Proc.mem(k);
                long[] p = peak.get(k);
                if (p == null) peak.put(k, p = new long[]{-1, -1, -1, -1, -1});
                for (int i = 0; i < m.length; i++) p[i] = Math.max(p[i], m[i]);
                if (m[4] > 0) pss += m[4];
            }
            pssSumPeakKb = Math.max(pssSumPeakKb, pss);
            long avail = Proc.system()[1];
            if (avail > 0) minAvailKb = Math.min(minAvailKb, avail);
        }

        void stop() {
            running = false;
            if (t != null) t.interrupt();
            sample();
        }

        synchronized Json.M result() {
            Json.M r = new Json.M().with("замеров", samples).with("исполнитель, наибольший RSS, МиБ", mib(exePeakKb));
            List<Json.M> ks = new ArrayList<>();
            for (Map.Entry<Long, long[]> e : peak.entrySet()) {
                long[] p = e.getValue();
                ks.add(new Json.M().with("pid", e.getKey()).with("RSS, МиБ", mib(p[0])).with("наибольший RSS (VmHWM), МиБ", mib(p[1]))
                        .with("своих страниц, МиБ", mib(p[2])).with("страниц файла (веса), МиБ", mib(p[3])).with("PSS, МиБ", mib(p[4])));
            }
            return r.with("процессы модели — наибольшее", ks).with("сумма PSS процессов модели, наибольшая, МиБ", mib(pssSumPeakKb))
                    .with("наименьшая доступная память телефона, МиБ", minAvailKb == Long.MAX_VALUE ? null : mib(minAvailKb));
        }
    }

    // ---------------------------------------------------------------- безопасность

    /** Почему нельзя начинать; null — можно. Зарядка не мешает: энергия не меряется (§16). */
    String startRefusal(Battery b) {
        if (dev.isPhone() && b.tempC == null) return "Не начинаю: нет показаний температуры батареи";
        if (b.levelPct != null && b.levelPct < START_MIN_LEVEL)
            return "Не начинаю: заряд " + b.levelPct + " %, нужно от " + START_MIN_LEVEL + " %";
        if (b.tempC != null && b.tempC > START_MAX_TEMP)
            return "Не начинаю: батарея " + c(b.tempC) + " °C, подождите до " + (int) START_MAX_TEMP + " °C";
        return null;
    }

    /** Между этапами: остыть при 40 °C до 37 °C, не дольше 10 минут. */
    private void between() throws Stopped {
        checkStop();
        long t0 = System.currentTimeMillis();
        for (;;) {
            Battery b = dev.battery();
            if (b.tempC == null || b.tempC < PAUSE_TEMP) break;
            if (System.currentTimeMillis() - t0 > MAX_PAUSE_MS) throw new Stopped("Закончил раньше: долго остывает");
            ui.status("Пауза: батарея " + c(b.tempC) + " °C, жду остывания до " + (int) START_MAX_TEMP);
            while (b.tempC != null && b.tempC > START_MAX_TEMP && System.currentTimeMillis() - t0 <= MAX_PAUSE_MS) {
                sleep(5000);
                checkStop();
                b = dev.battery();
            }
        }
    }

    private void checkStop() throws Stopped {
        if (watchdogFired != null) throw new Stopped("Прерван сторожем: " + watchdogFired);
        if (stopPressed) throw new Stopped("Остановлено вами");
    }

    /** Сторож: раз в 2 секунды датчик батареи; при 45 °C или 25 % — убить исполнитель и модель. */
    private void watchdog() {
        while (!Thread.currentThread().isInterrupted()) {
            Battery b = dev.battery();
            String why = null;
            if (b.tempC != null && b.tempC >= KILL_TEMP) why = "батарея " + c(b.tempC) + " °C";
            else if (b.levelPct != null && b.levelPct <= KILL_LEVEL) why = "заряд " + b.levelPct + " %";
            if (why != null) {
                watchdogFired = why;
                killAll();
                return;
            }
            try {
                Thread.sleep(WATCH_MS);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    /** Убить исполнитель, его процессы модели и программы модели, запущенные напрямую. */
    private void killAll() {
        Exec ex = current;
        if (ex != null) {
            List<Long> kids = Proc.descendants(ex.pid);
            ex.kill();
            Proc.kill(kids);
        }
        synchronized (direct) {
            for (Process p : direct) p.destroyForcibly();
        }
    }

    // ---------------------------------------------------------------- мелочи

    private void save() {
        try {
            File d = out.getParentFile();
            if (d != null) d.mkdirs();
            Files.write(out.toPath(), (Json.str(res) + "\n").getBytes(StandardCharsets.UTF_8));
        } catch (IOException e) {
            ui.log("Не записал файл итогов: " + e.getMessage());
        }
    }

    private String name(String path) {
        for (String[] s : dev.serves()) if (s[1].equals(path)) return s[0];
        return path;
    }

    static String sha256(File f) throws IOException {
        try (InputStream in = new FileInputStream(f)) {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] buf = new byte[1 << 20];
            int n;
            while ((n = in.read(buf)) > 0) md.update(buf, 0, n);
            StringBuilder b = new StringBuilder();
            for (byte x : md.digest()) b.append(String.format("%02x", x));
            return b.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IOException(e);
        }
    }

    static String tail(File f) {
        try {
            String s = new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8);
            return s.length() > 600 ? s.substring(s.length() - 600) : s;
        } catch (IOException e) {
            return null;
        }
    }

    static Long mib(long kb) {
        return kb < 0 ? null : (kb + 512) / 1024;
    }

    static double sec(long ns) {
        return Math.round(ns / 1e7) / 100.0;
    }

    static String c(double t) {
        return String.format(new Locale("ru"), "%.1f", t);
    }

    static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
