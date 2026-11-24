# -*- mode: python ; coding: utf-8 -*-
"""HFUTone 打包配置（PyInstaller 目录模式）。

一个客户端，两个功能：
  · 直播课堂（原 HFUTlive，已并入本目录：PyQt5 + QtWebEngine + 本地 HTTP 服务 + mpv）
  · 雨课堂（原 ykt_ui「冰冰冰糖」C# 版，已用 Python 重写协议层，TLS 走内置 LibreSSL curl）

产物 dist/HFUTone：
  HFUTone.exe         主程序（无控制台）
  _internal/          运行时（PyQt5 / QtWebEngine / web 静态资源 / vendor/curl）
  mpv/                由 build_release.py 事后复制（内置播放器）

注意：web/server.py、src/*.py 是运行时插入 sys.path 导入的，PyInstaller
静态分析看不到，必须用 pathex + hiddenimports 显式指明。
"""
from pathlib import Path

ROOT = Path(SPECPATH)          # noqa: F821  PyInstaller 注入
WEB = ROOT / 'web'
SRC = ROOT / 'src'
CURL = ROOT / 'vendor' / 'curl'

a = Analysis(                  # noqa: F821
    [str(ROOT / 'app_qt.pyw')],
    pathex=[str(WEB), str(SRC)],
    binaries=[],
    datas=[
        # 整个 web/ 都带上：早先逐个数文件名，漏掉 mobile.css / mobile.js，
        # 结果 index.html 里那两条引用一直是 404（client-errors.log 里刷屏），
        # 移动端布局也随之失效。用 glob 收全，以后加文件不用再改这里。
        (str(p), 'web') for p in WEB.glob('*') if p.is_file()
    ] + [
        # web/im/Easemob.js（环信 Web SDK）：子目录也要带上，glob('*') 只扫顶层
        (str(p), 'web/im') for p in (WEB / 'im').glob('*') if p.is_file()
    ] + [
        # 内置 curl：LibreSSL 版，学校网关按 TLS 指纹拦 HTML 时必须走它
        (str(CURL / name), 'vendor/curl')
        for name in ('curl.exe.gz', 'libcurl-x64.dll.gz', 'cacert.pem.gz', 'manifest.txt')
    ] + [
        # ffmpeg：网页里没有 H.264 解码器，PPT 流得靠它抽成 JPEG 帧
        (str(ROOT / 'vendor' / 'ffmpeg' / 'ffmpeg.exe'), 'vendor/ffmpeg'),
    ] + [(str(ROOT / 'assets' / 'hfutlive-hd.png'), 'assets')],
    hiddenimports=['server', 'hfutlive', 'yktcore', 'yktapi', 'yktws',
                   'yktapplogin', 'yktqr', 'hfutplay', 'wsserve',
                   'cxapi', 'cxsign', 'cxcrypto', 'cxdes', 'cxchat'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest', 'lib2to3'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)              # noqa: F821

exe = EXE(                     # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='HFUTone',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,             # 不弹黑控制台
    disable_windowed_traceback=False,
    icon=str(ROOT / 'assets' / 'hfutlive.ico'),
    version=str(ROOT / 'version_info.txt'),
)

coll = COLLECT(                # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='HFUTone',
)