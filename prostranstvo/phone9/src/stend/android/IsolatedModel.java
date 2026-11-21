package stend.android;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Parcel;
import android.os.ParcelFileDescriptor;
import android.os.RemoteException;
import android.os.ResultReceiver;

import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/** Drives the isolated service over two pipes so server and Android use the same protocol. */
final class IsolatedModel extends Process implements ServiceConnection {
    private final Context context;
    private final CountDownLatch connected = new CountDownLatch(1), done = new CountDownLatch(1);
    private final AtomicInteger exit = new AtomicInteger(-1);
    private final AtomicBoolean stopping = new AtomicBoolean();
    private volatile IBinder remote;
    private boolean bound;
    private InputStream output;
    private OutputStream input;
    private int pid;

    IsolatedModel(Context context, int variant, int mode, File weights, int threads, int batch) throws IOException {
        this.context = context;
        Intent intent = new Intent(context, ModelService.class);
        try {
            synchronized (this) {
                bound = context.bindIsolatedService(intent, Context.BIND_AUTO_CREATE,
                        UUID.randomUUID().toString().replace("-", ""), Runnable::run, this);
            }
            if (!bound || !connected.await(10, TimeUnit.SECONDS) || remote == null) {
                throw new IOException("изолированная служба модели не запустилась");
            }
            identity();
            start(variant, mode, weights, threads, batch);
        } catch (InterruptedException e) {
            destroy();
            Thread.currentThread().interrupt();
            throw new IOException("запуск модели прерван", e);
        } catch (IOException | RemoteException | RuntimeException e) {
            destroy();
            throw new IOException("не запустить модель с отдельным Android UID", e);
        }
    }

    private void identity() throws IOException, RemoteException {
        Parcel data = Parcel.obtain(), reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(ModelService.INTERFACE);
            if (!remote.transact(ModelService.IDENTITY, data, reply, 0)) throw new IOException("UID модели не получен");
            reply.readException();
            int uid = reply.readInt();
            pid = reply.readInt();
            if (uid == android.os.Process.myUid() || pid <= 0) throw new IOException("модель вне изоляции");
            remote.linkToDeath(() -> complete(1), 0);
        } finally {
            data.recycle();
            reply.recycle();
        }
    }

    private void start(int variant, int mode, File weights, int threads, int batch) throws IOException, RemoteException {
        ResultReceiver result = new ResultReceiver(null) {
            @Override protected void onReceiveResult(int code, Bundle ignored) { complete(code == 0 ? 0 : 1); }
        };
        ParcelFileDescriptor[] requests = ParcelFileDescriptor.createPipe();
        input = new ParcelFileDescriptor.AutoCloseOutputStream(requests[1]);
        try (ParcelFileDescriptor reader = requests[0]) {
            ParcelFileDescriptor[] responses = ParcelFileDescriptor.createPipe();
            output = new FilterInputStream(new ParcelFileDescriptor.AutoCloseInputStream(responses[0])) {
                private int frameBytes;

                private int check(int b) throws IOException {
                    if (b == '\n') frameBytes = 0;
                    else if (b >= 0 && ++frameBytes >= 16 * 1024) throw new IOException("слишком длинный ответ модели");
                    return b;
                }

                @Override public int read() throws IOException { return check(in.read()); }
                @Override public int read(byte[] buffer, int offset, int length) throws IOException {
                    int count = in.read(buffer, offset, length);
                    for (int i = 0; i < count; i++) check(buffer[offset + i] & 0xff);
                    return count;
                }
            };
            try (ParcelFileDescriptor writer = responses[1];
                 ParcelFileDescriptor model = mode == 2
                         ? ParcelFileDescriptor.open(weights, ParcelFileDescriptor.MODE_READ_ONLY) : null) {
                Parcel data = Parcel.obtain(), reply = Parcel.obtain();
                try {
                    data.writeInterfaceToken(ModelService.INTERFACE);
                    data.writeInt(variant);
                    data.writeInt(mode);
                    data.writeInt(threads);
                    data.writeInt(batch);
                    result.writeToParcel(data, 0);
                    reader.writeToParcel(data, 0);
                    writer.writeToParcel(data, 0);
                    if (model != null) model.writeToParcel(data, 0);
                    if (!remote.transact(ModelService.START, data, reply, 0)) throw new IOException("служба модели недоступна");
                    reply.readException();
                } finally {
                    data.recycle();
                    reply.recycle();
                }
            }
        }
    }

    @Override public void onServiceConnected(ComponentName name, IBinder service) {
        remote = service;
        connected.countDown();
    }

    @Override public void onServiceDisconnected(ComponentName name) { complete(1); }
    @Override public void onBindingDied(ComponentName name) { connected.countDown(); complete(1); }
    @Override public void onNullBinding(ComponentName name) { connected.countDown(); complete(1); }

    private synchronized void complete(int code) {
        if (!exit.compareAndSet(-1, code)) return;
        try {
            unbind();
        } finally {
            done.countDown();
        }
    }

    private synchronized void unbind() {
        if (bound) {
            bound = false;
            context.unbindService(this);
        }
    }

    @Override public OutputStream getOutputStream() { return input; }
    @Override public InputStream getInputStream() { return output; }
    @Override public InputStream getErrorStream() { return new ByteArrayInputStream(new byte[0]); }
    @Override public int waitFor() throws InterruptedException { done.await(); return exit.get(); }
    @Override public boolean waitFor(long timeout, TimeUnit unit) throws InterruptedException {
        return done.await(timeout, unit);
    }
    @Override public int exitValue() {
        int status = exit.get();
        if (status == -1) throw new IllegalThreadStateException("модель ещё работает");
        return status;
    }

    @Override public void destroy() {
        if (!stopping.compareAndSet(false, true)) return;
        IBinder service = remote;
        if (service != null && exit.get() == -1) {
            Parcel data = Parcel.obtain();
            try {
                data.writeInterfaceToken(ModelService.INTERFACE);
                service.transact(ModelService.STOP, data, null, IBinder.FLAG_ONEWAY);
            } catch (RemoteException | RuntimeException ignored) {
                // Unbinding below also releases the isolated process.
            } finally {
                data.recycle();
            }
        }
        if (service == null || pid == 0) complete(1);
        else unbind();
        try { if (input != null) input.close(); } catch (IOException ignored) {}
        try { if (output != null) output.close(); } catch (IOException ignored) {}
    }

    @Override public Process destroyForcibly() { destroy(); return this; }
}
