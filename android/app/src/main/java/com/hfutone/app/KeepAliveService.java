package com.hfutone.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

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
 *
 * 通知栏做成一条「胶囊」（RemoteViews），后台时一眼看到守护状态：
 * 圆点是 IM 在线与否，主行是守护开关，副行是兜底间隔，右侧是已签/待办数。
 * 数据从本地 HTTP 服务读，20 秒一轮，失败了也不出声。
 */
public class KeepAliveService extends Service {
    private static final String TAG = "HFUTone";
    private static final String CHANNEL = "hfutone_status";
    private static final String EVENT_CHANNEL = "hfutone_event";
    private static final int NOTIFY_ID = 1;
    /** 事件横幅用独立的 id 段，绝不能和常驻那条撞 */
    private static final int EVENT_ID_BASE = 100;
    /**
     * 状态轮询间隔。
     *
     * 原来是 20 秒，但那意味着签到成功后最多要等 20 秒才弹横幅。本地回环请求
     * 开销可以忽略，压到 5 秒，横幅基本是"签完就跳出来"。
     */
    private static final long TICK_MS = 5000;

    private Handler handler;
    private NotificationManager nm;
    /** 上次看到的事件 id；-1 表示还没初始化（第一次只记起点，不补弹历史） */
    private long lastEventId = -1;

    // 胶囊上显示的四个字段，由 fetchState() 刷新
    private String title = "签到守护";
    private String detail = "正在连接…";
    private String tail = "";
    private int dot = R.drawable.island_dot_idle;

    @Override
    public void onCreate() {
        super.onCreate();
        handler = new Handler(Looper.getMainLooper());
        nm = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL, "运行状态", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            NotificationManager m = getSystemService(NotificationManager.class);
            if (m != null && m.getNotificationChannel(CHANNEL) == null) {
                m.createNotificationChannel(ch);
            }

            // 签到成功要弹横幅（heads-up），必须单独一个 HIGH 通道：
            // IMPORTANCE 建完就不能改，所以不能和常驻那条共用。
            NotificationChannel ev = new NotificationChannel(
                    EVENT_CHANNEL, "签到提醒", NotificationManager.IMPORTANCE_HIGH);
            ev.setShowBadge(true);
            ev.enableVibration(true);
            if (m != null && m.getNotificationChannel(EVENT_CHANNEL) == null) {
                m.createNotificationChannel(ev);
            }
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // API 34 起三参版本要求类型与 manifest 里的 foregroundServiceType 一致
            startForeground(NOTIFY_ID, buildNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIFY_ID, buildNotification());
        }

        ensurePython();
        scheduleTick();
        // 被系统杀掉后自动重建，保证上课期间不静默退出
        return START_STICKY;
    }

    /** 胶囊本体。折叠态高度固定，四个元素必须都在一行内。 */
    private Notification buildNotification() {
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            b = new Notification.Builder(this, CHANNEL);
        } else {
            //noinspection deprecation
            b = new Notification.Builder(this);
        }

        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int piFlag = Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                ? PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
                : PendingIntent.FLAG_UPDATE_CURRENT;
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, piFlag);

        //noinspection deprecation
        b.setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle(title)
                .setContentText(detail)
                .setContentIntent(pi)
                .setOngoing(true)
                .setShowWhen(false)
                .setPriority(Notification.PRIORITY_LOW);

        try {
            RemoteViews v = new RemoteViews(getPackageName(), R.layout.notify_island);
            v.setTextViewText(R.id.island_title, title);
            v.setTextViewText(R.id.island_detail, detail);
            v.setTextViewText(R.id.island_tail, tail);
            v.setImageViewResource(R.id.island_dot, dot);
            v.setOnClickPendingIntent(R.id.island_title, pi);
            b.setCustomContentView(v);
        } catch (Throwable t) {
            // 个别 ROM 拦自定义布局，退回系统默认样式，别让服务起不来
            Log.w(TAG, "胶囊布局不可用，用默认通知：" + t);
        }
        return b.build();
    }

    /**
     * 从本地 HTTP 服务读一次守护状态，刷新胶囊。
     *
     * 请求走 127.0.0.1，不经过网络；失败什么都不做——通知栏不该因为一次读失败
     * 就变成错误状态，用户会以为守护挂了。
     */
    private void fetchState() {
        String base = Boot.url();
        if (base.isEmpty()) { return; }
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    String body = httpGet(base + "api/autosign/state");
                    JSONObject o = new JSONObject(body);
                    applyState(o);
                } catch (Throwable t) {
                    Log.w(TAG, "读守护状态失败：" + t);
                }
            }
        }, "hfutone-island").start();
    }

    /** 把状态拼成一行文案。字段见 src/autosign.py 的 status_payload()。 */
    private void applyState(JSONObject o) {
        boolean running = o.optBoolean("running", false);
        boolean cx = o.optBoolean("cxAutoSign", false);
        boolean ykt = o.optBoolean("yktAutoSign", false);
        boolean im = o.optBoolean("imConnected", false);
        int eff = o.optInt("effectiveCxPollSec", 180);
        JSONObject st = o.optJSONObject("stats");
        int signed = st == null ? 0 : st.optInt("signed", 0);
        int need = st == null ? 0 : st.optInt("needAction", 0);

        final String t;
        final String d;
        final String tl;
        final int dt;
        if (!running || (!cx && !ykt)) {
            t = "签到守护未开启";
            d = "去设置里打开学习通或雨课堂自动签到";
            tl = "";
            dt = R.drawable.island_dot_idle;
        } else {
            t = "签到守护中";
            StringBuilder sb = new StringBuilder();
            sb.append("学习通 ").append(cx ? "开" : "关");
            sb.append(" · 雨课堂 ").append(ykt ? "开" : "关");
            sb.append(im ? " · IM 在线" : (" · IM 重连 · 兜底 " + eff + "s"));
            d = sb.toString();
            tl = need > 0 ? (need + " 待办") : (signed > 0 ? ("已签 " + signed) : "");
            dt = im ? R.drawable.island_dot_ok : R.drawable.island_dot_err;
        }

        if (handler == null || nm == null) { return; }
        // 事件数组是倒序的（最新在前），在主线程里一起处理，省得两处抢
        final JSONArray evs = o.optJSONArray("events");
        // 字段在主线程写，后台线程只负责算——省得读到半更新的状态
        handler.post(new Runnable() {
            @Override
            public void run() {
                title = t;
                detail = d;
                tail = tl;
                dot = dt;
                try {
                    nm.notify(NOTIFY_ID, buildNotification());
                } catch (Throwable ex) {
                    Log.w(TAG, "刷新通知失败：" + ex);
                }
                checkEvents(evs);
            }
        });
    }

    /**
     * 扫一遍事件，有新的"签到成功"就弹横幅。
     *
     * 第一次读到事件表时只记下最大的 id 当起点——否则冷启动会把历史签到
     * 全部弹一遍，像刷屏。
     */
    private void checkEvents(JSONArray evs) {
        if (evs == null || evs.length() == 0) { return; }
        if (lastEventId < 0) {
            JSONObject newest = evs.optJSONObject(0);
            lastEventId = newest == null ? 0 : newest.optLong("id", 0);
            return;
        }
        // 倒序遍历：从最旧的往后走，保证连签几条时横幅按时间顺序弹
        for (int i = evs.length() - 1; i >= 0; i--) {
            JSONObject e = evs.optJSONObject(i);
            if (e == null) { continue; }
            long id = e.optLong("id", 0);
            if (id <= lastEventId) { continue; }
            lastEventId = id;
            if ("signed".equals(e.optString("status"))) {
                alertSigned(e);
            }
        }
    }

    /** 签到成功的横幅（heads-up）。点它回到 App。 */
    private void alertSigned(JSONObject e) {
        if (nm == null) { return; }
        String name = e.optString("name", "签到");
        String detail = e.optString("detail", "");
        String who = name + (detail.isEmpty() ? "" : " · " + detail);

        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            b = new Notification.Builder(this, EVENT_CHANNEL);
        } else {
            //noinspection deprecation
            b = new Notification.Builder(this);
        }

        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        int piFlag = Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                ? PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
                : PendingIntent.FLAG_UPDATE_CURRENT;
        PendingIntent pi = PendingIntent.getActivity(this, 1, open, piFlag);

        //noinspection deprecation
        b.setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("已自动签到")
                .setContentText(who)
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setPriority(Notification.PRIORITY_HIGH)
                .setDefaults(Notification.DEFAULT_ALL);

        // 横幅也用自定义布局，和常驻胶囊同一套视觉：深底、粗标题、灰副行、等宽时间
        try {
            RemoteViews v = new RemoteViews(getPackageName(), R.layout.notify_alert);
            v.setTextViewText(R.id.alert_name, who);
            v.setTextViewText(R.id.alert_time, e.optString("time", ""));
            v.setOnClickPendingIntent(R.id.alert_name, pi);
            b.setCustomContentView(v);
            b.setCustomBigContentView(v);
        } catch (Throwable t) {
            Log.w(TAG, "横幅布局不可用，用默认样式：" + t);
        }

        try {
            int id = EVENT_ID_BASE + (int) (e.optLong("id", 0) % 1000);
            nm.notify(id, b.build());
        } catch (Throwable t) {
            Log.w(TAG, "弹签到横幅失败：" + t);
        }
    }

    private void scheduleTick() {
        if (handler == null) { return; }
        handler.removeCallbacks(ticker);
        handler.postDelayed(ticker, TICK_MS);
    }

    private final Runnable ticker = new Runnable() {
        @Override
        public void run() {
            fetchState();
            scheduleTick();
        }
    };

    /** 最小的 GET，只要一个字符串回来。连不上就抛，由调用方吞掉。 */
    private static String httpGet(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(3000);
        c.setReadTimeout(5000);
        c.setRequestMethod("GET");
        try {
            InputStream in = c.getInputStream();
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] chunk = new byte[4096];
            int n;
            while ((n = in.read(chunk)) > 0) { buf.write(chunk, 0, n); }
            in.close();
            return buf.toString(StandardCharsets.UTF_8.name());
        } finally {
            c.disconnect();
        }
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
    public void onDestroy() {
        if (handler != null) { handler.removeCallbacks(ticker); }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}