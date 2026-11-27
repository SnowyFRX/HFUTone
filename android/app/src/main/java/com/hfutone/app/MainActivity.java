package com.hfutone.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
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

    /**
     * 电池白名单。
     *
     * 前台服务只能保证「系统愿意留你」时不被杀，国产 ROM 的省电策略照样能清掉整个
     * 进程，签到守护就此静默失效——用户还以为它在跑。这里在首次冷启动弹一次系统
     * 对话框；被拒后不再纠缠（记在 SharedPreferences），用户可随时去系统设置里改。
     */
    private void ensureBatteryWhitelist() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) { return; }
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm == null || pm.isIgnoringBatteryOptimizations(getPackageName())) { return; }
        SharedPreferences sp = getSharedPreferences("hfutone", MODE_PRIVATE);
        if (sp.getBoolean("battery_asked", false)) { return; }
        sp.edit().putBoolean("battery_asked", true).apply();
        // 别把用户直接扔进系统设置：先用自己的弹窗说清楚要干什么，他点了才跳
        web.postDelayed(this::showBatteryDialog, 900);
    }

    private void showBatteryDialog() {
        if (isFinishing()) { return; }
        new AlertDialog.Builder(this)
                .setTitle("让签到守护活过息屏")
                .setMessage("系统在息屏后会清理后台进程，签到守护会静默停止，你不会收到任何提示。\n\n"
                        + "把 HFUTone 排除出电池优化能明显降低被清理的概率。点「去设置」后系统会问一次，选「允许」。")
                .setPositiveButton("去设置", (d, w) -> openBatterySettings())
                .setNegativeButton("以后再说", null)
                .show();
    }

    /** 跳系统设置让用户把本应用从电池优化里排除。首次自动弹，设置页也能手动点。 */
    private void openBatterySettings() {
        try {
            Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            i.setData(Uri.parse("package:" + getPackageName()));
            startActivity(i);
            Log.i(TAG, "已请求忽略电池优化");
        } catch (Exception e) {
            // 部分 ROM 阉了这个 Action，退回到列表页让用户自己找
            Log.w(TAG, "电池优化对话框打不开：" + e);
            try {
                startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            } catch (Exception ignored) {
                // 打不开就算了，不能因此挡住界面
            }
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Android 13+ 通知是运行时权限：不申请的话常驻胶囊和签到横幅都不会出现
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1001);
        }

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        // WebView 默认白底：Python 起服务的那几百毫秒会先闪一屏纯白。
        // 设成页面自己的米色，冷启动看起来是一体的。
        web.setBackgroundColor(Color.parseColor("#F7F4ED"));
        web.setWebViewClient(new WebViewClient() {
            /**
             * 页面里的 http(s) 链接（比如设置里的「腾讯坐标拾取器」）一律交给系统浏览器。
             * 默认行为是在 WebView 里导航过去 —— 那会把整个 App 页面顶掉，只能杀掉重进。
             */
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                // 设置页的「关闭电池优化」走这个内部 scheme，不经过系统浏览器
                if (url != null && url.startsWith("hfutone://battery")) {
                    openBatterySettings();
                    return true;
                }
                if (url != null && (url.startsWith("http://") || url.startsWith("https://"))) {
                    try {
                        Intent it = new Intent(Intent.ACTION_VIEW, android.net.Uri.parse(url));
                        it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                        startActivity(it);
                        return true;
                    } catch (Exception ignored) {
                        // 没装浏览器：让 WebView 自己兜着
                    }
                }
                return false;
            }
        });
        setContentView(web);

        // 电池白名单提示放在首屏之后：web 已就绪，postDelayed 才有意义
        ensureBatteryWhitelist();

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