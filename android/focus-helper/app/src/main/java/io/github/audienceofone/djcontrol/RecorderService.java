package io.github.audienceofone.djcontrol;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.IBinder;
import android.os.ParcelFileDescriptor;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import android.provider.MediaStore;
import android.util.Log;

import java.io.File;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/** Records one short, receipted microphone clip into shared Downloads, then stops itself. */
public final class RecorderService extends Service {
    private static final String TAG = "AudienceOfOneRecord";
    private static final String CHANNEL_ID = "audience_of_one_record";
    private static final String RECEIPT_URL = "http://127.0.0.1:18765/record";
    private static final String DEFAULT_ROOT = "AudienceOfOne";
    private static final int DEFAULT_SECONDS = 8;
    private static final int MAX_SECONDS = 60;
    private static final Pattern SAFE_RELATIVE =
        Pattern.compile("[A-Za-z0-9][A-Za-z0-9._ -]*(/[A-Za-z0-9][A-Za-z0-9._ -]*)*");

    private volatile boolean busy;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startForegroundWithNotification();
        String requestId = safeId(intent == null ? null : intent.getStringExtra("request_id"));
        String output = intent == null ? null : intent.getStringExtra("output");
        String root = intent == null ? null : intent.getStringExtra("root");
        boolean cue = intent != null && intent.getBooleanExtra("cue", false);
        if (root == null || root.isEmpty()) {
            root = DEFAULT_ROOT;
        }
        int seconds = clampSeconds(intent == null ? null : intent.getStringExtra("duration_seconds"));
        if (busy) {
            publish(requestId, "busy", 0);
            if (cue) {
                vibrate(600);
            }
            return START_NOT_STICKY;
        }
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            fail(requestId, "permission");
            return START_NOT_STICKY;
        }
        if (!isSafeRelative(output) || !output.endsWith(".m4a") || !isSafeRelative(root)) {
            fail(requestId, "path");
            return START_NOT_STICKY;
        }
        busy = true;
        if (cue) {
            vibrate(120);
            try {
                Thread.sleep(200);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            }
            vibrate(120);
        }
        publish(requestId, "recording", seconds);
        final String finalRoot = root;
        final String finalOutput = output;
        final int finalSeconds = seconds;
        new Thread(() -> {
            try {
                record(requestId, finalRoot, finalOutput, finalSeconds);
                publish(requestId, "saved", finalSeconds);
                if (cue) {
                    vibrate(300);
                }
            } catch (Exception error) {
                Log.w(TAG, "record failed request=" + requestId, error);
                publish(requestId, "failed", 0);
                if (cue) {
                    vibrate(600);
                }
            } finally {
                busy = false;
                stopSelf();
            }
        }, "audience-of-one-record").start();
        return START_NOT_STICKY;
    }

    private void record(String requestId, String root, String output, int seconds)
            throws Exception {
        MediaRecorder recorder = new MediaRecorder();
        recorder.setAudioSource(MediaRecorder.AudioSource.MIC);
        recorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
        recorder.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
        recorder.setAudioChannels(1);
        recorder.setAudioSamplingRate(44_100);
        recorder.setAudioEncodingBitRate(96_000);
        recorder.setMaxDuration(seconds * 1000);
        CountDownLatch finished = new CountDownLatch(1);
        recorder.setOnInfoListener((source, what, extra) -> {
            if (what == MediaRecorder.MEDIA_RECORDER_INFO_MAX_DURATION_REACHED) {
                finished.countDown();
            }
        });
        recorder.setOnErrorListener((source, what, extra) -> finished.countDown());

        Uri pending = null;
        File part = null;
        ContentResolver resolver = getContentResolver();
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                String directory = new File(output).getParent();
                String relative = Environment.DIRECTORY_DOWNLOADS + "/" + root
                    + (directory == null ? "" : "/" + directory);
                ContentValues values = new ContentValues();
                values.put(MediaStore.MediaColumns.DISPLAY_NAME, new File(output).getName());
                values.put(MediaStore.MediaColumns.MIME_TYPE, "audio/mp4");
                values.put(MediaStore.MediaColumns.RELATIVE_PATH, relative);
                values.put(MediaStore.MediaColumns.IS_PENDING, 1);
                pending = resolver.insert(
                    MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY),
                    values);
                if (pending == null) {
                    throw new IllegalStateException("MediaStore refused the clip row");
                }
                try (ParcelFileDescriptor descriptor = resolver.openFileDescriptor(pending, "w")) {
                    if (descriptor == null) {
                        throw new IllegalStateException("MediaStore returned no descriptor");
                    }
                    recorder.setOutputFile(descriptor.getFileDescriptor());
                    runRecorder(recorder, finished, seconds, requestId);
                }
                ContentValues publish = new ContentValues();
                publish.put(MediaStore.MediaColumns.IS_PENDING, 0);
                resolver.update(pending, publish, null, null);
                pending = null;
            } else {
                File target = new File(
                    Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS),
                    root + "/" + output);
                File parent = target.getParentFile();
                if (parent != null && !parent.isDirectory() && !parent.mkdirs()) {
                    throw new IllegalStateException("cannot create clip directory");
                }
                part = new File(target.getPath() + ".part");
                recorder.setOutputFile(part.getPath());
                runRecorder(recorder, finished, seconds, requestId);
                if (!part.renameTo(target)) {
                    throw new IllegalStateException("cannot publish the finished clip");
                }
                part = null;
            }
        } finally {
            recorder.release();
            if (pending != null) {
                resolver.delete(pending, null, null);
            }
            if (part != null && !part.delete()) {
                Log.w(TAG, "stale part file remains request=" + requestId);
            }
        }
    }

    private void runRecorder(MediaRecorder recorder, CountDownLatch finished, int seconds,
            String requestId) throws Exception {
        recorder.prepare();
        recorder.start();
        if (!finished.await(seconds + 10L, TimeUnit.SECONDS)) {
            Log.w(TAG, "recorder never reported max duration request=" + requestId);
        }
        recorder.stop();
    }

    private void startForegroundWithNotification() {
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null && manager.getNotificationChannel(CHANNEL_ID) == null) {
            manager.createNotificationChannel(new NotificationChannel(
                CHANNEL_ID, "Call-in recording", NotificationManager.IMPORTANCE_LOW));
        }
        Notification notification = new Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.presence_audio_online)
            .setContentTitle("Recording a call-in clip")
            .build();
        if (Build.VERSION.SDK_INT >= 30) {
            startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        } else {
            startForeground(1, notification);
        }
    }

    private void vibrate(long milliseconds) {
        Vibrator vibrator;
        if (Build.VERSION.SDK_INT >= 31) {
            VibratorManager manager = getSystemService(VibratorManager.class);
            vibrator = manager == null ? null : manager.getDefaultVibrator();
        } else {
            vibrator = getSystemService(Vibrator.class);
        }
        if (vibrator != null && vibrator.hasVibrator()) {
            vibrator.vibrate(VibrationEffect.createOneShot(
                milliseconds, VibrationEffect.DEFAULT_AMPLITUDE));
        }
    }

    private void fail(String requestId, String detail) {
        Log.w(TAG, "record rejected request=" + requestId + " reason=" + detail);
        publish(requestId, "failed", 0);
        stopSelf();
    }

    private void publish(String requestId, String state, int result) {
        long updatedAt = System.currentTimeMillis();
        Thread sender = new Thread(() -> {
            HttpURLConnection connection = null;
            try {
                String form = "request_id=" + URLEncoder.encode(requestId, "UTF-8")
                    + "&state=" + URLEncoder.encode(state, "UTF-8")
                    + "&result=" + result + "&updated_at=" + updatedAt;
                byte[] body = form.getBytes(StandardCharsets.UTF_8);
                connection = (HttpURLConnection) new URL(RECEIPT_URL).openConnection();
                connection.setRequestMethod("POST");
                connection.setConnectTimeout(300);
                connection.setReadTimeout(300);
                connection.setDoOutput(true);
                connection.setRequestProperty(
                    "Content-Type", "application/x-www-form-urlencoded; charset=utf-8");
                connection.setFixedLengthStreamingMode(body.length);
                try (OutputStream output = connection.getOutputStream()) {
                    output.write(body);
                }
                connection.getResponseCode();
            } catch (Exception error) {
                Log.w(TAG, "record receipt failed request=" + requestId + " state=" + state,
                    error);
            } finally {
                if (connection != null) {
                    connection.disconnect();
                }
            }
        }, "audience-of-one-record-receipt");
        sender.start();
        try {
            sender.join(700);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    private static boolean isSafeRelative(String value) {
        return value != null && value.length() <= 160
            && SAFE_RELATIVE.matcher(value).matches() && !value.contains("..");
    }

    private static String safeId(String value) {
        value = value == null ? "missing" : value.replaceAll("[^A-Za-z0-9._-]", "");
        return value.isEmpty() ? "missing" : value;
    }

    private static int clampSeconds(String value) {
        try {
            return value == null
                ? DEFAULT_SECONDS
                : Math.max(1, Math.min(MAX_SECONDS, Integer.parseInt(value)));
        } catch (NumberFormatException ignored) {
            return DEFAULT_SECONDS;
        }
    }
}
