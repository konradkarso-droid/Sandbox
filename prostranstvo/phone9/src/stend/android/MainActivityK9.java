package stend.android;

import android.app.Activity;
import android.app.AlertDialog;
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

import stend.core.Exec;
import stend.core.Stand;
import stend9.core.Kalibr9;

import java.io.File;
import java.io.FileInputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/**
 * Экран стенда калибровки. Раскладка — как у малого стенда (MainActivity9):
 * для левши кнопки внизу слева — «Начать» / «Остановить», «Модель»,
 * «Поделиться»; вверху крупно — что идёт или почему стоит. Единственная
 * опасная кнопка — «Сначала» (стирает собранное) — вверху справа и с
 * вопросом.
 *
 * Калибровка идёт этапами и продолжается с первого незаконченного: после
 * остановки (кнопкой, сторожем, по заряду) достаточно снова нажать
 * «Начать». «Поделиться» складывает папку итогов в zip, кладёт его в
 * «Загрузки» и открывает «Отправить».
 */
public final class MainActivityK9 extends Activity implements Stand.Listener {
    private static final int PICK = 9;

    private AndroidDevice9 dev;
    private TextView status, progress, log;
    private ScrollView logScroll;
    private Button start, pick, share, again;
    private volatile Kalibr9 stand;
    private volatile boolean copying;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        dev = new AndroidDevice9(this);
        int pad = dp(16);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);

        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        status = new TextView(this);
        status.setTextSize(22);
        status.setTypeface(Typeface.DEFAULT_BOLD);
        top.addView(status, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        again = new Button(this);
        again.setText("Сначала");
        again.setTextSize(14);
        again.setOnClickListener(v -> onAgain());
        top.addView(again);
        root.addView(top);

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
        share.setEnabled(new File(resultsDir(), "kalibr9.json").isFile());
        if (new File(resultsDir(), "kalibr9.json").isFile()) say("Есть начатая калибровка: «Начать» продолжит её.");
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
        return new File(dev.workDir(), "kalibr9");
    }

    private void say(String s) {
        log.append(s + "\n");
        logScroll.post(() -> logScroll.fullScroll(View.FOCUS_DOWN));
    }

    // ---- выбор модели: как у малого стенда

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
        Kalibr9 s = stand;
        if (s != null) {
            s.requestStop();
            start.setEnabled(false);
            say("Останавливаю…");
            return;
        }
        if (copying) return;
        stand = new Kalibr9(dev, this, resultsDir(), new Kalibr9.Profile());
        start.setText("Остановить");
        pick.setEnabled(false);
        share.setEnabled(false);
        again.setEnabled(false);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        new Thread(stand, "калибровка").start();
    }

    private void onAgain() {
        if (stand != null || copying) return;
        new AlertDialog.Builder(this)
                .setMessage("Начать калибровку сначала? Всё собранное будет стёрто. Если вы уже прислали файл в чат — можно.")
                .setPositiveButton("Стереть и начать сначала", (d, w) -> {
                    Exec.delete(resultsDir());
                    say("Собранное стёрто. «Начать» начнёт калибровку с первого этапа.");
                    share.setEnabled(false);
                })
                .setNegativeButton("Отмена", null)
                .show();
    }

    private void onShare() {
        File dir = resultsDir();
        if (!new File(dir, "kalibr9.json").isFile()) {
            say("Файла итогов ещё нет.");
            return;
        }
        String name = "kalibr9_" + new SimpleDateFormat("yyyyMMdd_HHmm", Locale.ROOT).format(new Date()) + ".zip";
        try {
            ContentValues v = new ContentValues();
            v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
            v.put(MediaStore.MediaColumns.MIME_TYPE, "application/zip");
            v.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
            Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
            if (uri == null) throw new IllegalStateException("система не дала создать файл в «Загрузках»");
            try (OutputStream o = getContentResolver().openOutputStream(uri); ZipOutputStream z = new ZipOutputStream(o)) {
                zip(z, dir, "");
            }
            say("Файл в «Загрузках»: " + name);
            Intent send = new Intent(Intent.ACTION_SEND).setType("application/zip").putExtra(Intent.EXTRA_STREAM, uri)
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            startActivity(Intent.createChooser(send, "Отправить итоги"));
        } catch (Exception e) {
            say("Не получилось поделиться: " + e.getMessage());
        }
    }

    private static void zip(ZipOutputStream z, File f, String path) throws java.io.IOException {
        File[] fs = f.listFiles();
        if (fs == null) return;
        byte[] buf = new byte[1 << 16];
        for (File x : fs) {
            String p = path + x.getName();
            if (x.isDirectory()) {
                zip(z, x, p + "/");
                continue;
            }
            if (p.endsWith(".tmp")) continue;
            z.putNextEntry(new ZipEntry(p));
            try (FileInputStream in = new FileInputStream(x)) {
                int n;
                while ((n = in.read(buf)) > 0) z.write(buf, 0, n);
            }
            z.closeEntry();
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
            again.setEnabled(true);
            share.setEnabled(new File(resultsDir(), "kalibr9.json").isFile());
            progress.setText("");
            say(why);
            say("Нажмите «Поделиться» и пришлите zip в чат. Если этапы остались — «Начать» продолжит.");
        });
    }
}
