package io.github.audienceofone.djcontrol;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioManager;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Window;
import android.view.WindowManager;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/** Owns one short, receipted audio-focus lease so Android may duck music for speech. */
public final class FocusActivity extends Activity {
    private static final String TAG = "AudienceOfOneFocus";
    private static final int MAX_HOLD_MS = 120_000;
    private static final String RECEIPT_URL = "http://127.0.0.1:18765/focus";

    private final Handler handler = new Handler(Looper.getMainLooper());
    private AudioManager audioManager;
    private AudioFocusRequest focusRequest;
    private boolean focusHeld;
    private Uri pendingRequest;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Window window = getWindow();
        window.setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
        window.setDimAmount(0f);
        window.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
            | WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
            | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);
        pendingRequest = getIntent() == null ? null : getIntent().getData();
    }

    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        pendingRequest = intent == null ? null : intent.getData();
        handler.post(this::applyPendingRequest);
    }

    @Override
    protected void onPostResume() {
        super.onPostResume();
        applyPendingRequest();
    }

    private void applyPendingRequest() {
        Uri uri = pendingRequest;
        pendingRequest = null;
        if (uri == null) {
            publish("missing", "failed", AudioManager.AUDIOFOCUS_REQUEST_FAILED);
            finishAndRemoveTask();
            return;
        }
        if ("release".equals(uri.getHost())) {
            releaseFocus(requestId(uri), "released", true);
        } else if ("hold".equals(uri.getHost())) {
            holdFocus(uri);
        } else {
            publish(requestId(uri), "failed", AudioManager.AUDIOFOCUS_REQUEST_FAILED);
            finishAndRemoveTask();
        }
    }

    private void holdFocus(Uri uri) {
        String requestId = requestId(uri);
        int durationMs = Math.max(500, Math.min(MAX_HOLD_MS,
            integer(uri.getQueryParameter("duration_ms"), 15_000)));
        releaseFocus(requestId, "replaced", false);

        audioManager = (AudioManager) getSystemService(AUDIO_SERVICE);
        if (audioManager == null) {
            publish(requestId, "failed", AudioManager.AUDIOFOCUS_REQUEST_FAILED);
            finishAndRemoveTask();
            return;
        }
        AudioAttributes attributes = new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_ASSISTANCE_NAVIGATION_GUIDANCE)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build();
        focusRequest = new AudioFocusRequest.Builder(
            AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(attributes)
            .setOnAudioFocusChangeListener(change ->
                Log.i(TAG, "focus change=" + change + " request=" + requestId))
            .build();
        int result = audioManager.requestAudioFocus(focusRequest);
        focusHeld = result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
        publish(requestId, focusHeld ? "held" : "failed", result);
        if (!focusHeld) {
            finishAndRemoveTask();
            return;
        }
        handler.removeCallbacksAndMessages(null);
        handler.postDelayed(() -> releaseFocus(requestId, "expired", true), durationMs);
    }

    private void releaseFocus(String requestId, String state, boolean finish) {
        handler.removeCallbacksAndMessages(null);
        if (focusHeld && audioManager != null && focusRequest != null) {
            audioManager.abandonAudioFocusRequest(focusRequest);
        }
        focusHeld = false;
        focusRequest = null;
        if (!"replaced".equals(state)) {
            publish(requestId, state, AudioManager.AUDIOFOCUS_REQUEST_GRANTED);
        }
        if (finish) {
            finishAndRemoveTask();
        }
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
                Log.w(TAG, "focus receipt failed request=" + requestId + " state=" + state,
                    error);
            } finally {
                if (connection != null) {
                    connection.disconnect();
                }
            }
        }, "audience-of-one-focus-receipt");
        sender.start();
        try {
            sender.join(700);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }

    private static String requestId(Uri uri) {
        String value = uri == null ? null : uri.getQueryParameter("request_id");
        value = value == null ? "missing" : value.replaceAll("[^A-Za-z0-9._-]", "");
        return value.isEmpty() ? "missing" : value;
    }

    private static int integer(String value, int fallback) {
        try {
            return value == null ? fallback : Integer.parseInt(value);
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    @Override
    protected void onDestroy() {
        if (focusHeld) {
            releaseFocus(requestId(getIntent() == null ? null : getIntent().getData()),
                "destroyed", false);
        }
        handler.removeCallbacksAndMessages(null);
        super.onDestroy();
    }
}
