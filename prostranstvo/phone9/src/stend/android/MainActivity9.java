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
import stend9.core.Stand9;

import java.io.File;
import java.io.OutputStream;
import java.nio.file.Files;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Экран малого стенда. Для левши: кнопки внизу слева — «Начать» /
 * «Остановить» первой, затем «Модель» и «Поделиться»; вверху крупно — что
 * идёт сейчас или почему стоит. Опасных кнопок нет.
 *
 * «Модель» открывает системное окно выбора файла; стенд копирует файл к себе
 * (разрешений на чужие файлы не просит). Файл итогов пишется в папку
 * приложения после каждого этапа; «Поделиться» копирует последний в
 * «Загрузки» и открывает «Отправить».
 */
public final class MainActivity9 extends Activity implements Stand.Listener {
    private static final String PREFIX = "shag9b_stend_";
    private static final int PICK = 9;

    private AndroidDevice9 dev;
    private TextView status, progress, log;
    private ScrollView logScroll;
    private Button start, pick, share;
    private volatile Stand9 stand;
    private volatile boolean copying;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        dev = new AndroidDevice9(this);
        int pad = dp(16);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);

        status = new TextView(this);
        status.setTextSize(22);
        status.setTypeface(Typeface.DEFAULT_BOLD);
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
        start = button("Начать", v -> onStartStop());
        pick = button("Модель", v -> onPick());
        share = button("Поделиться", v -> onShare());
        bar.addView(start);
        bar.addView(pick);
        bar.addView(share);
        root.addView(bar);

        setContentView(root);
        File m = dev.modelFile();
        status.setText(m.isFile() ? "Готов. Нажмите «Начать»." : "Сначала выберите файл модели кнопкой «Модель».");
        if (m.isFile()) say("Модель на месте: " + (m.length() >> 20) + " МиБ.");
        share.setEnabled(lastResult() != null);
        if (lastResult() != null) say("Есть файл прошлого прогона: " + lastResult().getName());
    }

    private Button button(String text, View.OnClickListener l) {
        Button x = new Button(this);
        x.setText(text);
        x.setTextSize(20);
        x.setOnClickListener(l);
        return x;
    }

    @Override
    protected void onResume() {
        super.onResume();
        dev.base.foreground = true;
    }

    @Override
    protected void onPause() {
        dev.base.foreground = false;
        super.onPause();
    }

    private int dp(int x) {
        return (int) (x * getResources().getDisplayMetrics().density + 0.5f);
    }

    private File resultsDir() {
        return new File(dev.workDir(), "itogi");
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

    // ---- выбор модели

    private void onPick() {
        if (stand != null || copying) return;
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*");
        startActivityForResult(i, PICK);
    }

    @Override
    protected void onActivityResult(int req, int code, Intent data) {
        super.onActivityResult(req, code, data);
        if (req != PICK || code != RESULT_OK || data == null || data.getData() == null) return;
        AndroidModelImport.prompt(this, data.getData(), dev.modelFile(), () -> {
            copying = true;
            start.setEnabled(false);
            pick.setEnabled(false);
            status.setText("Проверяю и копирую модель…");
        }, progress::setText, message -> {
            copying = false;
            start.setEnabled(true);
            pick.setEnabled(true);
            progress.setText("");
            status.setText(message);
            say(message);
        });
    }

    // ---- прогон

    private void onStartStop() {
        Stand9 s = stand;
        if (s != null) {
            s.requestStop();
            start.setEnabled(false);
            say("Останавливаю…");
            return;
        }
        if (copying) return;
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmm", Locale.ROOT).format(new Date());
        File out = new File(resultsDir(), PREFIX + stamp + ".txt");
        stand = new Stand9(dev, this, out);
        start.setText("Остановить");
        pick.setEnabled(false);
        share.setEnabled(false);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        new Thread(stand, "стенд").start();
    }

    private void onShare() {
        File f = lastResult();
        if (f == null) {
            say("Файла итогов ещё нет.");
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
            startActivity(Intent.createChooser(send, "Отправить итоги"));
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
            pick.setEnabled(true);
            share.setEnabled(lastResult() != null);
            progress.setText("");
            say(why);
            if (result != null) say("Нажмите «Поделиться» и пришлите файл в чат.");
        });
    }
}
