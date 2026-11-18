package stend.test;

import stend.core.Agg;
import stend.core.Battery;
import stend.core.Json;
import stend.core.Plan;
import stend.core.Safety;
import stend.core.Safety.Act;
import stend.core.Stand;
import stend.server.ServerMain;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

/**
 * Проверки ведущего на обычной JVM, без библиотек. Запуск:
 * java -cp КЛАССЫ stend.test.Tests [--exe ИСПОЛНИТЕЛЬ --assets ПАПКА]
 * С исполнителем — ещё и короткий прогон ведущего целиком.
 */
public final class Tests {
    static int passed, failed;

    static void check(boolean ok, String what) {
        if (ok) passed++;
        else {
            failed++;
            System.out.println("НЕ ПРОШЛО: " + what);
        }
    }

    static Battery bat(Double t, Integer lvl, Boolean plugged) {
        return new Battery(-300000L, 3000000L, 3900, t, lvl, plugged);
    }

    public static void main(String[] args) throws Exception {
        json();
        safety();
        plan();
        agg();
        if (args.length >= 4) whole(args[1], args[3]);
        System.out.println("прошло " + passed + ", не прошло " + failed);
        if (failed > 0) System.exit(1);
    }

    @SuppressWarnings("unchecked")
    static void json() {
        Json.M m = new Json.M().with("а", "строка \"с\" кавычкой\nи переводом").with("n", 12L).with("x", 1.5)
                .with("l", Arrays.asList(1L, null, true)).with("пусто", new Json.M());
        Object back = Json.parse(Json.str(m));
        check(back.equals(m), "JSON: запись и разбор туда-обратно");
        Map<String, Object> b = (Map<String, Object>) Json.parse(Json.str(new Json.M().with("b", new byte[]{1, 2, 3})));
        check(((Map<String, Object>) b.get("b")).get("b64").equals("AQID"), "JSON: байты как {\"b64\": …}");
        check(Json.parse("{\"s\":\"\\u0436\"}").equals(new Json.M().with("s", "ж")), "JSON: \\u-последовательность");
        check(Json.str(Double.NaN).equals("null"), "JSON: NaN пишется как null");
    }

    static void safety() {
        // Молчание: при нормальных показаниях пауз и остановок нет.
        check(Safety.startRefusal(bat(30.0, 80, false), true) == null, "старт: нормальные показания — можно");
        check(Safety.during(bat(36.0, 70, false), true, true, false, false, 0).act == Act.GO,
                "ход: нормальные показания — идём");
        check(Safety.during(bat(39.9, 70, false), true, true, false, false, 0).act == Act.GO,
                "ход: 39,9 °C без паузы — идём");
        check(Safety.during(Battery.NONE, false, true, false, false, 0).act == Act.GO,
                "сервер: без батареи — идём");
        check(Safety.startRefusal(Battery.NONE, false) == null, "сервер: без батареи — можно начинать");
        // Срабатывание.
        check(Safety.startRefusal(bat(30.0, 80, true), true).equals("Не меряю: на зарядке"), "старт: на зарядке");
        check(Safety.startRefusal(bat(30.0, 39, false), true).startsWith("Не начинаю: заряд 39"), "старт: мало заряда");
        check(Safety.startRefusal(bat(37.5, 80, false), true).startsWith("Не начинаю: батарея 37,5"), "старт: тёплая");
        check(Safety.startRefusal(bat(null, 80, false), true).contains("нет показаний температуры"),
                "старт: телефон без температуры");
        Safety.Verdict v = Safety.during(bat(40.0, 70, false), true, true, false, false, 0);
        check(v.act == Act.PAUSE_HEAT && v.why.contains("40,0"), "ход: 40 °C — пауза по нагреву");
        check(Safety.during(bat(38.0, 70, false), true, true, false, true, 0).act == Act.PAUSE_HEAT,
                "ход: в паузе 38 °C — ждём дальше");
        check(Safety.during(bat(37.0, 70, false), true, true, false, true, 0).act == Act.GO,
                "ход: в паузе остыла до 37 °C — идём");
        check(Safety.during(bat(36.0, 24, false), true, true, false, false, 0).act == Act.STOP, "ход: заряд 24 % — конец");
        check(Safety.during(bat(36.0, 70, false), true, true, false, false, Safety.MAX_HEAT_PAUSE_MS + 1).act == Act.STOP,
                "ход: долго остывает — конец");
        check(Safety.during(bat(36.0, 70, false), true, true, true, false, 0).act == Act.STOP, "ход: кнопка — конец");
        check(Safety.during(bat(36.0, 70, true), true, true, false, false, 0).act == Act.PAUSE, "ход: на зарядке — пауза");
        check(Safety.during(bat(36.0, 70, false), true, false, false, false, 0).act == Act.PAUSE, "ход: свёрнуто — пауза");
        // Порядок: безопасность выше полноты — нагрев раньше «свёрнуто», остановка раньше нагрева.
        check(Safety.during(bat(41.0, 70, false), true, false, false, false, 0).act == Act.PAUSE_HEAT,
                "порядок: нагрев раньше «свёрнуто»");
        check(Safety.during(bat(41.0, 20, false), true, true, false, false, 0).act == Act.STOP,
                "порядок: конец по заряду раньше нагрева");
    }

    static void plan() {
        List<String> k = Arrays.asList("счёт", "память", "ворота", "фиксация", "работа узла");
        Plan p = Plan.of("телефон", k);
        check(p.plannedMs() == 60_000 + 4 * 5 * 50_000L, "план телефона: 60 с фона и 4 круга по 5 × 50 с");
        check(p.steps.get(0).part.equals("фон") && p.steps.get(1).round == 1 && p.steps.get(1).kind.equals("счёт"),
                "план телефона: сначала фон, потом круг 1 со «счёта»");
        check(p.steps.get(p.steps.size() - 1).part.equals("каналы"), "план: каналы в конце");
        Plan s = Plan.of("сервер", k);
        check(s.steps.stream().noneMatch(x -> x.part.equals("покой") || x.part.equals("фон")), "план сервера: без покоя и фона");
    }

    static void agg() {
        Agg a = new Agg();
        a.add(10, 100, 0, "завершён");
        a.add(20, 100, 0, "завершён");
        a.add(30, 100, 0, "завершён");
        Map<String, Object> j = a.toJson();
        check(Math.abs((Double) j.get("mean_ns") - 20) < 1e-9 && Math.abs((Double) j.get("sd_ns") - 10) < 1e-9,
                "свод: среднее 20, разброс 10");
        check(j.get("fuel_min").equals(100L) && j.get("fuel_max").equals(100L), "свод: топливо постоянно");
    }

    @SuppressWarnings("unchecked")
    static void whole(String exe, String assets) throws Exception {
        File dir = Files.createTempDirectory("stend8_test").toFile();
        File out = new File(dir, "замер.txt");
        final String[] fin = new String[1];
        Stand s = new Stand(new ServerMain.ServerDevice(exe, new File(assets), new File(dir, "work")),
                new Stand.Listener() {
                    public void status(String x) { }
                    public void log(String x) { }
                    public void progress(String x) { }
                    public void finished(String why, File r) { fin[0] = why; }
                }, "короткий", out);
        s.run();
        check(fin[0] != null && fin[0].startsWith("Готово"), "прогон целиком: готово (" + fin[0] + ")");
        int head = 0, zam = 0, ch = 0, end = 0, bad = 0;
        for (String l : Files.readAllLines(out.toPath(), StandardCharsets.UTF_8)) {
            Map<String, Object> m = (Map<String, Object>) Json.parse(l);
            String t = (String) m.get("type");
            if (t.equals("заголовок")) head++;
            if (t.equals("замер")) {
                zam++;
                Map<String, Object> o = (Map<String, Object>) m.get("outcomes");
                long cnt = (Long) m.get("count");
                if (cnt > 0 && !(o.size() == 1 && o.containsKey("завершён"))) bad++;
            }
            if (t.equals("канал")) ch++;
            if (t.equals("конец") && Boolean.TRUE.equals(m.get("complete"))) end++;
        }
        check(head == 1 && end == 1, "прогон целиком: заголовок и конец");
        check(zam > 0 && bad == 0, "прогон целиком: замеры есть, все нагрузки завершились (" + bad + " с иным исходом)");
        check(ch == 4 * 6, "прогон целиком: по 6 символов в 4 каналах (" + ch + ")");
    }
}
