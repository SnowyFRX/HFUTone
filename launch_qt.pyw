"""HFUTone 启动壳（pythonw 运行，无控制台）。

为什么要这一层：
  - pythonw.exe 直接跑 QtWebEngine 会初始化失败（无 stdio 句柄，窗口不出现）
  - python.exe 直接跑会留一个黑控制台窗口
  - 用 pythonw 作为外壳，CREATE_NO_WINDOW 启动 python.exe 的 app_qt.pyw：
    子进程拿到有效的 stdio 句柄（转向 NUL），又不会出现控制台窗口
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP = ROOT / 'app_qt.pyw'
CREATE_NO_WINDOW = 0x08000000


def main() -> int:
    py = Path(sys.executable).with_name('python.exe')
    if not py.exists():
        py = Path(sys.executable)
    # CREATE_NO_WINDOW 下子进程的 stdio 句柄是无效的，QtWebEngine 初始化时写日志会崩；
    # 显式重定向到 NUL，句柄有效但看不到窗口
    subprocess.Popen(
        [str(py), str(APP)],
        cwd=str(ROOT),
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
