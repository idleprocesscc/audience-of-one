package io.github.audienceofone.djcontrol;

import android.app.Activity;
import android.media.AudioManager;
import android.os.Bundle;

/** Apply one explicit music-stream fade from a foreground, translucent activity. */
public final class FaderActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        final int requestedPercent = getIntent().getIntExtra("target_percent", -1);
        final int duration = Math.max(
            0, Math.min(30_000, getIntent().getIntExtra("duration_ms", 0))
        );
        new Thread(() -> {
            try {
                AudioManager audio = (AudioManager) getSystemService(AUDIO_SERVICE);
                if (audio == null || requestedPercent < 0 || requestedPercent > 100) return;
                int maximum = audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
                int requested = Math.round(maximum * (requestedPercent / 100f));
                int start = audio.getStreamVolume(AudioManager.STREAM_MUSIC);
                int steps = Math.max(1, Math.min(30, Math.round(duration / 160f)));
                long delay = steps > 0 ? duration / steps : 0;
                for (int step = 1; step <= steps; step++) {
                    int value = Math.round(start + (requested - start) * (step / (float) steps));
                    audio.setStreamVolume(AudioManager.STREAM_MUSIC, value, 0);
                    if (step < steps && delay > 0) Thread.sleep(delay);
                }
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
            } finally {
                runOnUiThread(this::finishAndRemoveTask);
            }
        }, "audience-fader").start();
    }
}
