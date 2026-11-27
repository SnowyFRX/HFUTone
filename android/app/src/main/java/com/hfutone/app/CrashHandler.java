package com.hfutone.app;

import android.content.Context;

import java.io.File;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 把崩溃堆栈写进 files/crash.log。
 *
 * 存在的理由：安卓端出问题时拿不到 logcat（手机不一定连着线），只能靠用户
 * 复述"点一下通知就崩了"。有了这个文件，下一次崩溃能直接看到类名和行号。
 *
 * 只保留最近几次，别把存储写满。
 */
public class CrashHandler implements Thread.UncaughtExceptionHandler {
    private static final int MAX_BYTES = 64 * 1024;

    private final Context ctx;
    private final Thread.UncaughtExceptionHandler prev;

    public CrashHandler(Context ctx, Thread.UncaughtExceptionHandler prev) {
        this.ctx = ctx.getApplicationContext();
        this.prev = prev;
    }

    @Override
    public void uncaughtException(Thread thread, Throwable ex) {
        try {
            // 写到外部私有目录（Android/data/com.hfutone.app/files/crash.log）：
            // 连电脑或文件管理器都能拿到，比 getFilesDir() 好用。
            File dir = ctx.getExternalFilesDir(null);
            if (dir == null) { dir = ctx.getFilesDir(); }
            File f = new File(dir, "crash.log");
            if (f.exists() && f.length() > MAX_BYTES) {
                // 超了就滚动：只留后半段，别无限增长
                byte[] all = new byte[(int) f.length()];
                java.io.FileInputStream in = new java.io.FileInputStream(f);
                int n = in.read(all);
                in.close();
                java.io.FileOutputStream out = new java.io.FileOutputStream(f, false);
                out.write(all, Math.max(0, n - MAX_BYTES / 2), n - Math.max(0, n - MAX_BYTES / 2));
                out.close();
            }
            StringWriter sw = new StringWriter();
            PrintWriter pw = new PrintWriter(sw);
            pw.println("=== " + new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.CHINA)
                    .format(new Date()) + "  thread=" + thread.getName());
            ex.printStackTrace(pw);
            pw.println();
            pw.flush();

            java.io.FileOutputStream out = new java.io.FileOutputStream(f, true);
            out.write(sw.toString().getBytes("UTF-8"));
            out.close();
        } catch (Throwable ignored) {
            // 写日志本身不能再抛，否则真的死循环
        }

        if (prev != null) {
            prev.uncaughtException(thread, ex);
        }
    }
}