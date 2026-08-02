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
            Intent service = new Intent(this, RecorderService.class);
            service.putExtra("request_id", uri.getQueryParameter("request_id"));
            service.putExtra("output", uri.getQueryParameter("output"));
            service.putExtra("root", uri.getQueryParameter("root"));
            service.putExtra("duration_seconds", uri.getQueryParameter("duration_seconds"));
            startForegroundService(service);
        }
        finishAndRemoveTask();
    }
}
