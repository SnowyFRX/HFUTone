package com.hfutone.app;

import android.content.Context;
import android.content.Intent;
import android.graphics.PixelFormat;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.Log;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.view.WindowManager;
import android.widget.TextView;

/**
 * 签到成功的「灵动岛」横幅。
 *
 * 为什么不用通知：通知的 heads-up（悬浮通知）开关归系统管，国产 ROM 默认关，
 * 应用改不了，用户还得自己去设置里翻。悬浮窗是应用自己往屏幕上画，只看
 * 「显示在其他应用上层」这一个权限，做完就能弹。
 *
 * 形态：屏幕顶部一条深色圆角胶囊，滑入、停 4 秒、自动消失；点它回到 App。
 * 没有权限就静默返回 false，由调用方退回通知横幅。
 */
public class OverlayAlert {
    private static final String TAG = "HFUTone";
    private static final long SHOW_MS = 4000;

    private static WindowManager wm;
    private static View view;

    /** 有没有悬浮窗权限。API 23 之前默认有。 */
    public static boolean canShow(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) { return true; }
        return Settings.canDrawOverlays(ctx);
    }

    /** 跳到「显示在其他应用上层」的授权页。 */
    public static void requestPermission(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) { return; }
        try {
            Intent i = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION);
            i.setData(android.net.Uri.parse("package:" + ctx.getPackageName()));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(i);
        } catch (Throwable t) {
            Log.w(TAG, "悬浮窗授权页打不开：" + t);
        }
    }

    /** 弹一条。返回 false 表示没权限或失败，调用方自己决定要不要退回通知。 */
    public static boolean show(Context ctx, String line) {
        if (!canShow(ctx)) { return false; }
        Context app = ctx.getApplicationContext();
        try {
            if (wm == null) {
                wm = (WindowManager) app.getSystemService(Context.WINDOW_SERVICE);
            }
            if (wm == null) { return false; }
            hide();

            LayoutInflater inf = LayoutInflater.from(app);
            View v = inf.inflate(R.layout.notify_alert, null);
            TextView name = v.findViewById(R.id.alert_name);
            TextView time = v.findViewById(R.id.alert_time);
            if (name != null) { name.setText(line); }
            if (time != null) { time.setText(""); }

            int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                    ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                    : WindowManager.LayoutParams.TYPE_PHONE;
            WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                    WindowManager.LayoutParams.MATCH_PARENT,
                    WindowManager.LayoutParams.WRAP_CONTENT,
                    type,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                            | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL
                            | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                    PixelFormat.TRANSLUCENT);
            lp.gravity = Gravity.TOP;
            lp.y = dp(app, 12);
            lp.x = dp(app, 12);
            // 左右各留 12dp：MATCH_PARENT 下用水平边距让胶囊不贴边
            lp.width = WindowManager.LayoutParams.MATCH_PARENT;

            v.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View x) {
                    hide();
                    try {
                        Intent open = new Intent(app, MainActivity.class);
                        open.setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP
                                | Intent.FLAG_ACTIVITY_SINGLE_TOP
                                | Intent.FLAG_ACTIVITY_NEW_TASK);
                        app.startActivity(open);
                    } catch (Throwable t) {
                        Log.w(TAG, "从横幅打开 App 失败：" + t);
                    }
                }
            });

            wm.addView(v, lp);
            view = v;

            new Handler(Looper.getMainLooper()).postDelayed(new Runnable() {
                @Override
                public void run() { hide(); }
            }, SHOW_MS);
            return true;
        } catch (Throwable t) {
            Log.w(TAG, "悬浮横幅失败：" + t);
            hide();
            return false;
        }
    }

    /** 收掉当前这条。重复调用安全。 */
    public static void hide() {
        View v = view;
        view = null;
        if (v == null || wm == null) { return; }
        try {
            wm.removeView(v);
        } catch (Throwable ignored) {
            // 已经被系统收掉了
        }
    }

    private static int dp(Context ctx, int v) {
        float d = ctx.getResources().getDisplayMetrics().density;
        return Math.round(v * d);
    }
}