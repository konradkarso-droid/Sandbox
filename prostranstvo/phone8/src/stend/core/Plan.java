package stend.core;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * План прогона: фон, круги «работа — покой» по всем видам нагрузки,
 * каналы. Длительности — объявленные числа спецификации шага 8 (§4).
 *
 * Покой между блоками нужен ради энергии: экран и система тратят своё
 * всё время, и цену работы видно только как разность с соседним покоем.
 * На сервере энергии нет, поэтому там фона и покоя нет.
 */
public final class Plan {
    public static final class Step {
        public final String part;   // «фон», «работа», «покой», «каналы»
        public final String kind;   // вид нагрузки для «работы», иначе null
        public final int round;     // номер круга с 1; 0 — вне кругов
        public final long ms;       // активное время шага; для каналов 0 — все символы

        Step(String part, String kind, int round, long ms) {
            this.part = part;
            this.kind = kind;
            this.round = round;
            this.ms = ms;
        }
    }

    public final String name;
    public final List<Step> steps;
    /** Сколько символов каждого канала прогнать; -1 — все. */
    public final int channelLimit;

    private Plan(String name, List<Step> steps, int channelLimit) {
        this.name = name;
        this.steps = Collections.unmodifiableList(steps);
        this.channelLimit = channelLimit;
    }

    public static Plan of(String name, List<String> kinds) {
        switch (name) {
            case "телефон": return build(name, kinds, 60_000, 4, 30_000, 20_000, -1);
            case "сервер": return build(name, kinds, 0, 4, 30_000, 0, -1);
            case "короткий": return build(name, kinds, 500, 1, 700, 300, 6);
            // Проверка предела шага 8б: семь стратегий «потратить побольше» по
            // 3 круга; каналов нет. Объявленные числа, не подобранные.
            case "предел": return build(name, kinds, 60_000, 3, 20_000, 15_000, 0);
            case "предел-короткий": return build(name, kinds, 500, 1, 700, 300, 0);
            case "предел 8в": return build(name, kinds, 60_000, 2, 10_000, 10_000, 0);
            case "предел 8в-короткий": return build(name, kinds, 500, 1, 700, 300, 0);
            // Энергия: три круга по 20 с, как в шаге 8б — устойчивых кругов два.
            case "энергия 8в": return build(name, kinds, 60_000, 3, 20_000, 15_000, 0);
            case "энергия 8в-короткий": return build(name, kinds, 500, 3, 700, 300, 0);
            default: throw new IllegalArgumentException("неизвестный план " + name);
        }
    }

    static Plan build(String name, List<String> kinds, long background, int rounds, long work, long rest,
                      int channelLimit) {
        List<Step> s = new ArrayList<>();
        if (background > 0) s.add(new Step("фон", null, 0, background));
        for (int r = 1; r <= rounds; r++) {
            for (String k : kinds) {
                s.add(new Step("работа", k, r, work));
                if (rest > 0) s.add(new Step("покой", null, r, rest));
            }
        }
        s.add(new Step("каналы", null, 0, 0));
        return new Plan(name, s, channelLimit);
    }

    /** Сумма времени шагов с заданной длительностью, без каналов и пауз. */
    public long plannedMs() {
        long t = 0;
        for (Step x : steps) t += x.ms;
        return t;
    }
}
