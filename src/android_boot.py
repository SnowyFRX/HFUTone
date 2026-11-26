"""android_boot.py —— Android 侧的唯一入口。

桌面版入口是 app_qt.pyw（Qt 窗口 + 本地服务）；Android 上没有 Qt：
界面交给 WebView，这里只负责把桌面版那套 HTTP 服务原样跑在 127.0.0.1。

三件事必须在这里做完，且**必须在 import server 之前**——server 模块在导入时
就会创建 YKT/CX 状态对象，那时读的配置目录、网页目录已经定死了：

  1. YKT_CONFIG_DIR  指到应用私有目录（files/config）。App 的 assets 与源码树
     都是只读的，桌面版那套「项目内 .config」在这里写不进去。
  2. HFUT_WEB_DIR    指到启动时从 apk assets 解压出来的网页目录。
  3. YKT_NO_CURL=1   强制走纯 Python 传输层：Android 上没有 curl 可执行文件，
     而整套业务逻辑的 HTTP 都经过 yktcore.request / fetch_bytes。
"""
from __future__ import annotations

import os
import threading

_state = {'port': 0, 'srv': None}


def boot(config_dir: str, web_dir: str = '', port: int = 0) -> int:
    """起 HTTP 服务并返回实际监听端口。重复调用返回同一个端口。"""
    if _state['port']:
        return int(_state['port'])

    os.environ.setdefault('YKT_CONFIG_DIR', config_dir)
    if web_dir:
        os.environ.setdefault('HFUT_WEB_DIR', web_dir)
    os.environ.setdefault('YKT_NO_CURL', '1')

    for sub in ('logs', 'bin'):
        try:
            os.makedirs(os.path.join(config_dir, sub), exist_ok=True)
        except OSError:
            pass

    import server                                   # noqa: E402  必须在环境变量之后

    # 端口可能还被上一次进程的 socket 占着：Android 的 force-stop 不走 onDestroy，
    # 监听 socket 直到内核回收才释放，而桌面版的 LocalServer 又刻意不允许复用地址。
    # 所以这里顺着往后找第一个能绑上的端口，把真实端口返回给原生侧。
    base = int(port or 8765)
    last: OSError | None = None
    srv = None
    for p in range(base, base + 20):
        try:
            srv = server.LocalServer(('127.0.0.1', p), server.Handler)
            break
        except OSError as exc:
            last = exc
    if srv is None:
        raise last or OSError('没有可用端口')

    threading.Thread(target=srv.serve_forever, name='hfut-http', daemon=True).start()
    server.YKT.start_monitor()
    server.CX_IM.start()
    server.AUTOSIGN.start()
    _state['srv'] = srv
    _state['port'] = int(srv.server_address[1])
    return _state['port']


def shutdown() -> None:
    srv = _state.get('srv')
    if srv is not None:
        try:
            srv.shutdown()
        except Exception:                            # noqa: BLE001
            pass
        _state['srv'] = None
        _state['port'] = 0


def info() -> dict:
    """给原生侧做诊断用。"""
    return {
        'port': _state['port'],
        'configDir': os.environ.get('YKT_CONFIG_DIR', ''),
        'webDir': os.environ.get('HFUT_WEB_DIR', ''),
        'noCurl': os.environ.get('YKT_NO_CURL', ''),
    }