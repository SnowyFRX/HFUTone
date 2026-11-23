package com.hfutone.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

/**
 * 前台保活服务。
 *
 * 为什么必须有：签到的时间窗只有开课那几分钟，用户此时基本不会盯着这个 App
 * （在微信里等验证码、或者在别的页面）。没有前台服务时，App 退到后台就可能被
 * 系统回收，Python 里的 HTTP 服务、12 秒一轮的自动推流、签到状态全部停摆。
 *
 * 前台服务类型用 specialUse 而不是 dataSync：Android 14 起 dataSync 类型的
 * 前台服务在 24 小时内累计只能跑 6 小时，超时系统直接 onTimeout 停服务——
 * 这正好卡在"上半天的课"。specialUse 没有这个时长限制，声明用途即可。
 *
 * 服务还要负责"自愈"：进程被系统回收后 START_STICKY 会把服务重新拉起来，
 * 这时候没有 Activity，界面不会出现，但签到守护必须活着，所以这里重新
 * 走一遍 {@link Boot#ensure} 把 Python 的 HTTP 服务重新起起来。
 */
public class KeepAliveService extends Service {
    private static final String TAG = "HFUTone";
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
        Notification n = b.build();

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // API 34 起三参版本要求类型与 manifest 里的 foregroundServiceType 一致
            startForeground(NOTIFY_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIFY_ID, n);
        }

        ensurePython();
        // 被系统杀掉后自动重建，保证上课期间不静默退出
        return START_STICKY;
    }

    /** 进程重建后（没有 Activity）也要把 HTTP 服务起起来，否则签到守护是断的。 */
    private void ensurePython() {
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Boot.ensure(getApplicationContext());
                    Log.i(TAG, "守护服务：Python 服务就绪 " + Boot.url());
                } catch (Throwable t) {
                    Log.e(TAG, "守护服务：Python 启动失败", t);
                }
            }
        }, "hfutone-service-boot").start();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}