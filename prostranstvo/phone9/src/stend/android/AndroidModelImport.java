package stend.android;

import android.app.Activity;
import android.app.AlertDialog;
import android.net.Uri;
import android.text.InputType;
import android.widget.EditText;
import android.widget.LinearLayout;

import stend9.core.GgufImport;

import java.io.File;
import java.io.InputStream;
import java.util.function.Consumer;

final class AndroidModelImport {
    private AndroidModelImport() {}

    static void prompt(Activity host, Uri uri, File target, Runnable started,
                       Consumer<String> progress, Consumer<String> finished) {
        LinearLayout fields = new LinearLayout(host);
        fields.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (24 * host.getResources().getDisplayMetrics().density + 0.5f);
        fields.setPadding(pad, 0, pad, 0);
        EditText hash = new EditText(host);
        hash.setSingleLine();
        hash.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        hash.setHint("Ожидаемый SHA-256");
        fields.addView(hash);
        EditText size = new EditText(host);
        size.setSingleLine();
        size.setInputType(InputType.TYPE_CLASS_NUMBER);
        size.setHint("Ожидаемый размер в байтах");
        fields.addView(size);
        AlertDialog dialog = new AlertDialog.Builder(host)
                .setTitle("Проверка модели GGUF")
                .setMessage("Укажите SHA-256 и размер файла из доверенного релиза.")
                .setView(fields)
                .setNegativeButton("Отмена", null)
                .setPositiveButton("Проверить и импортировать", null)
                .show();
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            GgufImport.Reference reference;
            try {
                reference = new GgufImport.Reference(hash.getText().toString(), size.getText().toString());
            } catch (IllegalArgumentException exc) {
                (hash.getText().toString().trim().matches("[0-9a-fA-F]{64}") ? size : hash)
                        .setError(exc.getMessage());
                return;
            }
            dialog.dismiss();
            started.run();
            new Thread(() -> {
                String message;
                try (InputStream in = host.getContentResolver().openInputStream(uri)) {
                    long[] last = {0};
                    GgufImport.install(in, target, reference, done -> {
                        if (done - last[0] >= (50L << 20)) {
                            last[0] = done;
                            host.runOnUiThread(() -> progress.accept((done >> 20) + " из "
                                    + (reference.size >> 20) + " МиБ"));
                        }
                    });
                    message = "Модель проверена и скопирована. Нажмите «Начать».";
                } catch (Exception exc) {
                    message = "Модель не импортирована: " + exc.getMessage();
                }
                String result = message;
                host.runOnUiThread(() -> finished.accept(result));
            }, "копия модели").start();
        });
    }
}
