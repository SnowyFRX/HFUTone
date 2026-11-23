package com.hfutone.app;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.chaquo.python.Python;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * 原生外壳。
 *
 * 职责只有三件：把 apk 里 assets/web 解压到应用私有目录、拉起内嵌 Python 跑桌面版
 * 那套 HTTP 服务、用 WebView 打开本地地址。业务逻辑与界面全部复用桌面版源码，
 * 这样两边功能始终一致，也避免把 5000 行 Python 重写成 Java。
 */
public class MainActivity extends Activity {
    private static final String TAG = "HFUTone";
    private WebView web;
    private int port = 0;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        web.setWebViewClient(new WebViewClient());
        setContentView(web);

        // 前台服务：保证退到后台后 Python 服务与自动签到继续活着
        Intent svc = new Intent(this, KeepAliveService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(svc);
        } else {
            startService(svc);
        }

        String filesDirTmp;
        try {
            // 用规范路径：/data/user/0 是 /data/data 的符号链接，传规范路径可以
            // 让 Python 侧拼出来的目录和 resolve() 的结果一致
            filesDirTmp = getFilesDir().getCanonicalPath();
        } catch (IOException e) {
            filesDirTmp = getFilesDir().getAbsolutePath();
        }
        final String filesDir = filesDirTmp;
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    File base = new File(filesDir);
                    File webDir = new File(base, "web");
                    File configDir = new File(base, "config");
                    if (!configDir.exists()) {
                        //noinspection ResultOfMethodCallIgnored
                        configDir.mkdirs();
                    }
                    long t0 = System.currentTimeMillis();
                    copyAssetDir("web", webDir);
                    Log.i(TAG, "assets/web 解压完成 " + (System.currentTimeMillis() - t0) + "ms -> " + webDir);

                    int p = Python.getInstance()
                            .getModule("android_boot")
                            .callAttr("boot", configDir.getAbsolutePath(), webDir.getAbsolutePath())
                            .toJava(Integer.class);
                    port = p;
                    final String url = "http://127.0.0.1:" + p + "/";
                    Log.i(TAG, "python 服务就绪 " + url);
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

    /** 把 assets 下的一个目录整棵复制出来；assets 里的文件不能用 open() 读，必须先落地。 */
    private void copyAssetDir(String assetPath, File dest) throws IOException {
        String[] items = getAssets().list(assetPath);
        if (items == null || items.length == 0) {
            InputStream in = null;
            try {
                in = getAssets().open(assetPath);
                File parent = dest.getParentFile();
                if (parent != null && !parent.exists()) {
                    //noinspection ResultOfMethodCallIgnored
                    parent.mkdirs();
                }
                OutputStream out = new FileOutputStream(dest);
                try {
                    byte[] buf = new byte[16384];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                    }
                } finally {
                    out.close();
                }
            } catch (IOException e) {
                // 空目录：assets 里建立不了空目录，这里当目录处理
                //noinspection ResultOfMethodCallIgnored
                dest.mkdirs();
            } finally {
                if (in != null) {
                    try {
                        in.close();
                    } catch (IOException ignored) {
                    }
                }
            }
            return;
        }
        //noinspection ResultOfMethodCallIgnored
        dest.mkdirs();
        for (String name : items) {
            copyAssetDir(assetPath + "/" + name, new File(dest, name));
        }
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
        try {
            if (port != 0) {
                Python.getInstance().getModule("android_boot").callAttr("shutdown");
            }
        } catch (Throwable ignored) {
        }
        super.onDestroy();
    }
}