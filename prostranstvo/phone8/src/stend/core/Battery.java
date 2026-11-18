package stend.core;

/**
 * Показания батареи в один момент. null — показание недоступно: так
 * «нет прибора» отличается от «ноль». На сервере недоступно всё.
 *
 * Ток — как отдаёт система, без пересчёта: у части телефонов это мкА, у
 * части мА, знак при разряде тоже бывает любым. Единицы определяет разбор
 * (по величине фона), а не стенд.
 */
public final class Battery {
    public final Long currentRaw;   // BATTERY_PROPERTY_CURRENT_NOW
    public final Long chargeUah;    // BATTERY_PROPERTY_CHARGE_COUNTER, мкА·ч
    public final Integer voltageMv;
    public final Double tempC;
    public final Integer levelPct;
    public final Boolean plugged;

    public static final Battery NONE = new Battery(null, null, null, null, null, null);

    public Battery(Long currentRaw, Long chargeUah, Integer voltageMv, Double tempC, Integer levelPct,
                   Boolean plugged) {
        this.currentRaw = currentRaw;
        this.chargeUah = chargeUah;
        this.voltageMv = voltageMv;
        this.tempC = tempC;
        this.levelPct = levelPct;
        this.plugged = plugged;
    }

    public Json.M toJson() {
        return new Json.M().with("i_raw", currentRaw).with("q_uah", chargeUah).with("v_mv", voltageMv)
                .with("temp_c", tempC).with("level", levelPct).with("plugged", plugged);
    }
}
