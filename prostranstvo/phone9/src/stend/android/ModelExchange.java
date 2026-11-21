package stend.android;

import stend.core.Exec;
import stend.core.Json;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.Map;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;

/** Forwards the executor's bounded text questions to two isolated services. */
final class ModelExchange implements Exec.ModelExchange {
    private static final int MAX_FRAME = 16 * 1024;
    private final Process[] models;
    private final InputStream[] replies;
    private final int timeout;
    private final AtomicBoolean closed = new AtomicBoolean();
    private volatile boolean busy;

    ModelExchange(Process first, Process second, int timeout) {
        models = new Process[] {first, second};
        replies = new InputStream[] {new BufferedInputStream(first.getInputStream()),
                                     new BufferedInputStream(second.getInputStream())};
        this.timeout = timeout;
    }

    @Override public String ask(int contour, String query, int limit) throws IOException {
        if (contour < 1 || contour > 2 || query == null || query.length() > 5464 || limit < 1 || limit > 4096
                || Base64.getDecoder().decode(query).length > 4096) {
            throw new IOException("недопустимый запрос модели");
        }
        Process model = models[contour - 1];
        if (!model.isAlive()) return null;
        FutureTask<String> request = new FutureTask<>(() -> talk(model, replies[contour - 1], query, limit));
        Thread thread = new Thread(request, "обмен с моделью");
        thread.setDaemon(true);
        busy = true;
        thread.start();
        try {
            return request.get(timeout, TimeUnit.SECONDS);
        } catch (ExecutionException | TimeoutException e) {
            model.destroyForcibly();
            return null;
        } catch (InterruptedException e) {
            model.destroyForcibly();
            Thread.currentThread().interrupt();
            throw new IOException("вопрос модели прерван", e);
        } finally {
            request.cancel(true);
            busy = false;
        }
    }

    private static String talk(Process model, InputStream response, String query, int limit) throws IOException {
        String request = Json.str(new Json.M().with("q", query).with("max", limit)) + "\n";
        model.getOutputStream().write(request.getBytes(StandardCharsets.UTF_8));
        model.getOutputStream().flush();
        Object parsed = Json.parse(readLine(response));
        Object value = parsed instanceof Map ? ((Map<?, ?>) parsed).get("a") : null;
        if (!(value instanceof String)) throw new IOException("неверный ответ модели");
        Base64.getDecoder().decode((String) value);
        return (String) value;
    }

    static String readLine(InputStream stream) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        for (int i = 0; i < MAX_FRAME; i++) {
            int b = stream.read();
            if (b == '\n') return new String(line.toByteArray(), StandardCharsets.UTF_8);
            if (b < 0) throw new EOFException("канал модели закрыт");
            line.write(b);
        }
        throw new IOException("слишком длинный ответ модели");
    }

    @Override public int alive() {
        int alive = 0;
        for (Process model : models) if (model.isAlive()) alive++;
        return alive;
    }

    @Override public void close() {
        if (closed.compareAndSet(false, true)) for (Process model : models) model.destroyForcibly();
    }

    @Override public boolean busy() { return busy; }
}
