package stend9.core;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;

/**
 * Процессы и память — чтением /proc, без Android: так ведущий одинаков на
 * телефоне и на сервере.
 *
 * Чего не умеет: видит только процессы своего пользователя (на Android —
 * своего приложения); если система закроет /proc детей, ответ — «не видно»
 * (-1, пустой список), а не «нет». Время процессора считается в тиках по
 * 100 в секунду — так на Linux и Android по умолчанию; другой частоты
 * ведущий не узнает.
 */
public final class Proc {
    private Proc() {
    }

    static final long TICKS_PER_S = 100;

    /** Поля /proc/PID/stat после имени процесса (оно в скобках и может содержать пробелы). */
    static String[] stat(long pid) {
        try {
            String s = new String(Files.readAllBytes(new File("/proc/" + pid + "/stat").toPath()), StandardCharsets.UTF_8);
            int r = s.lastIndexOf(')');
            return r < 0 ? null : s.substring(r + 2).trim().split(" ");
        } catch (IOException e) {
            return null;
        }
    }

    /** Состояние: 'R', 'S', 'Z'…; 0 — процесса нет. */
    public static char state(long pid) {
        String[] f = stat(pid);
        return f == null || f.length == 0 || f[0].isEmpty() ? 0 : f[0].charAt(0);
    }

    /** Жив: есть и не зомби. Сирота-зомби, которого никто не прибрал, — мёртв. */
    public static boolean alive(long pid) {
        char c = state(pid);
        return c != 0 && c != 'Z' && c != 'X';
    }

    /** Дети процесса — по полю «родитель» в /proc/ЧИСЛО/stat. */
    public static List<Long> children(long ppid) {
        List<Long> out = new ArrayList<>();
        File[] all = new File("/proc").listFiles();
        if (all == null) return out;
        for (File d : all) {
            String n = d.getName();
            if (n.isEmpty() || !Character.isDigit(n.charAt(0))) continue;
            long pid;
            try {
                pid = Long.parseLong(n);
            } catch (NumberFormatException e) {
                continue;
            }
            String[] f = stat(pid);
            if (f != null && f.length > 2 && String.valueOf(ppid).equals(f[1])) out.add(pid);
        }
        return out;
    }

    /**
     * Все потомки процесса: дети, их дети и так далее. Снимаются, пока процесс
     * жив: после его смерти осиротевшие потомки переходят к другому родителю,
     * и по дереву их уже не найти.
     */
    public static List<Long> descendants(long pid) {
        List<Long> out = new ArrayList<>();
        List<Long> queue = new ArrayList<>(children(pid));
        while (!queue.isEmpty()) {
            long k = queue.remove(0);
            if (out.contains(k)) continue;
            out.add(k);
            queue.addAll(children(k));
        }
        return out;
    }

    /** Время процессора процесса, мс (utime + stime); -1 — не видно. */
    public static long cpuMs(long pid) {
        String[] f = stat(pid);
        if (f == null || f.length < 13) return -1;
        try {
            return (Long.parseLong(f[11]) + Long.parseLong(f[12])) * 1000 / TICKS_PER_S;
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    /** Первое слово командной строки — какая программа запущена. */
    public static String program(long pid) {
        try {
            byte[] b = Files.readAllBytes(new File("/proc/" + pid + "/cmdline").toPath());
            int end = 0;
            while (end < b.length && b[end] != 0) end++;
            return new String(b, 0, end, StandardCharsets.UTF_8);
        } catch (IOException e) {
            return null;
        }
    }

    /** Строка «Имя: число kB» из файла /proc; -1 — нет файла или поля. */
    static long kb(String path, String field) {
        try {
            for (String l : Files.readAllLines(new File(path).toPath(), StandardCharsets.UTF_8)) {
                if (l.startsWith(field + ":")) return Long.parseLong(l.substring(field.length() + 1).replaceAll("[^0-9]", ""));
            }
        } catch (IOException | NumberFormatException ignored) {
        }
        return -1;
    }

    /**
     * Память процесса, КиБ: [RSS, наибольший RSS, RSS своих страниц, RSS
     * страниц файлов, PSS]. RSS считает веса модели целиком в каждом процессе;
     * PSS делит общие страницы файла весов между процессами — сумма PSS двух
     * процессов модели и есть их общая занятая память.
     */
    public static long[] mem(long pid) {
        String st = "/proc/" + pid + "/status";
        return new long[]{kb(st, "VmRSS"), kb(st, "VmHWM"), kb(st, "RssAnon"), kb(st, "RssFile"),
                kb("/proc/" + pid + "/smaps_rollup", "Pss")};
    }

    /** Память устройства, КиБ: [всего, доступно]. */
    public static long[] system() {
        return new long[]{kb("/proc/meminfo", "MemTotal"), kb("/proc/meminfo", "MemAvailable")};
    }

    /** Убить процессы сразу (SIGKILL) — сторож и этап «смерть с исполнителем». */
    public static void kill(List<Long> pids) {
        if (pids.isEmpty()) return;
        List<String> cmd = new ArrayList<>();
        cmd.add(new File("/system/bin/kill").exists() ? "/system/bin/kill" : "kill");
        cmd.add("-9");
        for (long p : pids) cmd.add(String.valueOf(p));
        try {
            new ProcessBuilder(cmd).redirectErrorStream(true).start().waitFor();
        } catch (IOException ignored) {
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
