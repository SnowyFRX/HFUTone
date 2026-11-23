"""hfutplay —— 把一路直播流解成 JPEG 帧，供网页内嵌显示。

为什么不用 `<video src="...m3u8">`：QtWebEngine 5.15（Chromium 83）没有编解码器
许可，`canPlayType('video/mp4; codecs="avc1.42E01E"')` 返回空串，MSE 也不支持
avc1/aac —— 学校的流全是 H.264 + AAC，网页直接播必然是黑框。

所以把解码放到 ffmpeg 里：ffmpeg 拉流 → 转成 MJPEG 序列 → 前端 WebSocket
收帧 → canvas 画。网页只负责解 JPEG（浏览器自带，且不缩分辨率），PPT 那种
小字画面不会糊；延迟只有一帧。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import hfutlive as HfutLive                 # 复用它的目录约定

BASE = Path(__file__).resolve().parent.parent      # HFUTone/


# ------------------------------------------------------------------ ffmpeg 定位

def find_ffmpeg(force: bool = False) -> str | None:
    """找一个能用的 ffmpeg。

    顺序：环境变量 → vendor（打包自带）→ .config/bin（首次使用时解出来的）
    → 开发机上的 tools/ → PATH。绿色版工具常躺在仓库里，所以要扫目录。
    """
    cands: list[str] = []
    env = os.environ.get('HFUT_FFMPEG') or ''
    if env:
        cands.append(env)
    meipass = getattr(sys, '_MEIPASS', '')          # 打包后静态资源在这里
    if meipass:
        cands.append(str(Path(meipass) / 'vendor' / 'ffmpeg' / 'ffmpeg.exe'))
    cands.append(str(BASE / 'vendor' / 'ffmpeg' / 'ffmpeg.exe'))
    cands.append(str(BASE / 'vendor' / 'ffmpeg.exe'))
    cands.append(str(HfutLive.CONFIG_DIR / 'bin' / 'ffmpeg.exe'))
    cands.append(str(BASE.parent / 'tools' / 'ffmpeg.exe'))
    cands.append(str(BASE / 'tools' / 'ffmpeg.exe'))
    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except OSError:
            continue
    return shutil.which('ffmpeg')


# --------------------------------------------------------------------- 帧解析

def jpeg_size(buf: bytes) -> tuple[int, int]:
    """从 JPEG 的 SOF 段读宽高，用来把 canvas 内部分辨率对齐到源。"""
    i = 2
    n = len(buf)
    while i + 9 < n:
        if buf[i] != 0xFF:
            i += 1
            continue
        marker = buf[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = (buf[i + 5] << 8) | buf[i + 6]
            w = (buf[i + 7] << 8) | buf[i + 8]
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        seg = (buf[i + 2] << 8) | buf[i + 3]
        i += 2 + seg
    return 0, 0


# ------------------------------------------------------------------- 转发器

class PptRelay:
    """单路 PPT 流的抽帧广播。同一时间只跑一路，够用且省 CPU。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._frame = b''
        self._frame_at = 0.0          # 这一帧从 ffmpeg 管道读出来的时刻
        self._size = (0, 0)
        self._src = ''
        self._title = ''
        self._fps = 20.0
        self._quality = 4
        self._frames_total = 0
        self._started_at = 0.0
        self._t0 = 0.0
        self._err = ''
        self._stopping = False
        self._manual = False          # 手动拉的画面，自动推流不许擅自关掉

    # ---------------------------------------------------------------- 状态
    @property
    def manual(self) -> bool:
        return self._manual

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def snapshot(self) -> dict:
        with self._cond:
            elapsed = time.time() - self._t0 if self._t0 else 0
            fps = (self._frames_total / elapsed) if elapsed > 1 else self._fps
            return {
                'running': self.running,
                'src': self._src,
                'title': self._title,
                'seq': self._seq,
                'width': self._size[0],
                'height': self._size[1],
                'fps': round(fps, 1),
                'frames': self._frames_total,
                'seconds': int(elapsed),
                'manual': self._manual,
                'error': self._err,
            }

    # ---------------------------------------------------------------- 生命周期
    def start(self, url: str, title: str = '', fps: float = 20.0,
              quality: int = 4, manual: bool = False) -> dict:
        """起一路转码。已经在跑同一路就直接复用。"""
        with self._cond:
            if self.running and url == self._src:
                return {'ok': True, 'reused': True, **self.snapshot()}
        self.stop()
        ff = find_ffmpeg()
        if not ff:
            return {'ok': False, 'error': '没有找到 ffmpeg（vendor/ffmpeg 缺失）'}
        if not url:
            return {'ok': False, 'error': '流地址为空'}

        cmd = [ff, '-hide_banner', '-nostdin', '-loglevel', 'error']
        # 低延迟参数只给直播源：对本地文件反而读不出帧（mp4 的 moov 在末尾，
        # probesize 65536 / analyzeduration 0 会让 ffmpeg 直接放弃）。
        if url.startswith('rtmp'):
            cmd += ['-fflags', 'nobuffer', '-flags', 'low_delay',
                    '-probesize', '65536', '-analyzeduration', '0']
        elif url.startswith('http'):
            cmd += ['-fflags', 'nobuffer', '-flags', 'low_delay',
                    '-probesize', '500000']
        if url.startswith('lavfi:'):
            cmd += ['-re', '-f', 'lavfi', '-i', url[6:]]
        elif url.startswith(('rtmp', 'http')):
            cmd += ['-i', url]
        else:
            # 本地文件按实时读（-re），否则几十秒的片子会在一瞬间全吐出来，
            # 页面上就是一闪而过。回归测试也用这条路。
            cmd += ['-re', '-i', url]
        cmd += [
            '-an', '-sn', '-dn',
            '-vf', f'fps={fps:g}',
            # mjpeg 只吃 full-range，不加这行 ffmpeg 会以 -22 直接退出
            '-pix_fmt', 'yuvj420p',
            '-q:v', str(int(quality)),
            '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1',
        ]
        kwargs = {}
        if sys.platform == 'win32':
            kwargs['creationflags'] = 0x08000000        # CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, **kwargs)
        except OSError as e:
            return {'ok': False, 'error': f'ffmpeg 启动失败：{e}'}
        with self._cond:
            self._proc = proc
            self._src = url
            self._title = title
            self._fps = float(fps)
            self._quality = int(quality)
            self._seq = 0
            self._frame = b''
            self._frame_at = 0.0
            self._size = (0, 0)
            self._frames_total = 0
            self._err = ''
            self._stopping = False
            self._started_at = time.time()
            self._t0 = time.time()
            self._manual = bool(manual)
        self._thread = threading.Thread(target=self._read_loop, args=(proc,),
                                        name='ppt-relay', daemon=True)
        self._thread.start()
        threading.Thread(target=self._read_err, args=(proc,),
                         name='ppt-relay-err', daemon=True).start()
        return {'ok': True, 'reused': False, 'pid': proc.pid, 'src': url}

    def stop(self) -> None:
        with self._cond:
            proc, self._proc = self._proc, None
            self._stopping = True
            self._frame = b''
            self._cond.notify_all()
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except OSError:
                pass

    def _read_err(self, proc: subprocess.Popen) -> None:
        """留住 ffmpeg 的最后一句抱怨，界面才有东西可显示。"""
        err = proc.stderr
        if err is None:
            return
        tail = ''
        while True:
            try:
                chunk = err.readline()
            except (OSError, ValueError):
                break
            if not chunk:
                break
            line = chunk.decode('utf-8', 'replace').strip()
            if line:
                tail = (tail + '\n' + line)[-1200:]
                with self._cond:
                    self._err = tail.strip()
        try:
            err.close()
        except OSError:
            pass

    # ---------------------------------------------------------------- 读帧
    def _read_loop(self, proc: subprocess.Popen) -> None:
        buf = bytearray()
        out = proc.stdout
        if out is None:
            return
        while True:
            with self._cond:
                if self._stopping or self._proc is not proc:
                    break
            try:
                chunk = out.read(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf.extend(chunk)
            # 一帧 = FFD8 ... FFD9，缺口时就留着等下一块
            while True:
                s = buf.find(b'\xff\xd8')
                if s < 0:
                    if len(buf) > 8 * 1024 * 1024:
                        buf.clear()
                    break
                e = buf.find(b'\xff\xd9', s + 2)
                if e < 0:
                    if s:
                        del buf[:s]
                    break
                frame = bytes(buf[s:e + 2])
                del buf[:e + 2]
                with self._cond:
                    self._seq += 1
                    self._frame = frame
                    self._frame_at = time.time()
                    self._frames_total += 1
                    if self._size == (0, 0):
                        w, h = jpeg_size(frame)
                        if w and h:
                            self._size = (w, h)
                    self._cond.notify_all()
        with self._cond:
            if not self._stopping and self._proc is proc:
                rc = proc.poll()
                if rc not in (0, None):
                    self._err = f'ffmpeg 退出（code {rc}），流可能还没开播'
                self._cond.notify_all()

    def wait_frame(self, since: int, timeout: float = 2.0):
        """等一帧比 `since` 新的画面。返回 (seq, jpeg, 产出时刻)；超时 jpeg 为 None。

        产出时刻一起带出去：页面拿它算端到端延迟（帧从 ffmpeg 出来到画在 canvas 上）。
        """
        deadline = time.time() + timeout
        with self._cond:
            while self._seq <= since and not self._stopping:
                left = deadline - time.time()
                if left <= 0:
                    return since, None, 0.0
                if not self.running and self._err:
                    return since, None, 0.0
                self._cond.wait(min(left, 0.5))
            if self._seq > since and self._frame:
                return self._seq, self._frame, self._frame_at
            return since, None, 0.0


RELAY = PptRelay()