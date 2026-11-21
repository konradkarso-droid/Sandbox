package stend.core;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.File;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.SecureDirectoryStream;
import java.nio.file.attribute.BasicFileAttributeView;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/**
 * Исполнитель второй реализации в отдельном процессе; обмен — строками
 * JSON по протоколу контракта шага 7 (§1).
 *
 * Процесс запускается через sh, который печатает свой номер и заменяет
 * себя исполнителем (exec): номер процесса тот же, и по нему читается
 * пиковая память из /proc. Изменять исполнитель ради этого не пришлось.
 */
public final class Exec implements AutoCloseable {
    private final Process p;
    private final BufferedWriter in;
    private final BufferedReader out;
    final File root;
    public final long pid;

    public Exec(String exe, File root, String kind) throws IOException {
        this(exe, root, kind, java.util.Collections.<String>emptyList(), java.util.Collections.<String, String>emptyMap());
    }

    /**
     * extra — параметры запуска после --root и --kind (шаг 9б: --model=…,
     * --model-timeout=…, --model-net=…); env — переменные окружения поверх
     * обычных (MODEL_SERVE — программа процесса модели, контракт §18.1).
     */
    public Exec(String exe, File root, String kind, List<String> extra, Map<String, String> env) throws IOException {
        this.root = root;
        if (!root.isDirectory() && !root.mkdirs()) throw new IOException("не создать папку " + root);
        File[] old = root.listFiles();
        if (old != null) for (File f : old) if (f.getName().startsWith("run")) delete(root, f);
        String sh = new File("/system/bin/sh").exists() ? "/system/bin/sh" : "/bin/sh";
        java.util.ArrayList<String> cmd = new java.util.ArrayList<>(Arrays.asList(sh, "-c", "echo $$; exec \"$0\" \"$@\"",
                exe, "--root", root.getPath(), "--kind", kind));
        cmd.addAll(extra);
        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.environment().putAll(env);
        pb.redirectError(ProcessBuilder.Redirect.appendTo(new File(root, "stderr.txt")));
        p = pb.start();
        in = new BufferedWriter(new OutputStreamWriter(p.getOutputStream(), StandardCharsets.UTF_8));
        out = new BufferedReader(new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8));
        String first = out.readLine();
        if (first == null) throw new IOException("исполнитель не запустился: " + exe);
        pid = Long.parseLong(first.trim());
    }

    /** Ответ исполнителя; при статусе не «ok» — исключение с причиной. */
    public Object call(String cmd, Object... args) throws IOException {
        in.write(Json.str(new Json.M().with("cmd", cmd).with("args", Arrays.asList(args))));
        in.write('\n');
        in.flush();
        String line = out.readLine();
        if (line == null) {
            int code;
            try {
                code = p.waitFor(2, TimeUnit.SECONDS) ? p.exitValue() : -1;
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                code = -1;
            }
            throw new IOException("исполнитель завершился на «" + cmd + "», код " + code);
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> r = (Map<String, Object>) Json.parse(line);
        if (!"ok".equals(r.get("status"))) throw new IOException("исполнитель: " + r.get("status") + ": " + r.get("value"));
        return r.get("value");
    }

    public String newRun(Map<String, Object> cfg) throws IOException {
        return (String) call("new", cfg, null);
    }

    @SuppressWarnings("unchecked")
    public List<Object> load(int contour, byte[] module) throws IOException {
        return (List<Object>) call("load", contour, module);
    }

    @SuppressWarnings("unchecked")
    public List<Object> run(int contour) throws IOException {
        return (List<Object>) call("run", contour);
    }

    /** Выгрузка контура: [остаток топлива, расход бюджета]. */
    @SuppressWarnings("unchecked")
    public long[] fuelSpent(int contour) throws IOException {
        Map<String, Object> d = (Map<String, Object>) call("dump");
        Map<String, Object> cs = (Map<String, Object>) d.get("contours");
        Map<String, Object> c = (Map<String, Object>) cs.get(String.valueOf(contour));
        return new long[]{((Number) c.get("fuel")).longValue(), ((Number) c.get("spent")).longValue()};
    }

    /** Пиковая память процесса, КиБ (VmHWM); -1 — недоступно. */
    public long peakKb() {
        try {
            for (String l : Files.readAllLines(new File("/proc/" + pid + "/status").toPath(), StandardCharsets.UTF_8)) {
                if (l.startsWith("VmHWM:")) return Long.parseLong(l.replaceAll("[^0-9]", ""));
            }
        } catch (IOException | NumberFormatException e) {
            return -1;
        }
        return -1;
    }

    /** Убить исполнитель сразу (SIGKILL), не дожидаясь конца команды. */
    public void kill() {
        p.destroyForcibly();
    }

    public boolean alive() {
        return p.isAlive();
    }

    @Override
    public void close() {
        try {
            in.close();
        } catch (IOException ignored) {
        }
        try {
            if (!p.waitFor(3, TimeUnit.SECONDS)) p.destroyForcibly();
        } catch (InterruptedException e) {
            p.destroyForcibly();
            Thread.currentThread().interrupt();
        }
    }

    /** Remove a direct child of a trusted directory without opening symlinks. */
    public static void delete(File root, File child) throws IOException {
        Path base = root.toPath().toAbsolutePath().normalize();
        Path entry = child.toPath().toAbsolutePath().normalize();
        if (!base.equals(entry.getParent()) || base.getParent() == null) {
            throw new IOException("путь вне рабочей папки: " + child);
        }
        try (DirectoryStream<Path> opened = Files.newDirectoryStream(base.getParent())) {
            if (!(opened instanceof SecureDirectoryStream)) {
                throw new IOException("файловая система не поддерживает безопасное удаление: " + root);
            }
            SecureDirectoryStream<Path> parent = (SecureDirectoryStream<Path>) opened;
            try (SecureDirectoryStream<Path> dir = parent.newDirectoryStream(base.getFileName(), LinkOption.NOFOLLOW_LINKS)) {
                deleteEntry(dir, entry.getFileName());
            }
        }
    }

    private static void deleteEntry(SecureDirectoryStream<Path> parent, Path name) throws IOException {
        try {
            BasicFileAttributeView view = parent.getFileAttributeView(
                    name, BasicFileAttributeView.class, LinkOption.NOFOLLOW_LINKS);
            if (view.readAttributes().isDirectory()) {
                try (SecureDirectoryStream<Path> dir = parent.newDirectoryStream(name, LinkOption.NOFOLLOW_LINKS)) {
                    for (Path child : dir) deleteEntry(dir, child.getFileName());
                }
                parent.deleteDirectory(name);
            } else {
                parent.deleteFile(name);
            }
        } catch (NoSuchFileException ignored) {
            // The requested entry is already absent.
        }
    }
}
