package com.hfutone.app;

import android.os.Bundle;

import com.chaquo.python.android.PyApplication;

/**
 * Chaquopy 要求在 Android 上显式初始化 Python 运行时。
 * 继承 PyApplication 就自动完成 Python.start(new AndroidPlatform(this))，
 * 否则 Python.getInstance() 会抛 "Cannot use GenericPlatform on Android"。
 *
 * 顺带挂一个崩溃日志钩子：安卓端拿不到 logcat 时，files/crash.log 是唯一线索。
 */
public class App extends PyApplication {
    @Override
    public void onCreate() {
        super.onCreate();
        Thread.UncaughtExceptionHandler prev = Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler(new CrashHandler(this, prev));
    }
}