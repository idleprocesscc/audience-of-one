package io.github.audienceofone.djcontrol;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.view.Window;
import android.view.WindowManager;

/** Accepts one djrecord:// request while briefly foreground, then hands it to the service. */
public final class RecordActivity extends Activity {
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Window window = getWindow();
        window.setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
        window.setDimAmount(0f);
        window.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
            | WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
            | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
    }

    @Override
    protected void onPostResume() {
        super.onPostResume();
        Uri uri = getIntent() == null ? null : getIntent().getData();
        if (uri != null && "record".equals(uri.getHost())) {
            String timestamp = Long.toString(System.currentTimeMillis());
            String requestId = uri.getQueryParameter("request_id");
            String output = uri.getQueryParameter("output");
            if (requestId == null || requestId.isEmpty()) {
                requestId = "callin-" + timestamp;
            }
            if (output == null || output.isEmpty()) {
                output = "call-in-work/" + timestamp + ".m4a";
            }
            Intent service = new Intent(this, RecorderService.class);
            service.putExtra("request_id", requestId);
            service.putExtra("output", output);
            service.putExtra("root", uri.getQueryParameter("root"));
            service.putExtra("duration_seconds", uri.getQueryParameter("duration_seconds"));
            service.putExtra("cue", "true".equals(uri.getQueryParameter("cue")));
            startForegroundService(service);
        }
        finishAndRemoveTask();
    }
}
