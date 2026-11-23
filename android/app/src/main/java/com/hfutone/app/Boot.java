package com.hfutone.app;

import android.content.Context;
import android.util.Log;

import com.chaquo.python.Python;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * 启动闸门：把 apk 里的 assets/web 解压到私有目录，再拉起 Python 侧那套 HTTP 服务。
 *
 * 抽成一个类是因为现在有两个入口都要它：
 *   - MainActivity：正常打开 App，起完服务再 loadUrl；
 *   - KeepAliveService：进程被系统回收后 START_STICKY 把服务拉起来，此时没有
 *     Activity，界面不会出现，但签到守护必须在——12 秒一轮的自动推流和 25 秒
 *     一轮的签到盯盘都在 Python 侧，服务没起就等于守护断了。
 *
 * 端口只算一次。force-stop 不走 onDestroy，旧 socket 可能还占着 8765，
 * android_boot.boot() 会自己往后顺延，这里只负责把实际端口缓存下来复用。
 */
public final class Boot {
    private static final String TAG = "HFUTone";
    private static final Object LOCK = new Object();
    private static int port = 0;

    private Boot() {
    }

    /** 已经起过就返回同一个端口；没起过就同步起一次（可能耗时几百毫秒）。 */
    public static int ensure(Context ctx) throws IOException {
        synchronized (LOCK) {
            if (port != 0) {
                return port;
            }
            Context app = ctx.getApplicationContext();

            String base;
            try {
                // 用规范路径：/data/user/0 是 /data/data 的符号链接，传规范路径可以
                // 让 Python 侧拼出来的目录和 resolve() 的结果一致
                base = app.getFilesDir().getCanonicalPath();
            } catch (IOException e) {
                base = app.getFilesDir().getAbsolutePath();
            }

            File webDir = new File(base, "web");
            File configDir = new File(base, "config");
            if (!configDir.exists()) {
                //noinspection ResultOfMethodCallIgnored
                configDir.mkdirs();
            }

            // assets 里的文件不能用 open() 读，必须整棵复制到可读可写的私有目录
            long t0 = System.currentTimeMillis();
            copyAssetDir(app, "web", webDir);
            Log.i(TAG, "assets/web 解压完成 " + (System.currentTimeMillis() - t0) + "ms -> " + webDir);

            int p = Python.getInstance()
                    .getModule("android_boot")
                    .callAttr("boot", configDir.getAbsolutePath(), webDir.getAbsolutePath())
                    .toJava(Integer.class);
            port = p;
            Log.i(TAG, "python 服务就绪 http://127.0.0.1:" + p + "/");
            return port;
        }
    }

    public static String url() {
        return port == 0 ? "" : "http://127.0.0.1:" + port + "/";
    }

    /** 把 assets 下的一个目录整棵复制出来。 */
    private static void copyAssetDir(Context ctx, String assetPath, File dest) throws IOException {
        String[] items = ctx.getAssets().list(assetPath);
        if (items == null || items.length == 0) {
            InputStream in = null;
            try {
                in = ctx.getAssets().open(assetPath);
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
            copyAssetDir(ctx, assetPath + "/" + name, new File(dest, name));
        }
    }
}