package stend9.core;

import stend.core.Battery;
import stend.core.Exec;
import stend.core.Json;
import stend.core.Stand;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * Ведущий стенда калибровки шага 9б: числа потоков, время вопроса на
 * холодном и прогретом телефоне, срок T и предел₂, проверка предела
 * программами, которые тратят как можно больше, и живой агент.
 *
 * Этапы К0–К6, вопросы и вердикты объявлены до прогона в спецификации
 * шага 9б, §17; здесь не повторяются. Всё, что зависит от устройства, даёт
 * Device9; настройки, которыми проверка на сервере отличается от телефона
 * (бюджет, длина прогрева, сценарии), — Profile.
 *
 * Прогон можно прервать в любой момент (кнопка, сторож, заряд): итог
 * пишется после каждого этапа, и следующий запуск продолжает с первого
 * незаконченного этапа, если модель та же. Этап, прерванный посередине,
 * начинается заново; что успело собраться — в «прерывания».
 *
 * Энергию стенд не считает: он пишет журнал батареи (battery.csv, раз в
 * 0,5 с) и время каждого вопроса по часам стены, а энергию и проверку
 * единиц считает разбор на сервере (§17).
 *
 * Чего не умеет: о нагреве судит по датчику батареи; законы в журналах
 * живого агента проверяет не он, а наблюдатели на сервере по файлам,
 * которые он кладёт в папку итогов.
 */
public final class Kalibr9 implements Runnable {
    private static final String K5_TIMING_MODE = "только К5 по времени";
    static final double Z = 1.5;
    /** Предел₁ — работа среды без модели: 155 мс старого телефона × 10, не мерено на этом (§17). */
    static final double LIMIT1_S = 1.55;
    static final int A = 256, U = 906, G = 5;
    static final int[] SIZES = {256, 1024, 2048, 4096};
    static final int ANSWER_FULL = 100;
    static final long WATCH_MS = 500;
    static final double KILL_TEMP = 45.0, START_MAX_TEMP = 37.0, PAUSE_TEMP = 40.0;
    static final int KILL_LEVEL = 25, START_MIN_LEVEL = 40;
    /**
     * К5 на телефоне — около 75 минут полной нагрузки (41 вопрос Р4096 и 56
     * вопросов Р256). iQOO Neo 10 под такой нагрузкой тратит около 0,5 %
     * заряда в минуту, то есть около 40 %; сторож кончает на 25 %. С меньшим
     * зарядом этап заведомо не дойдёт до конца — он и не начинается. На
     * зарядке не проверяется: заряд не убывает (энергия К5 тогда не меряется).
     */
    static final int K5_MIN_LEVEL = 80;
    static final long MAX_PAUSE_MS = 10 * 60 * 1000L;
    static final int DIRECT_TIMEOUT_S = 900;
    static final String[] STAGES = {"К1", "К2", "К3", "К4", "К5", "К6"};

    /** Чем проверка на сервере отличается от телефона. */
    public static final class Profile {
        public int budget = 625;
        public long warmMs = 10 * 60 * 1000L;
        public double warmTemp = 40.0;
        public int repeats = 3;
        public List<String> live = Arrays.asList("честный", "к E", "наружу");
        public int[][] pairs = {{2, 8}, {4, 4}, {4, 8}, {6, 8}, {8, 8}};
        /** Срок T = tScale × t(4096); на телефоне 1,5 (§4.3), порча «срок короче» — меньше. */
        public double tScale = Z;
        /** Последний этап прогона: проверка порчей идёт только до своего этапа. */
        public String lastStage = "К6";
    }

    private final Device9 dev;
    private final Stand.Listener ui;
    private final File dir;
    private final Profile prof;
    private final File k5Baseline;
    private Json.M res = new Json.M();
    private volatile String stopReason;
    private volatile String watchdogFired;
    private volatile Exec current;
    private final List<Process> direct = Collections.synchronizedList(new ArrayList<Process>());
    private Map<String, byte[]> qs;

    /** dir — папка итогов: kalibr9.json, battery.csv и файлы живого агента. */
    public Kalibr9(Device9 dev, Stand.Listener ui, File dir, Profile prof) {
        this(dev, ui, dir, prof, null);
    }

    private Kalibr9(Device9 dev, Stand.Listener ui, File dir, Profile prof, File k5Baseline) {
        this.dev = dev;
        this.ui = ui;
        this.dir = dir;
        this.prof = prof;
        this.k5Baseline = k5Baseline;
    }

    /** Run К5 in a fresh report directory using the completed stages of a previous calibration. */
    public static Kalibr9 repeatK5(Device9 dev, Stand.Listener ui, File dir, File baseline) {
        Profile prof = new Profile();
        prof.lastStage = "К5";
        return new Kalibr9(dev, ui, dir, prof, baseline);
    }

    /**
     * Кнопка «Остановить». Исполнитель отвечает на команду run только в
     * конце контура (в К5 — через час), поэтому флага мало: исполнитель и
     * модель убиваются сразу, как это делает сторож, — run возвращается со
     * сбоем, и этап кончается на checkStop. Убивает отдельный поток: кнопка
     * нажимается в потоке экрана.
     */
    public void requestStop() {
        stop("Остановлено вами");
    }

    public void movedToBackground() {
        if (k5Baseline != null) stop("К5 прерван: приложение было свёрнуто. Для замера держите стенд на экране.");
    }

    private synchronized void stop(String reason) {
        if (stopReason != null) return;
        stopReason = reason;
        Thread t = new Thread(this::killAll, "остановка");
        t.setDaemon(true);
        t.start();
    }

    public File resultFile() {
        return new File(dir, "kalibr9.json");
    }

    static final class Stopped extends Exception {
        Stopped(String why) {
            super(why);
        }
    }

    // ---------------------------------------------------------------- ход прогона

    @Override
    @SuppressWarnings("unchecked")
    public void run() {
        String why = k5Baseline == null ? "Готово: все этапы пройдены." : "Повтор К5 завершён.";
        dir.mkdirs();
        Thread watch = new Thread(this::watchdog, "сторож и батарея");
        watch.setDaemon(true);
        try {
            qs = questions();
            resume();
            checkStop();
            phaseInfo();
            watch.start();
            for (String st : STAGES) {
                if (done(st)) continue;
                if (st.compareTo(prof.lastStage) > 0) break;
                Json.M stage = new Json.M().with("начат", System.currentTimeMillis());
                stages().with(st, stage);
                ui.progress("");
                try {
                    if (st.equals("К1")) stageThreads(stage);
                    else if (st.equals("К2")) stageTiming(stage, false);
                    else if (st.equals("К3")) stageTiming(stage, true);
                    else if (st.equals("К4")) stageNumbers(stage);
                    else if (st.equals("К5")) stageLimit(stage);
                    else stageLive(stage);
                    stage.with("закончен", System.currentTimeMillis());
                } catch (Stopped | IOException | RuntimeException s) {
                    // Этап, прерванный посередине, начнётся заново; собранное — в «прерывания».
                    interruptions().add(new Json.M().with("этап", st).with("почему", String.valueOf(s.getMessage()))
                            .with("когда", System.currentTimeMillis()).with("собрано", stage));
                    stages().remove(st);
                    throw s;
                }
                save();
            }
        } catch (Stopped s) {
            why = s.getMessage();
            if (k5Baseline != null) res.with("остановлен", why);
        } catch (Exception e) {
            why = "Сбой стенда: " + e;
            res.with("сбой", String.valueOf(e));
        } finally {
            watch.interrupt();
            killAll();
            res.with("вердикты", verdicts());
            save();
            for (Map.Entry<String, Object> e : ((Json.M) res.get("вердикты")).entrySet()) ui.log(e.getKey() + ": " + e.getValue());
            ui.status(why);
            ui.finished(why, resultFile());
        }
    }

    @SuppressWarnings("unchecked")
    private void resume() throws IOException, Stopped {
        File f = k5Baseline == null ? resultFile() : k5Baseline;
        if (!f.isFile()) {
            if (k5Baseline != null) throw new Stopped("Для повтора К5 нужны сохранённые этапы К1–К4.");
            return;
        }
        Object o = Json.parse(new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8));
        if (!(o instanceof Map)) {
            if (k5Baseline != null) throw new Stopped("Файл исходной калибровки не читается.");
            return;
        }
        Map<String, Object> old = (Map<String, Object>) o;
        if (k5Baseline == null) res.putAll(old);
        else loadK5Baseline(old);
        res.remove("вердикты");
        res.remove("сбой");
        List<Object> starts = (List<Object>) res.get("запуски");
        if (starts == null) res.with("запуски", starts = new ArrayList<>());
        starts.add(System.currentTimeMillis());
    }

    private void loadK5Baseline(Map<String, Object> old) throws Stopped {
        Object storedStages = old.get("этапы"), model = old.get("модель"), settings = old.get("настройки");
        if (!(storedStages instanceof Map) || !(model instanceof Map) || !(settings instanceof Map)) {
            throw new Stopped("Для повтора К5 нужны сохранённые этапы К1–К4 и проверенная модель.");
        }
        Object digest = ((Map<?, ?>) model).get("sha256");
        Object budget = ((Map<?, ?>) settings).get("бюджет");
        if (!(digest instanceof String) || !((String) digest).matches("[a-f0-9]{64}")) {
            throw new Stopped("В исходном отчёте нет контрольной суммы модели.");
        }
        if (!(budget instanceof Number) || ((Number) budget).doubleValue() != prof.budget) {
            throw new Stopped("Бюджет исходной калибровки отличается от настроек К5.");
        }
        Json.M preserved = new Json.M();
        for (String name : STAGES) {
            if (name.equals("К5")) break;
            Object prior = ((Map<?, ?>) storedStages).get(name);
            if (!completed(prior)) throw new Stopped("Для повтора К5 нужен завершённый этап " + name + ".");
            preserved.with(name, prior);
        }
        res.with("режим", K5_TIMING_MODE).with("модель", model).with("этапы", preserved);
    }

    @SuppressWarnings("unchecked")
    private Json.M stages() {
        Object s = res.get("этапы");
        if (!(s instanceof Map)) res.with("этапы", s = new Json.M());
        if (!(s instanceof Json.M)) {
            Json.M m = new Json.M();
            m.putAll((Map<String, Object>) s);
            res.with("этапы", s = m);
        }
        return (Json.M) s;
    }

    @SuppressWarnings("unchecked")
    private List<Object> interruptions() {
        Object s = res.get("прерывания");
        if (!(s instanceof List)) res.with("прерывания", s = new ArrayList<>());
        return (List<Object>) s;
    }

    private boolean done(String st) {
        return completed(stages().get(st));
    }

    private static boolean completed(Object s) {
        return s instanceof Map && ((Map<?, ?>) s).get("закончен") != null;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> stage(String st) {
        return (Map<String, Object>) stages().get(st);
    }

    // ---------------------------------------------------------------- К0

    private void phaseInfo() throws Stopped, IOException {
        ui.status("К0: сведения о телефоне и модели");
        long[] sys = Proc.system();
        Battery b = dev.battery();
        File m = dev.modelFile();
        if (!m.isFile()) throw new Stopped("Нет файла модели: выберите модель кнопкой «Модель».");
        Object known = res.get("модель") instanceof Map ? ((Map<?, ?>) res.get("модель")).get("sha256") : null;
        ui.progress("Считаю контрольную сумму модели…");
        String sha = Stand9.sha256(m);
        ui.progress("");
        if (known != null && !known.equals(sha)) {
            throw new Stopped("Модель другая, чем в начатой калибровке. Чтобы начать заново, нажмите «Сначала».");
        }
        ui.log(known != null ? "Модель та же, что в начатой калибровке: контрольная сумма сверена." : "Контрольная сумма модели посчитана.");
        res.with("стенд", "калибровка шага 9б").with("устройство", dev.describe())
                .with("модель", new Json.M().with("байт", m.length()).with("sha256", sha)
                        .with("известная", Stand9.KNOWN_SHA.equals(sha) ? "Qwen2.5-1.5B-Instruct Q4_K_M" : "нет — другой файл"))
                .with("сведения в начале запуска", new Json.M().with("память всего, МиБ", Stand9.mib(sys[0]))
                        .with("память доступно, МиБ", Stand9.mib(sys[1])).with("батарея", b.toJson()))
                .with("настройки", new Json.M().with("бюджет", prof.budget).with("A", A).with("U", U).with("g", G)
                        .with("z", Z).with("срок T от t(4096)", prof.tScale).with("предел₁, с", LIMIT1_S)
                        .with("прогрев, мс", prof.warmMs).with("прогрев до °C", prof.warmTemp).with("повторов", prof.repeats)
                        .with("сценарии", prof.live).with("до этапа", prof.lastStage));
        String refuse = startRefusal(b);
        // «Сведения в начале запуска» каждый запуск перезаписывает; отказ
        // начать остаётся только здесь.
        if (refuse != null) startRefusals().add(new Json.M().with("когда", System.currentTimeMillis()).with("почему", refuse));
        save();
        if (refuse != null) throw new Stopped(refuse);
    }

    @SuppressWarnings("unchecked")
    private List<Object> startRefusals() {
        Object s = res.get("отказы в начале");
        if (!(s instanceof List)) res.with("отказы в начале", s = new ArrayList<>());
        return (List<Object>) s;
    }

    // ---------------------------------------------------------------- К1

    private void stageThreads(Json.M r) throws Stopped {
        between();
        String serve = chooseServe();
        r.with("программа модели", name(serve)).with("путь", serve);
        List<Json.M> rows = new ArrayList<>();
        r.with("пары", rows);
        double best = Double.MAX_VALUE;
        int[] bestPair = null;
        for (int[] p : prof.pairs) {
            checkStop();
            ui.status("К1: потоки " + p[0] + " и " + p[1]);
            Model mdl = new Model(this, serve, p);
            try {
                mdl.ask(qs.get("прогрев"), 64);
                Json.M q = mdl.timed("Р4096", qs.get("Р4096"), A);
                q.with("выдача", p[0]).with("чтение", p[1]);
                rows.add(q);
                double s = ((Number) q.get("секунд")).doubleValue();
                if (s < best) {
                    best = s;
                    bestPair = p;
                }
            } finally {
                mdl.finish();
            }
            save();
        }
        String sha0 = null;
        boolean same = true;
        for (Json.M q : rows) {
            if (sha0 == null) sha0 = (String) q.get("sha256 ответа");
            same &= sha0 != null && sha0.equals(q.get("sha256 ответа"));
        }
        r.with("выбрано", Arrays.asList(bestPair[0], bestPair[1])).with("ответ при всех парах один", same);
    }

    // ---------------------------------------------------------------- К2, К3

    @SuppressWarnings("unchecked")
    private void stageTiming(Json.M r, boolean warm) throws Stopped {
        if (!warm) coolTo(START_MAX_TEMP);
        String serve = serveOf();
        int[] pair = pairOf();
        Model mdl = new Model(this, serve, pair);
        try {
            mdl.ask(qs.get("прогрев"), 64);
            r.with("батарея в начале", dev.battery().toJson());
            // Покой для мощности фона P₀ (§17): модель загружена и молчит.
            // У прогретого телефона — только после вопросов, чтобы покой не
            // остудил его до замера.
            if (!warm) r.with("покой до", idle());
            if (warm) {
                long t0 = System.currentTimeMillis();
                int n = 0;
                for (;;) {
                    Battery b = dev.battery();
                    if (System.currentTimeMillis() - t0 >= prof.warmMs) break;
                    if (b.tempC != null && b.tempC >= prof.warmTemp) break;
                    ui.status("К3: прогрев — " + (System.currentTimeMillis() - t0) / 1000 + " с" + (b.tempC == null ? "" : ", " + Stand9.c(b.tempC) + " °C"));
                    mdl.timed("Р4096", qs.get("Р4096"), A);
                    n++;
                }
                r.with("прогрев", new Json.M().with("секунд", (System.currentTimeMillis() - t0) / 1000.0).with("вопросов", n)
                        .with("батарея после", dev.battery().toJson()));
            }
            List<Json.M> rows = new ArrayList<>();
            r.with("вопросы", rows);
            for (int k = 1; k <= prof.repeats; k++) {
                for (String q : new String[]{"Р256", "Р1024", "Р2048", "Р4096", "О"}) {
                    ui.status((warm ? "К3" : "К2") + ": круг " + k + ", " + q);
                    rows.add(mdl.timed(q, qs.get(q), A).with("круг", k));
                    save();
                }
            }
            r.with("покой после", idle());
            r.with("батарея в конце", dev.battery().toJson());
            Map<String, String> sha = new HashMap<>();
            boolean same = true, full = true;
            for (Json.M q : rows) {
                String k = (String) q.get("вопрос");
                String s = (String) q.get("sha256 ответа");
                if (sha.containsKey(k)) same &= sha.get(k).equals(s);
                else sha.put(k, s);
                if (k.startsWith("Р")) full &= ((Number) q.get("байт ответа")).intValue() >= ANSWER_FULL;
            }
            r.with("повтор — те же байты", same).with("каждый Р до предела", full);
        } finally {
            mdl.finish();
        }
    }

    // ---------------------------------------------------------------- К4

    static int price(int b) {
        return 2 * G + (b + A + U - 1) / U;
    }

    @SuppressWarnings("unchecked")
    private void stageNumbers(Json.M r) {
        Map<Integer, Double> t = new LinkedHashMap<>();
        double usual = 0;
        for (String st : new String[]{"К2", "К3"}) {
            for (Map<String, Object> q : (List<Map<String, Object>>) stage(st).get("вопросы")) {
                String k = (String) q.get("вопрос");
                double s = ((Number) q.get("секунд")).doubleValue();
                if (k.equals("О")) usual = Math.max(usual, s);
                else t.merge(Integer.parseInt(k.substring(1)), s, Math::max);
            }
        }
        double worst = 0;
        List<Json.M> rows = new ArrayList<>();
        for (int b : SIZES) {
            int p = price(b), n = prof.budget / p;
            double v = n * t.get(b);
            worst = Math.max(worst, v);
            rows.add(new Json.M().with("b", b).with("p(b)", p).with("вопросов не больше", n).with("t(b), с", t.get(b))
                    .with("вопросов × t, с", round(v)));
        }
        int T = (int) Math.ceil(prof.tScale * t.get(4096));
        double limit2 = LIMIT1_S + Z * worst;
        r.with("по размерам", rows).with("срок T, с", T).with("предел₂, с", round(limit2))
                .with("обычный вопрос, с", usual).with("предел₁, с", LIMIT1_S);
    }

    // ---------------------------------------------------------------- К5

    @SuppressWarnings("unchecked")
    private void stageLimit(Json.M r) throws Stopped, IOException {
        Battery b = dev.battery();
        if (dev.isPhone() && b.levelPct != null && !Boolean.TRUE.equals(b.plugged) && b.levelPct < K5_MIN_LEVEL) {
            throw new Stopped("К5 — около 75 минут полной нагрузки: нужно от " + K5_MIN_LEVEL + " % заряда, сейчас "
                    + b.levelPct + " %. Зарядите, отключите зарядку, дайте батарее остыть до " + (int) START_MAX_TEMP
                    + " °C и нажмите «Начать»");
        }
        if (dev.isPhone() && Boolean.TRUE.equals(b.plugged)) ui.log("К5 на зарядке: время меряется, энергия — нет.");
        between();
        Map<String, Object> k4 = stage("К4");
        int T = ((Number) k4.get("срок T, с")).intValue();
        double limit2 = ((Number) k4.get("предел₂, с")).doubleValue();
        int[] sizes = {4096, 256};
        File root = new File(dev.workDir(), k5Baseline == null ? "k5" : "k5-repeat");
        Exec ex = startExec(root, T, "5б");
        File journal = null;
        List<Json.M> runs = new ArrayList<>();
        Json.M last = null;
        try {
            Json.M cfg = seriesCfg();
            // Задачи — для агента с заданиями (вид «6», этап К6); программам
            // «наибольшие» и «малые» они не нужны, и вид «5б» их не знает.
            cfg.keySet().removeIf(k -> k.startsWith("task_"));
            String wd = ex.newRun(cfg);
            journal = new File(wd, "journal5.jsonl");
            ex.load(1, dev.asset("k9/big.wasm"));
            ex.load(2, dev.asset("k9/small.wasm"));
            for (int c = 1; c <= 2; c++) {
                checkStop();
                ui.status("К5: контур " + c + (c == 1 ? " — наибольшие вопросы" : " — малые вопросы"));
                Progress pg = new Progress(this, journal, "а" + c, "Контур " + c, prof.budget / price(sizes[c - 1]), T);
                long t0 = dev.nanoTime(), w0 = System.currentTimeMillis();
                Object v;
                Json.M seen;
                pg.start();
                try {
                    v = ex.run(c);
                } catch (IOException e) {
                    v = "сбой: " + e;
                } finally {
                    seen = pg.stop();
                }
                last = new Json.M().with("контур", c).with("секунд", Stand9.sec(dev.nanoTime() - t0)).with("итог run", v)
                        .with("с", w0).with("по", System.currentTimeMillis()).with("по журналу во время контура", seen);
                checkStop();
                if (!dev.foreground()) throw new Stopped("Стенд был свёрнут — этап начнётся заново: не сворачивайте стенд, пока он работает.");
                runs.add(last);
                last = null;
                save();
            }
            r.with("срок T, с", T).with("предел₂, с", limit2).with("запуски", runs)
                    .with("журнал", byContour(journal));
            Files.copy(journal.toPath(), new File(dir, "k5_journal5.jsonl").toPath(),
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } catch (Stopped | IOException | RuntimeException e) {
            // Следующий запуск К5 сотрёт папку исполнителя (Exec): журнал
            // прерванного этапа и счёт по нему остаются только здесь.
            r.with("запуски", runs);
            if (last != null) r.with("прерванный контур", last);
            keepPartial(r, journal, "k5");
            throw e;
        } finally {
            current = null;
            ex.close();
        }
    }

    /**
     * Что успел собрать прерванный этап: журнал запуска — копией в папку
     * итогов, счёт по нему — в этап (он уйдёт в «прерывания»). Последняя
     * строка журнала может быть оборвана, если исполнитель убит посреди
     * записи: такая строка не считается.
     */
    private void keepPartial(Json.M r, File journal, String prefix) {
        if (journal == null || !journal.isFile()) return;
        try {
            File copy = new File(dir, prefix + "_prervan_" + r.get("начат") + ".jsonl");
            Files.copy(journal.toPath(), copy.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            List<String> lines = Files.readAllLines(journal.toPath(), StandardCharsets.UTF_8);
            if (!lines.isEmpty()) {
                try {
                    Json.parse(lines.get(lines.size() - 1));
                } catch (RuntimeException torn) {
                    lines.remove(lines.size() - 1);
                }
            }
            r.with("журнал до прерывания", byContour(lines)).with("файл журнала", copy.getName());
        } catch (IOException | RuntimeException e) {
            r.with("журнал до прерывания", "не прочитан: " + e);
        }
    }

    /** Счёт по журналу (урок 31): вопросы, ответы, −7, длины ответов — по контурам. */
    @SuppressWarnings("unchecked")
    static Json.M byContour(File journal) throws IOException {
        return byContour(Files.readAllLines(journal.toPath(), StandardCharsets.UTF_8));
    }

    @SuppressWarnings("unchecked")
    static Json.M byContour(List<String> journalLines) {
        Map<String, int[]> n = new LinkedHashMap<>();
        Map<String, List<Integer>> lens = new LinkedHashMap<>();
        Map<String, java.util.TreeSet<Integer>> qlens = new LinkedHashMap<>();
        for (String line : journalLines) {
            if (line.isEmpty()) continue;
            Map<String, Object> e = (Map<String, Object>) Json.parse(line);
            if (!"действие".equals(e.get("kind"))) continue;
            List<Object> sig = (List<Object>) e.get("signature");
            String rule = sig == null || sig.isEmpty() ? "" : String.valueOf(sig.get(0));
            String subj = String.valueOf(e.get("subject"));
            int[] c = n.get(subj);
            if (c == null) {
                n.put(subj, c = new int[3]);
                lens.put(subj, new ArrayList<Integer>());
                qlens.put(subj, new java.util.TreeSet<Integer>());
            }
            if ("вопрос модели".equals(rule)) {
                c[0]++;
                if (e.get("length") instanceof Number) qlens.get(subj).add(((Number) e.get("length")).intValue());
            }
            if (!"ответ модели".equals(rule)) continue;
            c[1]++;
            if (e.get("code") != null) c[2]++;
            if (e.get("length") instanceof Number) lens.get(subj).add(((Number) e.get("length")).intValue());
        }
        Json.M out = new Json.M();
        for (Map.Entry<String, int[]> e : n.entrySet()) {
            List<Integer> l = lens.get(e.getKey());
            Collections.sort(l);
            out.with(e.getKey(), new Json.M().with("вопросов", e.getValue()[0]).with("ответов", e.getValue()[1])
                    .with("отказов −7", e.getValue()[2]).with("длины ответов, байт", l)
                    .with("длины вопросов, байт", new ArrayList<Integer>(qlens.get(e.getKey()))));
        }
        return out;
    }

    // ---------------------------------------------------------------- К6

    @SuppressWarnings("unchecked")
    private void stageLive(Json.M r) throws Stopped, IOException {
        Map<String, Object> k4 = stage("К4");
        int T = ((Number) k4.get("срок T, с")).intValue();
        Map<String, Object> sc = (Map<String, Object>) Json.parse(new String(dev.asset("k9/scenarios.json"), StandardCharsets.UTF_8));
        Map<String, Object> words = (Map<String, Object>) sc.get("words");
        int rounds = ((Number) sc.get("rounds")).intValue();
        byte[] agent = dev.asset("k9/agent9.wasm");
        List<Json.M> rows = new ArrayList<>();
        r.with("срок T, с", T).with("предел₂, с", k4.get("предел₂, с")).with("сценарии", rows);
        for (String name : prof.live) {
            between();
            ui.status("К6: живой агент — «" + name + "»");
            File root = new File(dev.workDir(), "k6");
            File keep = new File(dir, "k6_" + slug(name));
            Exec.delete(dir, keep);
            keep.mkdirs();
            Exec ex = startExec(root, T, "6");
            Json.M row = new Json.M().with("сценарий", name);
            try {
                Json.M cfg = seriesCfg();
                cfg.putAll((Map<String, Object>) sc.get("cfg"));
                cfg.put("budget", prof.budget);
                String wd = ex.newRun(cfg);
                int snap = 0;
                snapshot(new File(wd), keep, snap++);
                Map<String, Object> loads = new LinkedHashMap<>();
                for (int c = 1; c <= 2; c++) {
                    loads.put(String.valueOf(c), ex.load(c, agent));
                    snapshot(new File(wd), keep, snap++);
                }
                for (int c = 1; c <= 2; c++) ex.call("say", c, words.get(name));
                List<Json.M> runs = new ArrayList<>();
                for (int k = 0; k < rounds; k++) {
                    for (int c = 1; c <= 2; c++) {
                        checkStop();
                        Progress pg = new Progress(this, new File(wd, "journal5.jsonl"), "а" + c, "«" + name + "», контур " + c, -1, T);
                        long t0 = dev.nanoTime(), w0 = System.currentTimeMillis();
                        Object v;
                        Json.M seen;
                        pg.start();
                        try {
                            v = ex.run(c);
                        } catch (IOException e) {
                            v = "сбой: " + e;
                        } finally {
                            seen = pg.stop();
                        }
                        checkStop();
                        if (!dev.foreground()) throw new Stopped("Стенд был свёрнут — этап начнётся заново: не сворачивайте стенд, пока он работает.");
                        runs.add(new Json.M().with("контур", c).with("секунд", Stand9.sec(dev.nanoTime() - t0)).with("итог run", v)
                                .with("с", w0).with("по", System.currentTimeMillis()).with("по журналу во время контура", seen));
                        snapshot(new File(wd), keep, snap++);
                        save();
                    }
                }
                Object dump = ex.call("dump");
                Files.write(new File(keep, "dump.json").toPath(), (Json.str(dump) + "\n").getBytes(StandardCharsets.UTF_8));
                Files.write(new File(keep, "run.json").toPath(), (Json.str(new Json.M().with("cfg", cfg).with("loads", loads)
                        .with("runs", runs).with("снимков", snap)) + "\n").getBytes(StandardCharsets.UTF_8));
                row.with("запуски", runs).with("журнал", byContour(new File(wd, "journal5.jsonl")));
            } finally {
                current = null;
                ex.close();
            }
            rows.add(row);
            save();
        }
    }

    /**
     * Все файлы запуска после шага — журнал, получатели, «снаружи» и секрет:
     * наблюдатели на сервере проверяют по ним законы (как _snapshot в
     * run_shag5b.py, плюс файлы, которые смотрит замкнутость).
     */
    private static void snapshot(File wd, File keep, int k) throws IOException {
        File d = new File(keep, String.format(java.util.Locale.ROOT, "snap%02d", k));
        d.mkdirs();
        File[] fs = wd.listFiles();
        if (fs == null) return;
        for (File f : fs) {
            if (f.isFile()) {
                Files.copy(f.toPath(), new File(d, f.getName()).toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            }
        }
    }

    static String slug(String s) {
        return s.equals("честный") ? "chestny" : s.equals("к E") ? "k_E" : s.equals("наружу") ? "naruzhu"
                : s.equals("разрешение") ? "razreshenie" : s.equals("соседу") ? "sosedu" : Integer.toHexString(s.hashCode());
    }

    // ---------------------------------------------------------------- вердикты

    @SuppressWarnings("unchecked")
    private Json.M verdicts() {
        Json.M v = new Json.M();
        Map<String, Object> k1 = stage("К1");
        v.with("К1 потоки на ответ не влияют", k1 == null || k1.get("закончен") == null ? "не проверено"
                : Boolean.TRUE.equals(k1.get("ответ при всех парах один")) ? "да; выбрано " + k1.get("выбрано") : "нет; выбрано " + k1.get("выбрано"));
        for (String st : new String[]{"К2", "К3"}) {
            Map<String, Object> s = stage(st);
            v.with(st + (st.equals("К2") ? " холодный" : " прогретый"), s == null || s.get("закончен") == null ? "не проверено"
                    : Boolean.TRUE.equals(s.get("повтор — те же байты")) && Boolean.TRUE.equals(s.get("каждый Р до предела")) ? "да"
                    : "нет: повтор " + s.get("повтор — те же байты") + ", до предела " + s.get("каждый Р до предела"));
        }
        Map<String, Object> k4 = stage("К4");
        if (k4 != null && k4.get("закончен") != null) {
            v.with("К4 числа", "T = " + k4.get("срок T, с") + " с; предел₂ = " + k4.get("предел₂, с") + " с; обычный вопрос — "
                    + k4.get("обычный вопрос, с") + " с");
        }
        Map<String, Object> k5 = stage("К5");
        if (k5 == null || k5.get("закончен") == null) {
            v.with("К5 предел", "не проверено");
        } else {
            List<String> bad = new ArrayList<>();
            double limit2 = ((Number) k5.get("предел₂, с")).doubleValue();
            Map<String, Object> j = (Map<String, Object>) k5.get("журнал");
            int[] sizes = {4096, 256};
            for (int c = 1; c <= 2; c++) {
                Map<String, Object> jc = (Map<String, Object>) j.get("а" + c);
                int want = prof.budget / price(sizes[c - 1]);
                if (jc == null) {
                    bad.add("контур " + c + ": нет вопросов");
                    continue;
                }
                if (((Number) jc.get("отказов −7")).intValue() != 0) bad.add("контур " + c + ": −7 — " + jc.get("отказов −7"));
                if (((Number) jc.get("вопросов")).intValue() != want) bad.add("контур " + c + ": вопросов " + jc.get("вопросов") + ", ждали " + want);
                if (!onlyValue((List<Object>) jc.get("длины вопросов, байт"), sizes[c - 1]))
                    bad.add("контур " + c + ": вопросы по " + jc.get("длины вопросов, байт") + " байт, ждали " + sizes[c - 1]);
                for (Object x : (List<Object>) jc.get("длины ответов, байт")) {
                    if (((Number) x).intValue() < ANSWER_FULL) {
                        bad.add("контур " + c + ": ответ короче " + ANSWER_FULL + " байт");
                        break;
                    }
                }
            }
            for (Map<String, Object> rr : (List<Map<String, Object>>) k5.get("запуски")) {
                double s = ((Number) rr.get("секунд")).doubleValue();
                if (s > limit2) bad.add("контур " + rr.get("контур") + ": " + s + " с больше предела₂");
            }
            v.with("К5 предел", bad.isEmpty() ? "да" : "нет: " + String.join("; ", bad));
        }
        Map<String, Object> k6 = stage("К6");
        if (k6 == null || k6.get("закончен") == null) {
            v.with("К6 живой агент", "не проверено");
        } else {
            List<String> bad = new ArrayList<>();
            double limit2 = ((Number) k6.get("предел₂, с")).doubleValue();
            for (Map<String, Object> row : (List<Map<String, Object>>) k6.get("сценарии")) {
                Map<String, Double> per = new HashMap<>();
                for (Map<String, Object> rr : (List<Map<String, Object>>) row.get("запуски"))
                    per.merge(String.valueOf(rr.get("контур")), ((Number) rr.get("секунд")).doubleValue(), Double::sum);
                for (Map.Entry<String, Double> e : per.entrySet())
                    if (e.getValue() > limit2) bad.add(row.get("сценарий") + ", контур " + e.getKey() + ": больше предела₂");
                for (Object x : ((Map<String, Object>) row.get("журнал")).values())
                    if (((Number) ((Map<String, Object>) x).get("отказов −7")).intValue() != 0) bad.add(row.get("сценарий") + ": −7");
            }
            v.with("К6 живой агент", bad.isEmpty() ? "да по времени и −7; законы — разбором журналов на сервере" : "нет: " + String.join("; ", bad));
        }
        if (watchdogFired != null) v.with("сторож", watchdogFired);
        return v;
    }

    // ---------------------------------------------------------------- программы

    @SuppressWarnings("unchecked")
    private int[] pairOf() {
        List<Object> p = (List<Object>) stage("К1").get("выбрано");
        return new int[]{((Number) p.get(0)).intValue(), ((Number) p.get(1)).intValue()};
    }

    /**
     * Программа модели этапа К1 — по имени, а не по пути: путь к папке
     * библиотек приложения Android меняет при каждой установке APK, а
     * калибровка может продолжаться после обновления стенда.
     */
    private String serveOf() throws Stopped {
        Object want = stage("К1").get("программа модели");
        for (String[] s : dev.serves()) if (s[0].equals(want)) return s[1];
        throw new Stopped("Программы модели «" + want + "» больше нет — нажмите «Сначала».");
    }

    /** Первая программа модели, которая на заглушке отвечает «жив»; иначе — последняя. */
    private String chooseServe() throws Stopped {
        for (String[] s : dev.serves()) {
            try {
                Model m = new Model(this, s[1], null, "--stub");
                String a = new String(m.ask("ЭХО жив".getBytes(StandardCharsets.UTF_8), 50), StandardCharsets.UTF_8);
                m.finish();
                if ("жив".equals(a)) return s[1];
            } catch (Stopped e) {
                // Быстрая программа на процессоре без dotprod и i8mm падает на
                // первой незнакомой команде — это «не работает», а не сбой.
                checkStop();
                ui.log("Программа модели «" + s[0] + "» не работает: " + e.getMessage());
            }
        }
        throw new Stopped("Ни одна программа модели не запустилась.");
    }

    private String name(String path) {
        for (String[] s : dev.serves()) if (s[1].equals(path)) return s[0];
        return path;
    }

    private Json.M seriesCfg() throws IOException {
        @SuppressWarnings("unchecked")
        Map<String, Object> sc = (Map<String, Object>) Json.parse(new String(dev.asset("k9/scenarios.json"), StandardCharsets.UTF_8));
        Json.M cfg = new Json.M();
        @SuppressWarnings("unchecked")
        Map<String, Object> c = (Map<String, Object>) sc.get("cfg");
        cfg.putAll(c);
        cfg.put("budget", prof.budget);
        return cfg;
    }

    private Exec startExec(File root, int T, String kind) throws IOException, Stopped {
        Exec ex = dev.startModelExecutor(root, kind, serveOf(), T, pairOf());
        current = ex;
        return ex;
    }

    /**
     * Программа модели, запущенная стендом напрямую: строки JSON по двум
     * каналам (как Stand9.Direct). Время вопроса — по часам стены; время
     * процессора — из /proc.
     */
    private static final class Model {
        final Kalibr9 s;
        final Process p;
        final long pid;
        final OutputStream in;
        final BufferedReader outR;

        Model(Kalibr9 s, String exe, int[] pair) throws Stopped {
            this(s, exe, pair, "--gguf");
        }

        Model(Kalibr9 s, String exe, int[] pair, String mode) throws Stopped {
            this.s = s;
            List<String> cmd = new ArrayList<>(Arrays.asList(exe, mode));
            if (mode.equals("--gguf")) {
                cmd.add(s.dev.modelFile().getPath());
                if (pair != null) cmd.addAll(Arrays.asList("--threads", "" + pair[0], "--threads-batch", "" + pair[1]));
            }
            try {
                p = s.dev.startModel(cmd);
            } catch (IOException e) {
                throw new Stopped("Программа модели не запустилась: " + e.getMessage());
            }
            s.direct.add(p);
            pid = Stand9.pidOf(p);
            in = p.getOutputStream();
            outR = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8));
        }

        byte[] ask(byte[] q, int max) throws Stopped {
            try {
                String line = "{\"q\": \"" + Base64.getEncoder().encodeToString(q) + "\", \"max\": " + max + "}\n";
                in.write(line.getBytes(StandardCharsets.UTF_8));
                in.flush();
            } catch (IOException e) {
                throw new Stopped("Программа модели умерла: " + e.getMessage());
            }
            String a = readLine();
            if (a == null) throw new Stopped("Программа модели не ответила (умерла).");
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) Json.parse(a);
            return Base64.getDecoder().decode((String) m.get("a"));
        }

        Json.M timed(String name, byte[] q, int max) throws Stopped {
            Battery b0 = s.dev.battery();
            long cpu0 = Proc.cpuMs(pid), w0 = System.currentTimeMillis(), n0 = s.dev.nanoTime();
            byte[] a = ask(q, max);
            double sec = Stand9.sec(s.dev.nanoTime() - n0);
            long w1 = System.currentTimeMillis(), cpu1 = Proc.cpuMs(pid);
            Battery b1 = s.dev.battery();
            boolean fg = s.dev.foreground();
            Json.M r = new Json.M().with("вопрос", name).with("байт вопроса", q.length).with("секунд", sec)
                    .with("время процессора, с", cpu0 < 0 ? null : (cpu1 - cpu0) / 1000.0).with("байт ответа", a.length)
                    .with("sha256 ответа", sha256(a)).with("ответ (начало)", new String(Arrays.copyOf(a, Math.min(a.length, 60)), StandardCharsets.UTF_8))
                    .with("с", w0).with("по", w1).with("°C до", b0.tempC).with("°C после", b1.tempC);
            if (!fg) throw new Stopped("Стенд был свёрнут — этап начнётся заново: не сворачивайте стенд, пока он работает.");
            s.ui.progress(name + ": " + sec + " с, " + a.length + " байт");
            return r;
        }

        String readLine() throws Stopped {
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
            long end = System.currentTimeMillis() + DIRECT_TIMEOUT_S * 1000L;
            try {
                while (t.isAlive() && System.currentTimeMillis() < end) {
                    t.join(200);
                    if (s.stopReason != null || s.watchdogFired != null) {
                        p.destroyForcibly();
                        s.checkStop();
                    }
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            if (t.isAlive()) {
                p.destroyForcibly();
                throw new Stopped("Программа модели не ответила за " + DIRECT_TIMEOUT_S + " с.");
            }
            if (err[0] != null) throw new Stopped("Программа модели: " + err[0].getMessage());
            return got[0];
        }

        void finish() {
            try {
                in.close();
            } catch (IOException ignored) {
            }
            try {
                if (!p.waitFor(10, TimeUnit.SECONDS)) {
                    p.destroyForcibly();
                    p.waitFor(3, TimeUnit.SECONDS);
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                p.destroyForcibly();
            } finally {
                try { outR.close(); } catch (IOException ignored) {}
                s.direct.remove(p);
            }
        }
    }

    /**
     * Ход контура на экране, пока исполнитель его гоняет. Исполнитель
     * отвечает на run только в конце контура (в К5 — больше часа), и без
     * этого экран всё это время показывал бы то, что было до команды.
     *
     * Раз в 2 с дочитывает журнал запуска — исполнитель дописывает его
     * запись за записью, с fsync, и «вопрос модели» пишет до вопроса, а
     * «ответ модели» после — и считает вопросы и ответы одного контура.
     * Время вопроса — от того, как замечена запись вопроса, до того, как
     * замечена запись ответа: точность — период опроса; своих часов в
     * журнале нет (журнал не зависит от времени).
     *
     * Журнал только читается. Не удалось прочитать или разобрать — ход не
     * обновится, на прогон это не влияет. Свёрнутый стенд ход замечает и
     * показывает, но не останавливает: что делать со свёрнутым, решает этап.
     */
    static final class Progress {
        static final long POLL_MS = 2000;
        private final Kalibr9 s;
        private final File journal;
        private final String subject, label;
        private final int want, deadlineS;
        private final long t0 = System.currentTimeMillis();
        private long offset, askedAt = -1;
        private byte[] tail = new byte[0];
        private int asked, answered;
        private boolean hidden;
        private volatile boolean stopped;
        private final List<Long> seconds = new ArrayList<>();
        private Thread th;

        /** want — сколько ответов ждать (−1 — неизвестно); deadlineS — срок T. */
        Progress(Kalibr9 s, File journal, String subject, String label, int want, int deadlineS) {
            this.s = s;
            this.journal = journal;
            this.subject = subject;
            this.label = label;
            this.want = want;
            this.deadlineS = deadlineS;
        }

        void start() {
            th = new Thread(() -> {
                while (!Thread.currentThread().isInterrupted()) {
                    String line = poll();
                    if (stopped) return;
                    s.ui.progress(line);
                    try {
                        Thread.sleep(POLL_MS);
                    } catch (InterruptedException e) {
                        return;
                    }
                }
            }, "ход контура");
            th.setDaemon(true);
            th.start();
        }

        /** Опрос кончен; журнал дочитан до конца. Итог — в запись контура. */
        synchronized Json.M stop() {
            stopped = true;
            th.interrupt();
            poll();
            return new Json.M().with("вопросов", asked).with("ответов", answered)
                    .with("время вопросов, с (опрос раз в 2 с)", new ArrayList<>(seconds)).with("стенд сворачивали", hidden);
        }

        /** Дочитать журнал; вернуть строку хода. */
        synchronized String poll() {
            if (!s.dev.foreground()) hidden = true;
            try (java.io.RandomAccessFile f = new java.io.RandomAccessFile(journal, "r")) {
                long len = f.length();
                if (len > offset) {
                    byte[] buf = Arrays.copyOf(tail, tail.length + (int) (len - offset));
                    f.seek(offset);
                    f.readFully(buf, tail.length, (int) (len - offset));
                    offset = len;
                    int from = 0;
                    for (int i = 0; i < buf.length; i++) {
                        if (buf[i] != '\n') continue;
                        line(new String(buf, from, i - from, StandardCharsets.UTF_8));
                        from = i + 1;
                    }
                    tail = Arrays.copyOfRange(buf, from, buf.length);
                }
            } catch (IOException | RuntimeException e) {
                // журнала ещё нет — ход обновится при следующем опросе
            }
            return text();
        }

        @SuppressWarnings("unchecked")
        private void line(String text) {
            Map<String, Object> e;
            try {
                e = (Map<String, Object>) Json.parse(text);
            } catch (RuntimeException bad) {
                return;
            }
            if (!"действие".equals(e.get("kind")) || !subject.equals(e.get("subject"))) return;
            Object sig = e.get("signature");
            String rule = sig instanceof List && !((List<Object>) sig).isEmpty() ? String.valueOf(((List<Object>) sig).get(0)) : "";
            long now = System.currentTimeMillis();
            if ("вопрос модели".equals(rule)) {
                asked++;
                askedAt = now;
            } else if ("ответ модели".equals(rule)) {
                answered++;
                if (askedAt >= 0) seconds.add(Math.round((now - askedAt) / 1000.0));
                askedAt = -1;
            }
        }

        private String text() {
            long now = System.currentTimeMillis();
            StringBuilder b = new StringBuilder(label).append(": ответов ").append(answered);
            if (want > 0) b.append(" из ").append(want);
            b.append(" · идёт ").append((now - t0) / 60000).append(" мин");
            if (askedAt >= 0) {
                b.append(" · вопрос ").append(asked).append(" идёт ").append((now - askedAt) / 1000)
                        .append(" с, срок ").append(deadlineS).append(" с");
            }
            if (hidden) b.append(" · стенд сворачивали");
            return b.toString();
        }
    }

    static final long IDLE_MS = 30_000;

    /** 30 с покоя; границы — по часам стены, мощность по ним считает разбор. */
    private Json.M idle() throws Stopped {
        ui.status("Покой 30 с — мощность фона");
        long w0 = System.currentTimeMillis();
        while (System.currentTimeMillis() - w0 < IDLE_MS) {
            Stand9.sleep(500);
            checkStop();
        }
        if (!dev.foreground()) throw new Stopped("Стенд был свёрнут — этап начнётся заново: не сворачивайте стенд, пока он работает.");
        return new Json.M().with("с", w0).with("по", System.currentTimeMillis());
    }

    // ---------------------------------------------------------------- безопасность и батарея

    String startRefusal(Battery b) {
        if (dev.isPhone() && b.tempC == null) return "Не начинаю: нет показаний температуры батареи";
        if (b.levelPct != null && b.levelPct < START_MIN_LEVEL)
            return "Не начинаю: заряд " + b.levelPct + " %, нужно от " + START_MIN_LEVEL + " %. Зарядите и нажмите «Начать» — калибровка продолжится";
        if (b.tempC != null && b.tempC > START_MAX_TEMP)
            return "Не начинаю: батарея " + Stand9.c(b.tempC) + " °C, подождите до " + (int) START_MAX_TEMP + " °C";
        return null;
    }

    /** Между этапами: остыть при 40 °C до 37 °C, не дольше 10 минут. */
    private void between() throws Stopped {
        cool(PAUSE_TEMP);
    }

    /** Холодный телефон (К2): не теплее 37 °C, ждать не дольше 10 минут. */
    private void coolTo(double t) throws Stopped {
        cool(t + 0.05);
    }

    private void cool(double pauseAt) throws Stopped {
        checkStop();
        long t0 = System.currentTimeMillis();
        for (;;) {
            Battery b = dev.battery();
            if (b.tempC == null || b.tempC < pauseAt) break;
            if (System.currentTimeMillis() - t0 > MAX_PAUSE_MS) throw new Stopped("Закончил раньше: долго остывает");
            ui.status("Пауза: батарея " + Stand9.c(b.tempC) + " °C, жду остывания до " + (int) START_MAX_TEMP);
            while (b.tempC != null && b.tempC > START_MAX_TEMP && System.currentTimeMillis() - t0 <= MAX_PAUSE_MS) {
                Stand9.sleep(5000);
                checkStop();
                b = dev.battery();
            }
        }
    }

    private void checkStop() throws Stopped {
        if (watchdogFired != null) throw new Stopped("Прерван сторожем: " + watchdogFired);
        if (stopReason != null) throw new Stopped(stopReason);
    }

    /**
     * Раз в 0,5 с: строка в battery.csv (часы стены, ток как есть, напряжение,
     * заряд в процентах и по счётчику, температура, зарядка) и проверка
     * сторожа — 45 °C или 25 % заряда убивают исполнитель и модель.
     */
    private void watchdog() {
        File f = new File(dir, "battery.csv");
        boolean head = !f.isFile();
        try (PrintStream w = new PrintStream(new FileOutputStream(f, true), true, "UTF-8")) {
            if (head) w.println("ms,i_raw,v_mv,level,q_uah,temp_c,plugged");
            while (!Thread.currentThread().isInterrupted()) {
                Battery b = dev.battery();
                w.println(System.currentTimeMillis() + "," + n(b.currentRaw) + "," + n(b.voltageMv) + "," + n(b.levelPct) + ","
                        + n(b.chargeUah) + "," + n(b.tempC) + "," + n(b.plugged));
                String why = null;
                if (b.tempC != null && b.tempC >= KILL_TEMP) why = "батарея " + Stand9.c(b.tempC) + " °C";
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
        } catch (IOException e) {
            ui.log("Журнал батареи не пишется: " + e.getMessage());
        }
    }

    private static String n(Object o) {
        return o == null ? "" : o instanceof Boolean ? ((Boolean) o ? "1" : "0") : String.valueOf(o);
    }

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

    @SuppressWarnings("unchecked")
    private Map<String, byte[]> questions() throws IOException {
        Map<String, Object> m = (Map<String, Object>) Json.parse(new String(dev.asset("k9/questions.json"), StandardCharsets.UTF_8));
        Map<String, byte[]> out = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : m.entrySet()) out.put(e.getKey(), Base64.getDecoder().decode((String) e.getValue()));
        return out;
    }

    private void save() {
        try {
            dir.mkdirs();
            File tmp = new File(dir, "kalibr9.json.tmp");
            Files.write(tmp.toPath(), (Json.str(res) + "\n").getBytes(StandardCharsets.UTF_8));
            if (!tmp.renameTo(resultFile())) throw new IOException("не переименовать " + tmp);
        } catch (IOException e) {
            ui.log("Не записал файл итогов: " + e.getMessage());
        }
    }

    static String sha256(byte[] b) {
        try {
            StringBuilder s = new StringBuilder();
            for (byte x : MessageDigest.getInstance("SHA-256").digest(b)) s.append(String.format("%02x", x));
            return s.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Список — ровно одно число v (после разбора JSON числа — Long, до — Integer). */
    static boolean onlyValue(List<Object> xs, long v) {
        return xs != null && xs.size() == 1 && xs.get(0) instanceof Number && ((Number) xs.get(0)).longValue() == v;
    }

    static double round(double x) {
        return Math.round(x * 10) / 10.0;
    }
}
