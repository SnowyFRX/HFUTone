"""HFUTone 原生窗口版（PyQt5 + QtWebEngine）。

和原版 rtmp_gui.exe 一样：双击就是一个独立窗口，没有浏览器外壳，
不依赖 Edge 的 app 模式、profile、代理设置。

进程内结构：
    QApplication
      └ QMainWindow
          └ QWebEngineView  → http://127.0.0.1:<port>/
    后台线程：ThreadingHTTPServer（复用 web/server.py 的路由与状态）

播放仍走原逻辑：点「开启直播」后由服务端直接拉起 mpv 独立窗口；
关闭本窗口时会顺手结束这些播放器，避免留下孤儿进程。
"""
from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

# pythonw.exe 下 sys.stdout/stderr 为 None，QtWebEngine（Chromium）初始化时会往
# 这两个流写日志，句柄缺失会直接初始化失败、窗口不出现。先接上 devnull。
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

ROOT = Path(__file__).resolve().parent          # .../HFUTone（打包后为 _MEIPASS）
WEB = ROOT / 'web'
sys.path.insert(0, str(WEB))

# 打包版：可写数据（配置/缓存）放在 exe 旁边，便携运行；
# 资源文件仍在 PyInstaller 的 _MEIPASS 里，由 ROOT 指向。
FROZEN = bool(getattr(sys, 'frozen', False))
BASE = Path(sys.executable).resolve().parent if FROZEN else ROOT

# QtWebEngine 在部分虚拟/远程环境里 GPU 初始化失败会白屏，先禁用 GPU 合成
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--ignore-gpu-blocklist --enable-gpu-rasterization')
os.environ.setdefault('QTWEBENGINE_DISABLE_SANDBOX', '1')
# 用户目录写不进去（安全软件锁了 LocalLow），缓存改放到可写目录，避免每次启动迁移失败
os.environ.setdefault('QTWEBENGINE_DISK_CACHE', str(BASE / '.qtcache'))

from PyQt5.QtCore import Qt, QUrl, QEvent       # noqa: E402
from PyQt5.QtGui import QKeySequence, QIcon     # noqa: E402
from PyQt5.QtWidgets import QShortcut           # noqa: E402
from PyQt5.QtWidgets import (QApplication, QMainWindow, QMessageBox)  # noqa: E402
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineProfile  # noqa: E402

import server as sv                             # noqa: E402  复用 web/server.py


def start_server() -> int:
    """在后台线程起 HTTP 服务，返回实际端口。"""
    port = sv.pick_port(sv.DEFAULT_PORT)
    srv = sv.LocalServer(('127.0.0.1', port), sv.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=sv.STATE.load, daemon=True).start()
    sv.YKT.start_monitor()
    sv.CX_IM.start()
    sv.AUTOSIGN.start()
    return port


class MainWindow(QMainWindow):
    def __init__(self, url: str, icon_path: Path | None):
        super().__init__()
        self.setWindowTitle('HFUTone · 直播课堂与雨课堂')
        self.resize(1500, 940)
        self.setMinimumSize(900, 600)

        if icon_path and icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.view = QWebEngineView(self)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)   # 去掉浏览器右键菜单
        self.view.setUrl(QUrl(url))
        self.setCentralWidget(self.view)
        # 自检：确认页面真正渲染（不只是窗口标题），结果打到 stderr
        self.view.loadFinished.connect(self._on_load)

        # 改样式后按 F5 / Ctrl+R 刷新，不用重启整个程序
        for seq in ('F5', 'Ctrl+R'):
            QShortcut(QKeySequence(seq), self,
                      activated=self.view.reload)

    @staticmethod
    def _log(msg: str) -> None:
        try:
            if sys.stderr is not None:
                print(msg, file=sys.stderr, flush=True)
        except Exception:                      # noqa: BLE001
            pass

    def _on_load(self, ok: bool) -> None:
        self._log(f'[qt] loadFinished={ok}')
        if not ok:
            return
        self.view.page().runJavaScript('document.title', lambda t: self._log(f'[qt] doc.title={t!r}'))
        self.view.page().runJavaScript(
            'document.getElementById("rows") ? document.getElementById("rows").children.length : -1',
            lambda n: self._log(f'[qt] rows={n}'))

    def changeEvent(self, event):              # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            active = 'true' if not self.isMinimized() else 'false'
            self.view.page().runJavaScript(
                f'window.HFUTLive && window.HFUTLive.setActive({active})')

    def closeEvent(self, event):              # noqa: N802
        # 结束本进程拉起的播放器，别留下孤儿 mpv / VLC 窗口
        with sv.STATE.lock:
            pids, sv.STATE.player_pids = sv.STATE.player_pids, []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        # 停掉 PPT 抽帧的 ffmpeg：它一直在读 exe 目录下的二进制，
        # 不停就会把 dist 锁住（下次打包/删除直接 PermissionError）。
        try:
            sv.PPT_RELAY.stop()
        except Exception:                     # noqa: BLE001
            pass
        self.view.stop()
        super().closeEvent(event)


def main() -> int:
    # 打包版自带 mpv（exe 同级 mpv/mpv.exe），优先用它；
    # 不在 PATH、也不需要对方装任何播放器。HFUT_PLAYER 已设置时不覆盖。
    if FROZEN:
        bundled_mpv = BASE / 'mpv' / 'mpv.exe'
        if bundled_mpv.is_file() and not os.environ.get('HFUT_PLAYER'):
            os.environ['HFUT_PLAYER'] = str(bundled_mpv)

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

    app = QApplication(sys.argv)
    app.setApplicationName('HFUTone')
    # 本地静态文件由服务端 no-store + URL 版本号控制，不再使用 Chromium 磁盘缓存，
    # 避免 index.html 与 app.js 版本错位导致按钮事件绑定异常。
    QWebEngineProfile.defaultProfile().setHttpCacheType(QWebEngineProfile.NoCache)

    try:
        port = start_server()
    except Exception as e:                   # noqa: BLE001
        QMessageBox.critical(None, 'HFUTone', f'本地服务启动失败：\n{e}')
        return 1

    icon = ROOT / 'assets' / 'hfutlive-hd.png'
    win = MainWindow(f'http://127.0.0.1:{port}/', icon)
    win.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
