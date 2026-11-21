package stend.android;

import android.app.Service;
import android.content.Intent;
import android.os.Binder;
import android.os.IBinder;
import android.os.Parcel;
import android.os.ParcelFileDescriptor;
import android.os.Process;
import android.os.ResultReceiver;

import java.io.IOException;
import java.util.concurrent.atomic.AtomicBoolean;

/** One model instance in an Android-assigned isolated UID. */
public final class ModelService extends Service {
    static final int IDENTITY = IBinder.FIRST_CALL_TRANSACTION;
    static final int START = IDENTITY + 1;
    static final int STOP = IDENTITY + 2;
    static final String INTERFACE = "stend.android.ModelService";
    private final AtomicBoolean started = new AtomicBoolean();

    private static native int runModel(int mode, int in, int out, int model, int threads, int batch);

    @Override public IBinder onBind(Intent intent) {
        return new Binder() {
            @Override protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) {
                if (code < IDENTITY || code > STOP) return false;
                data.enforceInterface(INTERFACE);
                int appUid = getApplicationInfo().uid;
                if (Binder.getCallingUid() != appUid || Process.myUid() == appUid) {
                    throw new SecurityException("model service requires an isolated UID and its own controller");
                }
                if (code == IDENTITY) {
                    reply.writeNoException();
                    reply.writeInt(Process.myUid());
                    reply.writeInt(Process.myPid());
                    return true;
                }
                if (code == STOP) {
                    Process.killProcess(Process.myPid());
                    return true;
                }
                start(data);
                reply.writeNoException();
                return true;
            }
        };
    }

    private void start(Parcel data) {
        int variant = data.readInt(), mode = data.readInt(), threads = data.readInt(), batch = data.readInt();
        if (variant < 0 || variant > 1 || mode < 0 || mode > 2 || threads < 1 || threads > 256
                || batch < 1 || batch > 256 || !started.compareAndSet(false, true)) {
            throw new IllegalArgumentException("invalid model configuration");
        }
        ResultReceiver result = ResultReceiver.CREATOR.createFromParcel(data);
        ParcelFileDescriptor in = ParcelFileDescriptor.CREATOR.createFromParcel(data);
        ParcelFileDescriptor out = ParcelFileDescriptor.CREATOR.createFromParcel(data);
        ParcelFileDescriptor model = mode == 2 ? ParcelFileDescriptor.CREATOR.createFromParcel(data) : null;
        new Thread(() -> {
            int exit = 1;
            try (ParcelFileDescriptor input = in; ParcelFileDescriptor output = out;
                 ParcelFileDescriptor weights = model) {
                System.loadLibrary(variant == 0 ? "serve9a" : "serve9b");
                exit = runModel(mode, input.getFd(), output.getFd(), weights == null ? -1 : weights.getFd(),
                                threads, batch);
            } catch (IOException | LinkageError | RuntimeException e) {
                android.util.Log.w("ModelService", "model stopped", e);
            } finally {
                result.send(exit, null);
            }
        }, "модель").start();
    }
}
