package stend.android;

import android.content.Context;
import android.os.Build;

import stend.core.Battery;
import stend.core.Json;
import stend9.core.Device9;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Телефон для малого стенда. Батарея и сведения — как у стенда шага 8
 * (AndroidDevice). Исполнитель и обе программы модели лежат в APK как
 * lib/arm64-v8a/lib*.so: Android запускает свои программы только из папки
 * библиотек приложения (почему — AndroidDevice). Модель — копия файла,
 * выбранного владельцем, в папке стенда; путь без пробелов: он идёт внутри
 * режима модели, а режим делится по пробелам (контракт §17).
 */
final class AndroidDevice9 implements Device9 {
    private final Context ctx;
    final AndroidDevice base;

    AndroidDevice9(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        this.base = new AndroidDevice(ctx);
    }

    public long nanoTime() {
        return System.nanoTime();
    }

    public Battery battery() {
        return base.battery();
    }

    public boolean foreground() {
        return base.foreground;
    }

    public boolean isPhone() {
        return true;
    }

    private String lib(String name) {
        return ctx.getApplicationInfo().nativeLibraryDir + "/" + name;
    }

    public String exe() {
        return lib("libexecutor7.so");
    }

    public List<String[]> serves() {
        return Arrays.asList(new String[]{"быстрая", lib("libserve9b.so")}, new String[]{"переносимая", lib("libserve9a.so")});
    }

    public File modelFile() {
        return new File(new File(ctx.getFilesDir(), "model"), "verified.gguf");
    }

    public Map<String, String> extraEnv() {
        return Collections.emptyMap();
    }

    public File workDir() {
        File d = new File(ctx.getFilesDir(), "stend9");
        //noinspection ResultOfMethodCallIgnored
        d.mkdirs();
        return d;
    }

    public byte[] asset(String name) throws IOException {
        try (InputStream in = ctx.getAssets().open(name)) {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
            return b.toByteArray();
        }
    }

    public Json.M describe() {
        Json.M d = base.describe();
        d.with("hardware", Build.HARDWARE);
        if (Build.VERSION.SDK_INT >= 31) d.with("soc", Build.SOC_MANUFACTURER + " " + Build.SOC_MODEL);
        return d;
    }
}
