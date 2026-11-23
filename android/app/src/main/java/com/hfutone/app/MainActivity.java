package com.hfutone.app;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/**
 * 原生外壳。
 *
 * 职责只有三件：把 apk 里 assets/web 解压到应用私有目录、拉起内嵌 Python 跑桌面版
 * 那套 HTTP 服务、用 WebView 打开本地地址。业务逻辑与界面全部复用桌面版源码，
 * 这样两边功能始终一致，也避免把 5000 行 Python 重写成 Java。
 *
 * 解压 + 起服务这一步抽在 {@link Boot} 里：KeepAliveService 在进程被回收后
 * 单独重启时也要走同一套逻辑，否则界面不出现、HTTP 服务也没起，签到守护就是断的。
 */
public class MainActivity extends Activity {
    private static final String TAG = "HFUTone";
    private WebView web;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        // WebView 默认白底：Python 起服务的那几百毫秒会先闪一屏纯白。
        // 设成页面自己的米色，冷启动看起来是一体的。
        web.setBackgroundColor(Color.parseColor("#F7F4ED"));
        web.setWebViewClient(new WebViewClient());
        setContentView(web);

        // debug 包才开 WebView 调试：桌面 chrome://inspect 能直接看到页面控制台，
        // 前端布局/接口问题不用每次 adb logcat 猜。release 包保持关闭。
        if ((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0) {
            WebView.setWebContentsDebuggingEnabled(true);
        }

        // 前台服务：保证退到后台后 Python 服务与自动签到继续活着
        Intent svc = new Intent(this, KeepAliveService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(svc);
        } else {
            startService(svc);
        }

        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Boot.ensure(MainActivity.this);
                    final String url = Boot.url();
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            web.loadUrl(url);
                        }
                    });
                } catch (Throwable t) {
                    Log.e(TAG, "启动失败", t);
                    final String msg = String.valueOf(t);
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            web.loadData("<meta name=viewport content='width=device-width'>"
                                    + "<h3>启动失败</h3><pre style='white-space:pre-wrap'>"
                                    + msg.replace("&", "&amp;").replace("<", "&lt;")
                                    + "</pre>", "text/html", "utf-8");
                        }
                    });
                }
            }
        }, "hfutone-boot").start();
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        // 刻意不 shutdown Python：Activity 退出只是界面没了，签到守护还要继续跑
        // （KeepAliveService 常驻前台，HTTP 服务与两条监控线程都在这个进程里）。
        // 真要停，只能由用户划掉最近任务或系统回收进程。
        super.onDestroy();
    }
}