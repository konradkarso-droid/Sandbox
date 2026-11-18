package stend.android;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.BatteryManager;
import android.os.Build;

import stend.core.Battery;
import stend.core.Device;
import stend.core.Json;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;

/**
 * Телефон для ведущего: батарея из BatteryManager и состояния батареи,
 * исполнитель из папки библиотек приложения, нагрузки из assets.
 *
 * Исполнитель лежит в APK как lib/arm64-v8a/libexecutor7.so: только из
 * папки библиотек Android разрешает запускать свои программы. Это обычная
 * программа, а не библиотека; имя — требование упаковки.
 */
final class AndroidDevice implements Device {
    private final Context ctx;
    volatile boolean foreground;

    AndroidDevice(Context ctx) {
        this.ctx = ctx.getApplicationContext();
    }

    public long nanoTime() {
        return System.nanoTime();
    }

    public Battery battery() {
        BatteryManager bm = (BatteryManager) ctx.getSystemService(Context.BATTERY_SERVICE);
        Long cur = prop(bm, BatteryManager.BATTERY_PROPERTY_CURRENT_NOW);
        Long q = prop(bm, BatteryManager.BATTERY_PROPERTY_CHARGE_COUNTER);
        Intent i = ctx.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (i == null) return new Battery(cur, q, null, null, null, null);
        int t = i.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Integer.MIN_VALUE);
        int v = i.getIntExtra(BatteryManager.EXTRA_VOLTAGE, Integer.MIN_VALUE);
        int lvl = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
        int scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, -1);
        int plugged = i.getIntExtra(BatteryManager.EXTRA_PLUGGED, -1);
        return new Battery(cur, q, v == Integer.MIN_VALUE ? null : v, t == Integer.MIN_VALUE ? null : t / 10.0,
                lvl < 0 || scale <= 0 ? null : lvl * 100 / scale, plugged < 0 ? null : plugged != 0);
    }

    /** Свойство батареи; null — система его не даёт (MIN_VALUE или 0). */
    private static Long prop(BatteryManager bm, int id) {
        if (bm == null) return null;
        long x = bm.getLongProperty(id);
        return x == Long.MIN_VALUE || x == 0 ? null : x;
    }

    public boolean foreground() {
        return foreground;
    }

    public boolean isPhone() {
        return true;
    }

    public String exe() {
        return ctx.getApplicationInfo().nativeLibraryDir + "/libexecutor7.so";
    }

    public File workDir() {
        return new File(ctx.getFilesDir(), "stend8");
    }

    public byte[] asset(String name) throws IOException {
        try (InputStream in = ctx.getAssets().open("w8/" + name)) {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
            return b.toByteArray();
        }
    }

    public Json.M describe() {
        return new Json.M().with("where", "телефон").with("manufacturer", Build.MANUFACTURER).with("model", Build.MODEL)
                .with("device", Build.DEVICE).with("android", Build.VERSION.RELEASE).with("sdk", Build.VERSION.SDK_INT)
                .with("abi", Build.SUPPORTED_ABIS.length > 0 ? Build.SUPPORTED_ABIS[0] : null)
                .with("incremental", Build.VERSION.INCREMENTAL)
                .with("cpus", Runtime.getRuntime().availableProcessors());
    }
}
