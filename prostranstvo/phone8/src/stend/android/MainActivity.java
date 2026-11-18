package stend.android;

import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import stend.core.Stand;

import java.io.File;
import java.io.OutputStream;
import java.nio.file.Files;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Экран стенда. Для левши: главные кнопки внизу слева («Начать» /
 * «Остановить», «Поделиться»); вверху крупно — что идёт сейчас или почему
 * стоит. Опасных кнопок нет.
 *
 * Файл замеров пишется в папку приложения по ходу прогона; «Поделиться»
 * копирует последний файл в «Загрузки» и открывает «Отправить». Так файл
 * не теряется, даже если прогон оборвался.
 */
public final class MainActivity extends Activity implements Stand.Listener {
    /** Какой план гоняет этот APK: «телефон» — калибровка шага 8, «предел» — проверка предела шага 8б,
     *  «предел 8в» — проверка предела с правилом «попытка платит»,
     *  «энергия 8в» — тот же предел по энергии: меньше стратегий, круги длиннее. */
    private static final String PLAN = "энергия 8в";
    private static final String PREFIX = PLAN.equals("энергия 8в") ? "shag8ve_" : PLAN.equals("предел 8в") ? "shag8v_"
            : PLAN.equals("предел") ? "shag8b_" : "shag8_";

    private AndroidDevice dev;
    private TextView status, progress, log;
    private ScrollView logScroll;
    private Button start, share;
    private volatile Stand stand;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        dev = new AndroidDevice(this);
        int pad = dp(16);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);

        status = new TextView(this);
        status.setTextSize(22);
        status.setTypeface(Typeface.DEFAULT_BOLD);
        status.setText("Готов. Снимите телефон с зарядки и нажмите «Начать».");
        root.addView(status);

        progress = new TextView(this);
        progress.setTextSize(16);
        progress.setPadding(0, dp(8), 0, dp(8));
        root.addView(progress);

        logScroll = new ScrollView(this);
        log = new TextView(this);
        log.setTextSize(14);
        log.setTextColor(Color.DKGRAY);
        logScroll.addView(log);
        root.addView(logScroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        start = new Button(this);
        start.setText("Начать");
        start.setTextSize(20);
        start.setOnClickListener(v -> onStartStop());
        share = new Button(this);
        share.setText("Поделиться");
        share.setTextSize(20);
        share.setOnClickListener(v -> onShare());
        share.setEnabled(lastResult() != null);
        bar.addView(start);
        bar.addView(share);
        root.addView(bar);

        setContentView(root);
        if (lastResult() != null) say("Есть файл прошлого замера: " + lastResult().getName());
    }

    @Override
    protected void onResume() {
        super.onResume();
        dev.foreground = true;
    }

    @Override
    protected void onPause() {
        dev.foreground = false;
        super.onPause();
    }

    private int dp(int x) {
        return (int) (x * getResources().getDisplayMetrics().density + 0.5f);
    }

    private File resultsDir() {
        return new File(dev.workDir(), "замеры");
    }

    private File lastResult() {
        File[] fs = resultsDir().listFiles((d, n) -> n.startsWith(PREFIX) && n.endsWith(".txt"));
        if (fs == null || fs.length == 0) return null;
        File best = fs[0];
        for (File f : fs) if (f.lastModified() > best.lastModified()) best = f;
        return best;
    }

    private void say(String s) {
        log.append(s + "\n");
        logScroll.post(() -> logScroll.fullScroll(View.FOCUS_DOWN));
    }

    private void onStartStop() {
        Stand s = stand;
        if (s != null) {
            s.requestStop();
            start.setEnabled(false);
            say("Останавливаю после текущего замера…");
            return;
        }
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmm", Locale.ROOT).format(new Date());
        File out = new File(resultsDir(), PREFIX + stamp + ".txt");
        try {
            stand = new Stand(dev, this, PLAN, out);
        } catch (Exception e) {
            status.setText("Сбой стенда: " + e);
            return;
        }
        start.setText("Остановить");
        share.setEnabled(false);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        Thread t = new Thread(() -> stand.run(), "стенд");
        t.start();
    }

    private void onShare() {
        File f = lastResult();
        if (f == null) {
            say("Файла замеров ещё нет.");
            return;
        }
        try {
            ContentValues v = new ContentValues();
            v.put(MediaStore.MediaColumns.DISPLAY_NAME, f.getName());
            v.put(MediaStore.MediaColumns.MIME_TYPE, "text/plain");
            v.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
            Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
            if (uri == null) throw new IllegalStateException("система не дала создать файл в «Загрузках»");
            try (OutputStream o = getContentResolver().openOutputStream(uri)) {
                Files.copy(f.toPath(), o);
            }
            say("Файл в «Загрузках»: " + f.getName());
            Intent send = new Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM, uri)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivity(Intent.createChooser(send, "Отправить замеры"));
        } catch (Exception e) {
            say("Не получилось поделиться: " + e.getMessage());
        }
    }

    // ---- Stand.Listener: зовётся из потока стенда

    public void status(String s) {
        runOnUiThread(() -> status.setText(s));
    }

    public void log(String s) {
        runOnUiThread(() -> say(s));
    }

    public void progress(String s) {
        runOnUiThread(() -> progress.setText(s));
    }

    public void finished(String why, File result) {
        runOnUiThread(() -> {
            stand = null;
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            start.setText("Начать");
            start.setEnabled(true);
            share.setEnabled(lastResult() != null);
            say(why);
            if (result != null) say("Нажмите «Поделиться» и пришлите файл в чат.");
        });
    }
}
