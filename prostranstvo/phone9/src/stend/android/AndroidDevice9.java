package stend.android;

import android.content.Context;
import android.os.Build;

import stend.core.Battery;
import stend.core.Exec;
import stend.core.Json;
import stend9.core.Device9;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.util.Arrays;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

/**
 * Телефон для малого стенда. Батарея и сведения — как у стенда шага 8
 * (AndroidDevice). Исполнитель запускается из папки библиотек. Загрузчик
 * GGUF работает в изолированной службе Android и получает дескриптор
 * проверенной копии весов, открытый только для чтения.
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

    @Override public Process startModel(List<String> command) throws IOException {
        String exe = command.get(0);
        int variant;
        if (exe.equals(lib("libserve9a.so"))) variant = 0;
        else if (exe.equals(lib("libserve9b.so"))) variant = 1;
        else throw new IOException("неизвестный модуль модели");
        switch (command.get(1)) {
            case "--stub": return new IsolatedModel(ctx, variant, 0, null, 2, 2);
            case "--probe-net": return new IsolatedModel(ctx, variant, 1, null, 2, 2);
            case "--gguf":
                if (command.size() < 3 || command.size() % 2 == 0 || !command.get(2).equals(modelFile().getPath())) {
                    throw new IOException("модель должна быть проверенным файлом стенда");
                }
                int threads = 2, batch = Runtime.getRuntime().availableProcessors();
                for (int i = 3; i + 1 < command.size(); i += 2) {
                    if (command.get(i).equals("--threads")) threads = Integer.parseInt(command.get(i + 1));
                    else if (command.get(i).equals("--threads-batch")) batch = Integer.parseInt(command.get(i + 1));
                    else throw new IOException("неизвестный параметр модели");
                }
                return new IsolatedModel(ctx, variant, 2, modelFile(), threads, batch);
            default: throw new IOException("неизвестный режим модели");
        }
    }

    @Override public Exec startModelExecutor(File root, String kind, String serve, int timeout, int[] threads) throws IOException {
        List<String> model = new ArrayList<>(Arrays.asList(serve, "--gguf", modelFile().getPath()));
        if (threads != null) model.addAll(Arrays.asList("--threads", "" + threads[0], "--threads-batch", "" + threads[1]));
        Process first = startModel(model);
        ModelExchange exchange;
        try {
            exchange = new ModelExchange(first, startModel(model), timeout);
        } catch (IOException | RuntimeException e) {
            first.destroyForcibly();
            throw e;
        }
        try {
            return new Exec(exe(), root, kind, Arrays.asList("--model=android", "--model-timeout=" + timeout,
                    "--model-net=android-service"), extraEnv(), exchange);
        } catch (IOException | RuntimeException e) {
            exchange.close();
            throw e;
        }
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
