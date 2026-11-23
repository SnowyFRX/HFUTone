package com.hfutone.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

/**
 * 前台保活服务。
 *
 * 为什么必须有：签到的时间窗只有开课那几分钟，用户此时基本不会盯着这个 App
 * （在微信里等验证码、或者在别的页面）。没有前台服务时，App 退到后台就可能被
 * 系统回收，Python 里的 HTTP 服务、12 秒一轮的自动推流、签到状态全部停摆。
 *
 * 这里只做一件事：挂一个常驻通知把自己钉在前台，Python 侧的逻辑不受影响。
 */
public class KeepAliveService extends Service {
    private static final String CHANNEL = "hfutone_status";
    private static final int NOTIFY_ID = 1;

    @Override
    public void onCreate() {
        super.onCreate();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "运行状态", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null && nm.getNotificationChannel(CHANNEL) == null) {
                nm.createNotificationChannel(ch);
            }
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            b = new Notification.Builder(this, CHANNEL);
        } else {
            //noinspection deprecation
            b = new Notification.Builder(this);
        }
        //noinspection deprecation
        b.setContentTitle("HFUTone")
                .setContentText("雨课堂 / 学习通签到守护中")
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true)
                .setShowWhen(false);
        startForeground(NOTIFY_ID, b.build());
        // 被系统杀掉后自动重建，保证上课期间不静默退出
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}