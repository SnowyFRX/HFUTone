package com.hfutone.app;

import com.chaquo.python.android.PyApplication;

/**
 * Chaquopy 要求在 Android 上显式初始化 Python 运行时。
 * 继承 PyApplication 就自动完成 Python.start(new AndroidPlatform(this))，
 * 否则 Python.getInstance() 会抛 "Cannot use GenericPlatform on Android"。
 */
public class App extends PyApplication {
}
