package stend9.core;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Locale;
import java.util.function.LongConsumer;

/** Accept model bytes only after checking the reference supplied by the operator. */
public final class GgufImport {
    private GgufImport() {}

    public static final class Reference {
        private final String sha256;
        public final long size;

        public Reference(String sha256, String size) {
            this.sha256 = sha256.trim().toLowerCase(Locale.ROOT);
            if (!this.sha256.matches("[0-9a-f]{64}")) {
                throw new IllegalArgumentException("SHA-256 должен состоять из 64 шестнадцатеричных знаков.");
            }
            try {
                this.size = Long.parseLong(size.trim());
            } catch (NumberFormatException exc) {
                throw new IllegalArgumentException("Укажите ожидаемый размер в байтах.");
            }
            if (this.size < 4) throw new IllegalArgumentException("Укажите ожидаемый размер в байтах.");
        }
    }

    public static void install(InputStream source, File target, Reference reference, LongConsumer progress)
            throws IOException, NoSuchAlgorithmException {
        if (source == null) throw new IOException("Файл модели не открылся.");
        Path folder = target.toPath().getParent();
        Files.createDirectories(folder);
        Path part = Files.createTempFile(folder, ".gguf-", ".part");
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] magic = {'G', 'G', 'U', 'F'};
            byte[] buffer = new byte[1 << 20];
            long done = 0;
            try (FileOutputStream out = new FileOutputStream(part.toFile())) {
                int count;
                while ((count = source.read(buffer, 0, (int) Math.min(buffer.length - 1L,
                        reference.size - done) + 1)) != -1) {
                    if (count > reference.size - done) throw new IOException("Файл больше ожидаемого размера.");
                    for (int i = 0; i < count && done + i < magic.length; i++) {
                        if (buffer[i] != magic[(int) done + i]) throw new IOException("Неверная сигнатура GGUF.");
                    }
                    out.write(buffer, 0, count);
                    digest.update(buffer, 0, count);
                    done += count;
                    progress.accept(done);
                }
                if (done != reference.size) throw new IOException("Размер GGUF не совпадает с эталоном.");
                StringBuilder actual = new StringBuilder(64);
                for (byte b : digest.digest()) actual.append(String.format(Locale.ROOT, "%02x", b & 255));
                if (!reference.sha256.contentEquals(actual)) throw new IOException("SHA-256 GGUF не совпадает с эталоном.");
                out.getFD().sync();
            }
            if (!part.toFile().setReadOnly()) throw new IOException("Не удалось закрыть файл для записи.");
            Files.move(part, target.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } finally {
            Files.deleteIfExists(part);
        }
    }
}
