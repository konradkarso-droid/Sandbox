package stend.core;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;

/**
 * Свод замеров одного размера нагрузки в одном блоке: число, среднее,
 * разброс, крайние значения времени; топливо и бюджет (они от запуска к
 * запуску не должны меняться — крайние значения это показывают).
 */
public final class Agg {
    long count;
    double sum, sumSq;
    long min = Long.MAX_VALUE, max = Long.MIN_VALUE;
    long fuelMin = Long.MAX_VALUE, fuelMax = Long.MIN_VALUE;
    long spentMin = Long.MAX_VALUE, spentMax = Long.MIN_VALUE;
    final Map<String, Integer> outcomes = new TreeMap<>();

    public void add(long ns, long fuel, long spent, String outcome) {
        count++;
        sum += ns;
        sumSq += (double) ns * ns;
        min = Math.min(min, ns);
        max = Math.max(max, ns);
        fuelMin = Math.min(fuelMin, fuel);
        fuelMax = Math.max(fuelMax, fuel);
        spentMin = Math.min(spentMin, spent);
        spentMax = Math.max(spentMax, spent);
        outcomes.merge(outcome, 1, Integer::sum);
    }

    public double mean() {
        return count == 0 ? Double.NaN : sum / count;
    }

    /** Выборочное стандартное отклонение; при одном замере — 0. */
    public double sd() {
        if (count < 2) return 0;
        double m = mean();
        return Math.sqrt(Math.max(0, (sumSq - count * m * m) / (count - 1)));
    }

    public Json.M toJson() {
        return new Json.M().with("count", count).with("mean_ns", mean()).with("sd_ns", sd())
                .with("min_ns", count == 0 ? null : min).with("max_ns", count == 0 ? null : max)
                .with("fuel_min", count == 0 ? null : fuelMin).with("fuel_max", count == 0 ? null : fuelMax)
                .with("spent_min", count == 0 ? null : spentMin).with("spent_max", count == 0 ? null : spentMax)
                .with("outcomes", new LinkedHashMap<>(outcomes));
    }
}
