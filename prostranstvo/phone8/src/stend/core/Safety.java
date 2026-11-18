package stend.core;

import java.util.Locale;

/**
 * Решения безопасности устройства — чистая логика, без Android.
 *
 * Числа объявлены в спецификации шага 8 (§7) до прогона и не подбирались
 * по результату. Порядок при ничьей: безопасность устройства выше
 * полноты замера, поэтому остановка проверяется раньше паузы, а нагрев —
 * раньше прочих пауз.
 *
 * Чего не умеет: судит о нагреве только по датчику батареи — датчики
 * процессора приложению недоступны. Частоты и термозащиту телефона не
 * трогает и не видит.
 */
public final class Safety {
    private Safety() {
    }

    public static final double START_MAX_TEMP = 37.0;
    public static final double PAUSE_TEMP = 40.0;
    public static final double RESUME_TEMP = 37.0;
    public static final int START_MIN_LEVEL = 40;
    public static final int STOP_LEVEL = 25;
    public static final long MAX_HEAT_PAUSE_MS = 10 * 60 * 1000L;

    /**
     * Что делать. Не enum: d8 из build-tools 34 падает на классе
     * перечисления, собранном javac 21, — обычный класс обходит это.
     */
    public static final class Act {
        public static final Act GO = new Act("GO"), PAUSE = new Act("PAUSE"), PAUSE_HEAT = new Act("PAUSE_HEAT"),
                STOP = new Act("STOP");
        private final String name;

        private Act(String name) {
            this.name = name;
        }

        @Override
        public String toString() {
            return name;
        }
    }

    public static final class Verdict {
        public final Act act;
        public final String why;

        Verdict(Act act, String why) {
            this.act = act;
            this.why = why;
        }

        @Override
        public String toString() {
            return act + (why == null ? "" : ": " + why);
        }
    }

    static final Verdict GO = new Verdict(Act.GO, null);

    private static String c(double t) {
        return String.format(new Locale("ru"), "%.1f", t);
    }

    /** Почему нельзя начинать; null — можно. */
    public static String startRefusal(Battery b, boolean phone) {
        if (phone && b.tempC == null) return "Не начинаю: нет показаний температуры батареи";
        if (Boolean.TRUE.equals(b.plugged)) return "Не меряю: на зарядке";
        if (b.levelPct != null && b.levelPct < START_MIN_LEVEL)
            return "Не начинаю: заряд " + b.levelPct + " %, нужно от " + START_MIN_LEVEL + " %";
        if (b.tempC != null && b.tempC > START_MAX_TEMP)
            return "Не начинаю: батарея " + c(b.tempC) + " °C, жду до " + (int) START_MAX_TEMP;
        return null;
    }

    /**
     * Решение в ходе прогона.
     *
     * @param heatPaused   сейчас идёт пауза по нагреву (тогда продолжаем только
     *                     после остывания до RESUME_TEMP, а не сразу ниже PAUSE_TEMP)
     * @param heatPausedMs сколько всего длились паузы по нагреву
     */
    public static Verdict during(Battery b, boolean phone, boolean foreground, boolean stopPressed,
                                 boolean heatPaused, long heatPausedMs) {
        if (stopPressed) return new Verdict(Act.STOP, "Остановлено вами");
        if (b.levelPct != null && b.levelPct < STOP_LEVEL)
            return new Verdict(Act.STOP, "Закончил раньше: заряд " + b.levelPct + " %");
        if (heatPausedMs > MAX_HEAT_PAUSE_MS) return new Verdict(Act.STOP, "Закончил раньше: долго остывает");
        if (phone && b.tempC == null)
            return new Verdict(Act.PAUSE_HEAT, "Пауза: нет показаний температуры батареи");
        if (b.tempC != null && (b.tempC >= PAUSE_TEMP || (heatPaused && b.tempC > RESUME_TEMP)))
            return new Verdict(Act.PAUSE_HEAT, "Пауза: батарея " + c(b.tempC) + " °C, жду остывания до "
                    + (int) RESUME_TEMP);
        if (Boolean.TRUE.equals(b.plugged)) return new Verdict(Act.PAUSE, "Не меряю: на зарядке");
        if (!foreground) return new Verdict(Act.PAUSE, "Пауза: приложение свёрнуто");
        return GO;
    }
}
