"""hfutlive —— 工大直播助手协议库（rtmp_gui.exe 的等价重实现）

纯标准库实现，覆盖原程序 HFUTAutoLogin 的全部能力：
    CAS 登录 / 取 Token / 拉课表 / 取流地址 / 调用播放器

用法：
    from hfutlive import HfutLive
    api = HfutLive("学号", "密码")
    for live in api.list_lives():
        for s in api.streams(live["id"]):
            print(s["name"], s["hls"])
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

CAS_HOST = 'http://cas.hfut.edu.cn'
SERVICE_URL = 'http://172.31.241.28:8080/somooc/api/hgd/sso/login'
LIVE_BASE = 'http://172.31.241.28:8443'
STREAM_BASE = 'http://172.31.241.177:81'

TOKEN_RE = re.compile(r'token[=":\']+([A-Za-z0-9]{32})')

def _pick_config_dir() -> Path:
    """挑一个真的可写的配置目录（用户目录可能被安全软件锁成只读）。"""
    if getattr(sys, 'frozen', False):                  # 打包版：exe 旁边
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent  # .../HFUTone
    here = base
    cands = []
    env = os.environ.get('HFUTLIVE_HOME')
    if env:
        cands.append(Path(env))
    cands += [here / '.config', Path.home() / '.hfutlive',
              Path(os.environ.get('LOCALAPPDATA') or here) / 'HFUTlive']
    for d in cands:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / '.write-test'
            probe.write_text('x', 'utf-8')
            probe.unlink()
            return d
        except OSError:
            continue
    return here / '.config'


CONFIG_DIR = _pick_config_dir()
CACHE_FILE = CONFIG_DIR / 'session.json'
CACHE_TTL = 20 * 60          # 服务端 session 约 30 分钟，留余量

# 默认走 fast（低延迟）；HFUT_PLAY_MODE=original 可回到原版参数。
# 原程序是 --demuxer-lavf-analyzeduration=5.0，光"分析流"就要等最多 5 秒，
# 端到端延迟必然超标；这里压到 0.5s，并把读取缓冲压到 0.5s。
MPV_ARGS_COMMON = (
    '--force-window=yes '
    # 硬解（实测 d3d11va 在这台 RTX 3060 上可用）。
    # 不只是性能问题：这个流用软解 + 短探测会初始化失败、窗口一直黑着，
    # 走硬解路径反而能正常出画面。
    '--hwdec=d3d11va '
    # 不用 desync：实测多实例并播时它会让某一路卡在 Starting playback 不出帧

    '--stream-lavf-o-add=reconnect=1 '
    '--stream-lavf-o-add=timeout=10000000 '
    '--stream-lavf-o-add=rtmp_live=live '
    '--network-timeout=10'
)
# rtmp_live=live：只保留实时数据、丢弃过期 GOP。
# 缺了它 FFmpeg 会把服务器发来的历史数据全缓冲，摄像头流能堆出几十秒延迟。
# network-timeout=10：连不上时快速失败退出，而不是留个黑窗口一直挂着。

# 平衡档（推荐）：分析时长保持原版，只把 mpv 的默认缓冲压小。
# 这样不会因为探测不全而黑屏，延迟的下降全部来自缓冲减少。
# 摄像头是动态高码率流，探测窗口比 PPT 给得大一些，否则参数识别不全
# 实测出来的（三路同时播是常态，以下都按并发场景验证）：
#   0. 探测窗口必须够大：analyzeduration=2.5 / probesize=2MB。
#      给 1.0/1MB 时三路并发会有一路识别不出视频轨 —— 窗口开着但永远黑屏，
#      日志里表现为 Detected file format 之后直接 Starting playback，没有 Video 行。
#   1. analyzeduration 必须 >= 1.0，给 0.5 直接不出画面
#   2. 不能设 --demuxer-readahead-secs=0.3（预读太少，解码器起不来）
#   3. 不能用 --cache=yes --cache-secs=0.5（同上）
# 另外不要用 --profile=low-latency，它内含 stream-buffer-size=4k，同样黑屏。
# 也不用 --profile=low-latency：它内含 stream-buffer-size=4k 等设置，
# 对学校的 FLV 流会把首帧卡死（实测无画面）。这里逐项显式给。
MPV_ARGS_BALANCED_LIVE = (
    '--demuxer-lavf-analyzeduration=2.5 '
    '--demuxer-lavf-probesize=2000000 '
    '--cache-pause=no '
    '--stream-buffer-size=512k '
    '--stream-lavf-o=tcp_nodelay=1'
)
# PPT 是静态低码率流，探测需求比摄像头小得多，单独给一套更紧的窗口。
# 实测（三路并发下）：1.0/1MB → 首帧 1.45s；0.5/500KB → 0.98s；
# 0.4/400KB 就识别不出视频轨了（黑屏）。取 0.5/500KB，留一档余量。
# PPT 是静态低码率流，单独用 1.0s/1MB 探测（三路并发实测：视频轨识别正常、
# 首帧约 1.45s）。再往下压（0.4/400KB）就会识别不出视频轨、窗口一直黑着。
MPV_ARGS_BALANCED_PPT = (
    '--no-audio '
    '--demuxer-lavf-analyzeduration=1.0 '
    '--demuxer-lavf-probesize=1000000 '
    '--cache-pause=no '
    '--stream-buffer-size=512k '
    '--stream-lavf-o=tcp_nodelay=1'
)

# 原版参数（HFUT_PLAY_MODE=original 时使用，跟 rtmp_gui.exe 完全一致）
MPV_ARGS_ORIGINAL_PPT = ('--no-audio --demuxer-lavf-analyzeduration=5.0 '
                         '--demuxer-lavf-probesize=5000000')
MPV_ARGS_ORIGINAL_LIVE = ('--demuxer-lavf-analyzeduration=2.0 '
                          '--demuxer-lavf-probesize=2000000')

# 低延迟档：目标「点开到出画面」2 秒内，端到端约 2–3.5 秒
MPV_ARGS_FAST = (
    '--demuxer-lavf-analyzeduration=1.0 '
    '--demuxer-lavf-probesize=2000000 '
    '--cache=yes --cache-secs=0.5 --demuxer-max-bytes=4MiB '
    '--demuxer-readahead-secs=0.3 '
    '--stream-lavf-o=tcp_nodelay=1'
)

# 极速档：彻底关缓冲，端到端可压到 1.5–2s，代价是抖动时容易卡
MPV_ARGS_ULTRA = (
    '--demuxer-lavf-analyzeduration=0.3 '
    '--demuxer-lavf-probesize=300000 '
    '--cache=no --demuxer-readahead-secs=0 '
    '--stream-lavf-o=tcp_nodelay=1'
)

# 稳定档：网络抖动大、或 VPN 丢包时用，多缓冲 3 秒换流畅
MPV_ARGS_STABLE = (
    '--demuxer-lavf-analyzeduration=1.0 '
    '--demuxer-lavf-probesize=1000000 '
    '--cache=yes --cache-secs=3 --demuxer-max-bytes=8MiB'
)

# 兼容旧引用
MPV_ARGS_PPT = '--no-audio'
MPV_ARGS_LIVE = MPV_ARGS_FAST
MPV_ARGS_FAST_PPT = '--no-audio ' + MPV_ARGS_FAST
MPV_ARGS_STABLE_PPT = '--no-audio ' + MPV_ARGS_STABLE


class HfutLiveError(RuntimeError):
    pass


def check_intranet(timeout: float = 2.0) -> dict:
    """探测校内服务是否可达。

    校园网 WiFi / 已连 VPN 时可达；家庭宽带直连必然超时。
    返回 {'api': bool, 'stream': bool}，供界面给出准确提示。
    """
    import socket as _socket
    out = {}
    for name, host, port in (('api', '172.31.241.28', 8443),
                             ('stream', '172.31.241.177', 81)):
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            out[name] = True
        except OSError:
            out[name] = False
        finally:
            s.close()
    return out


def friendly_error(err: Exception) -> str:
    """把底层异常翻译成用户能照着做的提示。"""
    text = str(err) or err.__class__.__name__
    low = text.lower()
    if ('10060' in low or '10061' in low or isinstance(err, TimeoutError)
            or 'timed out' in low or 'timeout' in low
            or '一段时间后没有正确答复' in text):
        return ('连不上校内服务器（172.31.241.28）。当前多半是校外宽带，'
                '请连接校园网 WiFi，或先启动 EasyConnect VPN 再刷新。')
    if 'refused' in low or 'connecterror' in low or '10054' in low:
        return '校内服务器拒绝连接，可能服务在维护；换个时间再试，或先确认 VPN 已连上。'
    if 'token' in low:
        return '登录成功但没换到会话 Token，通常是校外网络把回跳拦住了；连上 VPN 再试。'
    if '500' in text:
        return '校内接口返回 500，多半是会话过期；点一次刷新会自动重登。'
    if isinstance(err, urllib.error.URLError):
        return f'网络请求失败：{text}。检查是否已连校园网或 VPN。'
    return text


def _opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),          # 忽略系统/环境代理，避免踩坑
        urllib.request.HTTPCookieProcessor(CookieJar()),
    )


def _read(resp, limit=8_000_000) -> bytes:
    data = resp.read(limit)
    if 'gzip' in (resp.headers.get('Content-Encoding') or ''):
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    return data


class HfutLive:
    def __init__(self, username: str = '', password: str = '', *, use_cache: bool = True):
        self.username = username
        self.password = password
        self.token: str | None = None
        self.use_cache = use_cache
        self._opener = _opener()

    # ---------------------------------------------------------------- 登录
    def _load_cache(self) -> str | None:
        if not self.use_cache or not CACHE_FILE.exists():
            return None
        try:
            blob = json.loads(CACHE_FILE.read_text('utf-8'))
        except (OSError, ValueError):
            return None
        if blob.get('username') != self.username:
            return None
        if time.time() - blob.get('ts', 0) > CACHE_TTL:
            return None
        return blob.get('token')

    def _save_cache(self, token: str) -> None:
        if not self.use_cache:
            return
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps({
                'username': self.username, 'token': token, 'ts': time.time(),
            }), 'utf-8')
        except OSError:
            pass

    def login(self, *, force: bool = False) -> str:
        """完整 CAS 流程，返回 32 位 token。"""
        if not force:
            cached = self._load_cache()
            if cached:
                self.token = cached
                return cached

        # 每次登录都换干净的 opener：反复登录后 cookiejar 会攒下多个 JSESSIONID，
        # 叠加到 Cookie 头里会让服务端直接 500。
        self._opener = _opener()
        headers = {'User-Agent': UA, 'Accept': 'application/json, text/plain, */*'}
        cas_url = f'{CAS_HOST}/cas/login?service=' + urllib.parse.quote(SERVICE_URL, safe='')

        html = _read(self._opener.open(
            urllib.request.Request(cas_url, headers=headers), timeout=30)).decode('utf-8', 'replace')

        execution = ''
        for pat in (r'name="execution"\s+value="([^"]*)"',
                    r'value="([^"]*)"\s+name="execution"'):
            m = re.search(pat, html)
            if m:
                execution = m.group(1)
                break

        form = urllib.parse.urlencode({
            'username': self.username,
            'password': self.password,
            'execution': execution,
            '_eventId': 'submit',
            'geolocation': '',
            'codeRandom': '',
        }).encode()

        resp = self._opener.open(urllib.request.Request(cas_url, data=form, headers={
            **headers, 'Content-Type': 'application/x-www-form-urlencoded'}), timeout=30)
        url = resp.headers.get('Location') or resp.geturl()

        token = None
        for _ in range(6):
            if not url:
                break
            r = self._opener.open(urllib.request.Request(url, headers=headers), timeout=30)
            nxt = r.headers.get('Location', '')
            text = _read(r).decode('utf-8', 'replace')
            for cand in (r.geturl(), nxt, text):
                if not cand:
                    continue
                m = TOKEN_RE.search(cand)
                if m:
                    token = m.group(1)
                    break
            if token:
                break
            url = nxt

        if not token:
            raise HfutLiveError('登录失败: 无法获取 Token (请确认校园网环境)')

        # 原程序叫 login_and_jump：拿到 token 后还会走一遍 login-auto 和 /live，
        # 把服务端会话上下文建立起来，否则紧接着的 API 会 500。
        try:
            self._opener.open(urllib.request.Request(
                f'{LIVE_BASE}/login-auto?token={token}', headers=headers), timeout=20)
            self._opener.open(urllib.request.Request(
                f'{LIVE_BASE}/live',
                headers={**headers, 'Token': token,
                         'Cookie': f'JSESSIONID={token}; Token={token}',
                         'Referer': f'{LIVE_BASE}/live'}), timeout=20)
        except Exception:
            pass

        self.token = token
        self._save_cache(token)
        return token

    # ---------------------------------------------------------------- 接口
    def _api(self, path: str, payload: dict | None = None, _retry: bool = True) -> dict:
        if not self.token:
            self.login()
        headers = {
            'User-Agent': UA,
            'Accept': 'application/json, text/plain, */*',
            'Token': self.token,
            'Referer': f'{LIVE_BASE}/live',
            'Cookie': f'JSESSIONID={self.token}; Token={self.token}',
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(f'{LIVE_BASE}{path}', data=data, headers=headers)
        try:
            raw = _read(self._opener.open(req, timeout=30)).decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            # 401/403 是明确的会话失效；500 也常见于旧 token 被服务端丢弃，
            # 两种情况都重登一次再试，避免界面上出现莫名的「加载失败」。
            if e.code in (401, 403, 500) and _retry:
                self.login(force=True)
                return self._api(path, payload, _retry=False)
            raise
        return json.loads(raw)

    def list_lives(self, page_size: int = 200) -> list[dict]:
        data = self._api('/api/c/live/list', {
            'pageNo': 1, 'pageSize': page_size,
            'name': '', 'organizationId': '', 'error': False,
        })
        return (data.get('data') or {}).get('list') or []

    def live_detail(self, live_id: str) -> dict:
        data = self._api('/api/c/live/detail', {'id': live_id})
        return data.get('data') or {}

    def streams(self, live_id: str) -> list[dict]:
        """返回 [{name, stream, rtmp, hls, isInternal}]"""
        detail = self.live_detail(live_id)
        out = []
        for item in detail.get('itemList') or []:
            rtmp = item.get('url') or item.get('httpUrl') or ''
            hls = item.get('playUrl') or ''
            if not hls and rtmp:
                # rtmp://host/live/xxx -> http://host:81/live/xxx.m3u8
                try:
                    p = urllib.parse.urlsplit(rtmp)
                    hls = f'http://{p.hostname}:81{p.path}.m3u8'
                except ValueError:
                    hls = ''
            out.append({
                'name': item.get('name') or '',
                'stream': item.get('stream') or '',
                'rtmp': rtmp,
                'hls': hls,
                'isInternal': item.get('isInternal'),
            })
        return out

    # ---------------------------------------------------------------- 播放
    @staticmethod
    def stream_alive(hls_url: str, timeout: float = 3.0) -> bool | None:
        """判断流是否真在推。True=活着，False=未开播(404)，None=检测不了(网络不可达)。

        避免流没开播时拉起一堆连不上的 mpv 挂在后台。
        """
        if not hls_url or not hls_url.startswith('http'):
            return None
        try:
            req = urllib.request.Request(hls_url, method='HEAD', headers={'User-Agent': UA})
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)
            return True
        except urllib.error.HTTPError as e:
            return e.code != 404          # 404=流不存在；其余错误按活着处理，不误杀
        except Exception:
            return None                   # 超时/不可达：无法判断，由调用方决定

    @staticmethod
    def _kind_of(path: str) -> str:
        name = os.path.basename(path).lower()
        if 'vlc' in name:
            return 'vlc'
        if 'potplayer' in name:
            return 'potplayer'
        if 'ffplay' in name:
            return 'ffplay'
        return 'mpv'

    @staticmethod
    def find_player(force: bool = False) -> tuple[str, str] | None:
        """找可用播放器，返回 (kind, path)。

        查找顺序：用户指定 → 缓存 → PATH → 常见安装/绿色版目录扫描。
        绿色版 mpv 常常躺在 Downloads 里不进 PATH，所以要扫目录。
        """
        # 1) 用户显式指定（环境变量或配置文件）
        explicit = os.environ.get('HFUT_PLAYER') or ''
        if not explicit:
            try:
                explicit = (CONFIG_DIR / 'player.txt').read_text('utf-8').strip()
            except OSError:
                explicit = ''
        if explicit and os.path.exists(explicit):
            return HfutLive._kind_of(explicit), explicit

        # 1.5) 打包内置的 mpv（程序目录下的 mpv/mpv.exe）
        if getattr(sys, 'frozen', False):
            builtin = Path(sys.executable).resolve().parent / 'mpv' / 'mpv.exe'
            if builtin.is_file():
                return 'mpv', str(builtin)

        # 2) 缓存上一次找到的路径
        cache = CONFIG_DIR / 'player.cache'
        if not force:
            try:
                cached = cache.read_text('utf-8').strip()
                if cached and os.path.exists(cached):
                    return HfutLive._kind_of(cached), cached
            except OSError:
                pass

        # 3) PATH
        for name in ('mpv', 'mpv.exe', 'vlc', 'vlc.exe',
                     'PotPlayerMini64', 'PotPlayerMini64.exe', 'ffplay', 'ffplay.exe'):
            found = shutil.which(name)
            if found and not found.lower().endswith(('.bat', '.cmd')):
                return HfutLive._kind_of(found), found

        # 4) 扫常见目录（限深度，避免拖慢启动）
        home = Path.home()
        roots = [
            home / 'Downloads', home / 'Desktop', home / 'Documents',
            home / 'AppData' / 'Local', home / 'AppData' / 'Roaming',
            Path(r'C:\Program Files'), Path(r'C:\Program Files (x86)'),
            Path('D:/'), Path('E:/'),
        ]
        names = {'mpv.exe', 'vlc.exe', 'potplayermini64.exe', 'ffplay.exe'}
        for root in roots:
            if not root.is_dir():
                continue
            base_depth = len(root.parts)
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    if len(Path(dirpath).parts) - base_depth >= 4:
                        dirnames[:] = []
                        continue
                    # 跳过明显无关的大目录
                    dirnames[:] = [d for d in dirnames if d.lower() not in
                                   ('node_modules', '.git', 'windows', 'winsxs', 'temp')]
                    for fn in filenames:
                        if fn.lower() in names:
                            path = os.path.join(dirpath, fn)
                            if os.path.basename(path).lower() in ('mpv.bat', 'mpv.cmd'):
                                continue
                            try:
                                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                                cache.write_text(path, 'utf-8')
                            except OSError:
                                pass
                            return HfutLive._kind_of(path), path
            except OSError:
                continue
        return None

    # 只有老师那一路出声；PPT 无音频，学生流音频解码有持续错误、还会拖慢画面
    AUDIO_CHANNEL = '老师'

    @classmethod
    def wants_audio(cls, channel_name: str) -> bool:
        return (channel_name or '').strip() == cls.AUDIO_CHANNEL

    @staticmethod
    def mpv_args_for(no_audio: bool = False) -> list[str]:
        """构造与 play() 完全一致的 mpv 参数（不含播放器路径、URL 与 --wid）。

        应用内嵌入播放（--wid）复用同一套档位，保证体验与独立窗口一致。
        """
        mode = (os.environ.get('HFUT_PLAY_MODE') or 'balanced').lower()
        if mode == 'ultra':
            base = MPV_ARGS_ULTRA
        elif mode == 'stable':
            base = MPV_ARGS_STABLE
        elif mode == 'fast':
            base = MPV_ARGS_FAST
        elif mode == 'original':
            base = MPV_ARGS_ORIGINAL_PPT if no_audio else MPV_ARGS_ORIGINAL_LIVE
        else:                       # balanced：原版探测 + 小缓冲
            base = MPV_ARGS_BALANCED_PPT if no_audio else MPV_ARGS_BALANCED_LIVE
        args = base.split()
        # balanced / original 的参数串里已经带好 --no-audio 或 profile
        if mode in ('fast', 'ultra', 'stable'):
            args.append('--no-audio' if no_audio else '--video-sync=audio')
        args += MPV_ARGS_COMMON.split()
        return args

    def play(self, url: str, title: str = 'HFUTLive', *, low_latency: bool = True,
             no_audio: bool = False, stable: bool | None = None) -> bool:
        """用本机播放器打开流；没有播放器则交给系统默认程序。

        stable=True 走稳定档（多缓冲 3 秒），默认读环境变量 HFUT_PLAY_MODE=stable。
        """
        player = self.find_player()
        if player:
            kind, path = player
            if kind == 'mpv':
                args = [path, url] + self.mpv_args_for(no_audio)
                args += [f'--title={title}']
            elif kind == 'vlc':
                args = [path, '--network-caching=1000', url]
            elif kind == 'potplayer':
                args = [path, url]
            else:
                args = [path, '-window_title', title, '-low_delay', url]
            # CREATE_NO_WINDOW：外部播放器大多自带窗口，但万一落到控制台
            # 程序（或 .bat 包装）上就会弹一个黑框，而且那个黑框关不掉、
            # 还占着 exe 目录不放（打包时 dist 被锁就是它）。
            kw = {}
            if sys.platform == 'win32':
                kw['creationflags'] = 0x08000000
            return subprocess.Popen(args, close_fds=True, **kw)
        try:
            os.startfile(url)          # HLS 交给系统默认（浏览器/播放器）
            return True
        except OSError:
            return False

    def play_proc(self, url: str, title: str = 'HFUTLive', *, low_latency: bool = True,
                  no_audio: bool = False, stable: bool | None = None):
        """同 play()，但成功时返回 Popen 对象（用于记录 PID 统一关闭）。
        找不到播放器交给系统默认时返回 True，失败返回 False/None。"""
        return self.play(url, title, low_latency=low_latency, no_audio=no_audio, stable=stable)

    @staticmethod
    def find_ffmpeg() -> str | None:
        return shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')

    def record(self, url: str, out_file: str, *, duration: int | None = None) -> subprocess.Popen:
        """用 ffmpeg 录制 / 转存（无 ffmpeg 时抛错）。"""
        ff = self.find_ffmpeg()
        if not ff:
            raise HfutLiveError('系统未安装 ffmpeg，无法录制')
        args = [ff, '-y', '-i', url, '-c', 'copy', '-bsf:a', 'aac_adtstoasc', out_file]
        if duration:
            args = [ff, '-y', '-i', url, '-t', str(duration), '-c', 'copy', out_file]
        # ffmpeg 是控制台程序，不隐藏就弹黑框；它还会锁住 exe 目录，
        # 让打包/删除失败。隐藏窗口 + 独立进程组，父进程退出时能整组清理。
        kw = {}
        if sys.platform == 'win32':
            kw['creationflags'] = 0x08000000
        return subprocess.Popen(args, **kw)


def _fmt(live: dict) -> str:
    return (f'{live.get("startTime", "")[:16]}  {live.get("statusName", ""):<4} '
            f'{live.get("classroomName", ""):<10}  {live.get("name", "")}')


def _ensure_console() -> None:
    """让 Windows 控制台以 UTF-8 输出，避免中文乱码。"""
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def main(argv: list[str]) -> int:
    _ensure_console()
    import argparse
    ap = argparse.ArgumentParser('hfutlive', description='工大直播助手命令行版')
    ap.add_argument('-u', '--user', default=os.environ.get('HFUT_USER', ''))
    ap.add_argument('-p', '--password', default=os.environ.get('HFUT_PASS', ''))
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('list', help='列出直播课')
    p_play = sub.add_parser('play', help='播放第 N 条（从 0 开始）')
    p_play.add_argument('index', type=int)
    p_url = sub.add_parser('url', help='打印第 N 条的流地址')
    p_url.add_argument('index', type=int)
    p_rec = sub.add_parser('record', help='录制第 N 条')
    p_rec.add_argument('index', type=int)
    p_rec.add_argument('out', help='输出文件')
    p_rec.add_argument('--seconds', type=int, default=None)

    args = ap.parse_args(argv)
    if not args.user or not args.password:
        print('需要学号与密码：-u 学号 -p 密码（或环境变量 HFUT_USER/HFUT_PASS）')
        return 2

    api = HfutLive(args.user, args.password)
    lives = api.list_lives()
    print(f'token = {api.token}，共 {len(lives)} 条直播课')

    if args.cmd == 'list':
        for i, live in enumerate(lives):
            print(f'[{i:3d}] {_fmt(live)}')
        return 0

    idx = args.index
    if not 0 <= idx < len(lives):
        print('索引越界')
        return 2
    live = lives[idx]
    print(f'选中：{_fmt(live)}')

    streams = api.streams(live['id'])
    if not streams:
        print('当前教室内无直播信号')
        return 1
    for s in streams:
        print(f'  {s["name"]:<6} {s["stream"]:<3} {s["rtmp"]}')

    if args.cmd == 'url':
        return 0
    if args.cmd == 'play':
        ok = False
        for s in streams:
            ok |= api.play(s['rtmp'], title=f'HFUTLive-{s["name"]}',
                           low_latency=(s['name'] != 'PPT'))
        print('已启动播放器' if ok else '未找到播放器')
        return 0 if ok else 1
    if args.cmd == 'record':
        proc = api.record(streams[0]['rtmp'], args.out, duration=args.seconds)
        print(f'录制中 -> {args.out}（Ctrl+C 停止）')
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
