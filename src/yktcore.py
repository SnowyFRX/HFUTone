"""yktcore.py —— 长江雨课堂协议层。

从 ykt_ui 的 C# 实现（YktClient.cs / YktApi.cs / Net.cs）逐条移植过来，
只保留协议与判定逻辑，不碰界面。合并进 HFUTone 之后，直播课堂和雨课堂
共用同一个本地服务和同一套前端。

两个必须照抄的设计:

1. 传输走 curl 而不是 Python 的 TLS 栈。
   学校网关按 ClientHello 指纹拦请求，.NET / OpenSSL 的指纹一律被打成
   HTML 拦截页（日志里 code 为空那种）。原程序的做法是内嵌一个 LibreSSL
   版 curl，这里把同一份 curl 打包进 vendor/curl/，首次运行时解到 bin 目录。

2. 签到只解决"进班"，考勤真伪看 rollcall。
   POST /api/v3/lesson/checkin 成功只是进了课堂，未必产生考勤记录；
   动态二维码课堂会直接回 50070 拒绝进班，但我的记录可能早就登记了。
   所以判定顺序是: checkin code -> rollcall 里有没有我的记录 -> 才判失败。

离线回放: 设环境变量 YKT_REPLAY=<夹具目录> 之后所有请求改读夹具文件，
可以完全不联网跑一遍解析和判定（HFUTone/fixtures 里有样本）。
"""
from __future__ import annotations

import ctypes
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------- 常量

SERVER = 'https://changjiang.yuketang.cn'
CDN_BASE = 'https://qn-scd1.yuketang.cn/'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# ---------------------------------------------------------------- App 身份
# 雨课堂动态二维码考勤（code 50070）是按「客户端类型」判的：浏览器身份一律被拒、
# 要求你扫码；官方 App 的扫码只是 UI 行为，API 层不强制。所以理论上把 UA 和
# x-client/xtua 换成 App 的就能免扫。
#
# 但实测长江雨课堂这条路走不通，见 APP_IDENTITY 常量上的记录：同一个 web cookie
# 用 App 头去请求会直接 UNAUTHENTICATED。这里的常量留着，是为了将来真做了
# App 登录（拿 App 体系的凭据）之后能直接启用。
APP_UA = 'Android'

APP_HEADERS = {
    'user-agent': APP_UA,
    'accept': 'application/json',
    'brand': 'google Pixel 9 Pro',
    'uuid': '',
    'buildnumber': '1610',
    'xtua': 'client=app&tag=1.3.3&platform=Android',
    'systemversion': '16',
    'incremental': '14624737',
    'isphysicaldevice': 'true',
    'xtbz': 'ykt',
    'x-client': 'app',
}

# 当前登录用户的 uid，登录后由 yktapi 灌进来，随每个雨课堂请求带 x-uid。
# 空着也能发（少个头），但带上更接近真 App。
_app_uid = ''

# 是否把雨课堂请求整体伪装成 App 客户端。
#
# 实测结论（2026-11-23，长江雨课堂）：**默认必须关**。
# 同一个 sessionid cookie，三种身份去拉 /api/v3/classroom/on-lesson：
#   App 头      -> {"code":50000,"msg":"UNAUTHENTICATED"}
#   浏览器头     -> {"code":0,"msg":"OK",...}          <- 只有这个能用
#   裸 curl     -> {"code":50000,"msg":"UNAUTHENTICATED"}
#
# 差分测试进一步定位到：**只有 x-client: app 这一个头会触发 App 鉴权分支**，
# UA / xtbz / xtua / brand / x-uid 单独加都不影响（全部 200）。触发之后，
# web 体系的凭据一律 50000——所以凭据必须和身份配套。
#
# 想要 App 身份只有一个来源：yktapplogin 走 /api/v3/user/login/app 登录，
# 拿到 App 体系的 set-cookie（存 app_cookie），再通过 set_identity(True) 开启。
# 开关会落盘（app_identity 字段），重启由 restore_identity() 恢复。
APP_IDENTITY = False


def set_app_uid(uid) -> None:
    """登录成功后调一次，之后所有雨课堂请求都会带 x-uid。"""
    global _app_uid
    _app_uid = str(uid or '').strip()


def set_identity(app: bool, store: Store | None = None) -> bool:
    """切换当前身份并落盘。返回切换后状态。

    App 身份只有在 App 凭据存在时才有意义——没有凭据就切过去，等于把整条
    链路换成一套空 cookie，所有接口都会 50000。所以这里做硬校验。
    """
    global APP_IDENTITY
    store = store or Store()
    if app and not store.get_app_cookie():
        log('[身份] 没有 App 凭据，拒绝切到 App 身份')
        APP_IDENTITY = False
        store.save_app_identity(False)
        return False
    APP_IDENTITY = bool(app)
    store.save_app_identity(APP_IDENTITY)
    log(f'[身份] 当前身份 = {"App" if APP_IDENTITY else "web"}')
    return APP_IDENTITY


def restore_identity(store: Store | None = None) -> bool:
    """启动时恢复上次的身份。没有 App 凭据就自动退回 web。"""
    global APP_IDENTITY
    store = store or Store()
    want = store.get_app_identity()
    have = bool(store.get_app_cookie())
    APP_IDENTITY = bool(want and have)
    if want and not have:
        store.save_app_identity(False)
        log('[身份] 上次是 App 身份，但 App 凭据已丢失，回退到 web')
    return APP_IDENTITY


def is_ykt_url(url: str) -> bool:
    """雨课堂的域名（含 CDN）。学习通走另一套头，别混。"""
    return 'yuketang.cn' in (url or '')
CODE_MARK = '\n---YKTSTATUS---'
WS_URL = 'wss://changjiang.yuketang.cn/wsapp/'

ROOT = Path(__file__).resolve().parent.parent
# 打包后 ROOT 是 exe 所在目录（放 .config），而 datas 打进了 _internal，
# 也就是 sys._MEIPASS。资源（vendor/curl）必须从这里找，否则解压永远跳过，
# 静默退到系统 curl（Schannel 指纹），网关就会把接口打成 HTML。
RES = Path(getattr(sys, '_MEIPASS', ROOT))


# --------------------------------------------------------------------- 目录

def _pick_config_dir() -> Path:
    """优先项目内 .config；写不进去（只读安装位置 / 安全软件）就退到用户目录。"""
    env = os.environ.get('YKT_CONFIG_DIR')
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(ROOT / '.config')
    local = os.environ.get('LOCALAPPDATA')
    if local:
        candidates.append(Path(local) / 'HFUTone')
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / '.write-probe'
            probe.write_text('x', 'utf-8')
            probe.unlink()
            return p
        except OSError:
            continue
    return Path(os.environ.get('TEMP') or '.') / 'HFUTone'


CONFIG_DIR = _pick_config_dir()
CONFIG_FILE = CONFIG_DIR / 'ykt.json'
LOG_DIR = CONFIG_DIR / 'logs'
COOKIE_FILE = CONFIG_DIR / 'ykt-cookie.txt'


def log(message: str) -> None:
    """写一行日志：每天一个文件，落不下去就静默丢弃（不影响主流程）。"""
    line = f'{datetime.now():%H:%M:%S}  {message}'
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / f'ykt-{datetime.now():%Y%m%d}.log', 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def head(text: str, n: int = 160) -> str:
    if not text:
        return ''
    flat = text.replace('\r', ' ').replace('\n', ' ')
    return flat[:n]


# ------------------------------------------------------------------- curl 资源

BIN_DIR = Path(os.environ.get('YKT_RESDIR') or (CONFIG_DIR / 'bin'))


def _unpack_curl() -> None:
    """把 vendor/curl/*.gz 解到 bin 目录。已经解过且大小对得上就跳过。"""
    src = RES / 'vendor' / 'curl'
    if not src.is_dir():
        return
    manifest = {}
    try:
        for line in (src / 'manifest.txt').read_text('utf-8').splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                manifest[k.strip()] = int(v.strip())
    except (OSError, ValueError):
        manifest = {}
    try:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for gz in src.glob('*.gz'):
        name = gz.name[:-3]
        out = BIN_DIR / name
        want = manifest.get(name)
        try:
            if out.exists() and (want is None or out.stat().st_size == want):
                continue
            data = gzip.decompress(gz.read_bytes())
            tmp = out.with_suffix(out.suffix + '.tmp')
            tmp.write_bytes(data)
            tmp.replace(out)
            log(f'[资源] 解压 {name} -> {out} ({len(data)}B)')
        except OSError as exc:
            log(f'[资源] 解压 {name} 失败 {exc}')


_CURL_LOCK = threading.Lock()
_CURL_PATH: str | None = None


def curl_path() -> str:
    """内置 LibreSSL 版 curl 优先；退到 PATH 里的 curl。"""
    global _CURL_PATH
    with _CURL_LOCK:
        if _CURL_PATH:
            return _CURL_PATH
        _unpack_curl()
        for cand in (BIN_DIR / 'curl.exe', BIN_DIR / 'curl'):
            if cand.exists():
                _CURL_PATH = str(cand)
                break
        if not _CURL_PATH:
            _CURL_PATH = shutil.which('curl') or 'curl'
            log(f'[资源] 警告：没找到内置 curl，退到 {_CURL_PATH}'
                f'（Schannel 指纹可能被网关拦成 HTML）')
        else:
            log(f'[资源] curl = {_CURL_PATH}')
        return _CURL_PATH


def ca_cert_arg() -> list[str]:
    for cand in (BIN_DIR / 'cacert.pem',):
        if cand.exists():
            return ['--cacert', str(cand)]
    return []


# --------------------------------------------------------------------- 响应体

class Resp:
    """一次 HTTP 往返。headers 是原始响应头文本，body 已经剥掉头。"""

    __slots__ = ('status', 'body', 'headers', 'exit_code', 'ms')

    def __init__(self, status: int = 0, body: str = '', headers: str = '',
                 exit_code: int = 0, ms: int = 0):
        self.status = status
        self.body = body
        self.headers = headers
        self.exit_code = exit_code
        self.ms = ms

    def header(self, name: str) -> str:
        low = name.lower() + ':'
        for line in self.headers.replace('\r\n', '\n').split('\n'):
            if line.lower().startswith(low):
                return line[len(low):].strip()
        return ''

    @property
    def looks_like_html(self) -> bool:
        s = (self.body or '').lstrip()[:200].lower()
        return s.startswith('<!doctype') or s.startswith('<html')


# ------------------------------------------------------------------ 回放夹具

def replay_dir() -> str | None:
    v = os.environ.get('YKT_REPLAY')
    return v if v else None


_REPLAY_MAP = {
    'pre-info': 'pre-info.json',
    'on-lesson': 'on-lesson.json',
    'checkin': 'checkin.json',
    'lesson_info_v2': 'lesson-info.json',
    'fetch': 'presentation.json',
    'retry': 'answer-retry.json',
    'answer': 'answer.json',
    'login': 'login.json',
}


def _replay(url: str, with_headers: bool) -> Resp:
    d = replay_dir()
    assert d is not None
    path = url.split('?', 1)[0]
    tail = path.rsplit('/', 1)[-1].strip().lower()
    name = _REPLAY_MAP.get(tail, tail + '.json')
    f = Path(d) / name
    if not f.exists():
        return Resp(404, f'{{"code":-404,"msg":"夹具缺失: {name}"}}', '', 0, 0)
    headers = ''
    if with_headers:
        headers = ('HTTP/1.1 200 OK\r\n'
                   'set-auth: replay-bearer-0001\r\n'
                   'Set-Cookie: sessionid=replay0000000000000000000000000000; Path=/\r\n')
    return Resp(200, f.read_text('utf-8'), headers, 0, 0)


# ---------------------------------------------------------------------- curl

def request(method: str, url: str, *, cookie: str = '', referer: str = '',
            data: object | None = None, with_headers: bool = False,
            timeout: int = 20, extra_headers: dict | None = None,
            tag: str = '', form: dict | None = None,
            files: dict | None = None, follow: bool = False) -> Resp:
    """发一次请求。extra_headers 覆盖自定义头。

    data  按 JSON 序列化（雨课堂那一套接口都用 JSON）
    form  application/x-www-form-urlencoded（学习通全套接口都用这个）
    files multipart/form-data，{字段名: 本地文件路径}，和 form 可同时给
    follow 跟随 3xx 跳转，登录链路要它

    三种 body 互斥，按 data -> files -> form 的优先级取第一个非空的。
    """
    if replay_dir():
        r = _replay(url, with_headers)
        log(f'[回放] {method} {url}  status={r.status}  len={len(r.body)}')
        return r

    args = [curl_path(), '-s', '--max-time', str(int(timeout))]
    if follow:
        args.append('-L')
    if with_headers:
        args.append('-i')
    args += ['-X', method, url]
    args += ca_cert_arg()
    if cookie:
        args += ['-H', f'Cookie: {cookie}']
    if referer:
        args += ['-H', f'Referer: {referer}']
    # curl 会把同名的 -H 都发出去（两个 User-Agent 会让服务端取到浏览器那个），
    # 所以调用方自己指定 UA 时就不再追加默认的。
    extra = {k: v for k, v in (extra_headers or {}).items() if k}

    # 雨课堂一律按 App 身份发：动态码考勤只认客户端类型，浏览器身份必被 50070 拒。
    # 合并顺序是「默认 App 头 < 调用方显式指定」，这样个别接口仍能覆盖。
    # 学习通域名（chaoxing）不走这条，它有自己的 UA 和 schild。
    #
    # 注意 APP_IDENTITY 默认是 False：长江雨课堂的 web cookie 在 App 身份下
    # 会被判 UNAUTHENTICATED（见上面常量的实测记录），硬开会把整个会话弄废。
    if APP_IDENTITY and is_ykt_url(url):
        merged = dict(APP_HEADERS)
        merged.update(extra)
        if _app_uid and not any(k.lower() == 'x-uid' for k in merged):
            merged['x-uid'] = _app_uid
        extra = merged

    if not any(k.lower() == 'user-agent' for k in extra):
        args += ['-H', f'User-Agent: {UA}']
    for k, v in extra.items():
        args += ['-H', f'{k}: {(v or "")}'.replace('"', "'")]
    if data is not None:
        args += ['-H', 'Content-Type: application/json',
                 '-d', json.dumps(data, ensure_ascii=False, separators=(',', ':'))]
    elif files:
        # multipart：curl 的 -F 自己算 boundary 和 Content-Type，别手动加头
        for k, v in (form or {}).items():
            args += ['-F', f'{k}={v}']
        for field, path in files.items():
            args += ['-F', f'{field}=@{path}']
    elif form is not None:
        body = urllib.parse.urlencode(
            {k: ('' if v is None else v) for k, v in form.items()})
        args += ['-H', 'Content-Type: application/x-www-form-urlencoded',
                 '--data-raw', body]
    args += ['-w', CODE_MARK + '%{http_code}']

    started = time.perf_counter()
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding='utf-8', errors='replace',
                              timeout=timeout + 15, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        log(f'[HTTP] {method} {url}  进程超时（{timeout + 15}s）')
        return Resp(28, '', '', 28, int((time.perf_counter() - started) * 1000))
    except OSError as exc:
        log(f'[HTTP] {method} {url}  启动失败 {exc}')
        return Resp(0, '', '', 127, 0)

    out = proc.stdout or ''
    err = proc.stderr or ''
    ms = int((time.perf_counter() - started) * 1000)

    status = 0
    idx = out.rfind(CODE_MARK)
    if idx >= 0:
        tail = out[idx + len(CODE_MARK):].strip()
        out = out[:idx]
        try:
            status = int(tail.split()[0])
        except (ValueError, IndexError):
            status = 0
    if status == 0:
        status = 200 if proc.returncode == 0 else abs(proc.returncode)

    headers, body = split_headers(out)
    log(f'[HTTP] {method} {url}  {ms}ms  exit={proc.returncode}  status={status}'
        f'  len={len(body)}  tag={tag}  resp={head(body, 120)}'
        + (f'  stderr={head(err, 100)}' if err.strip() else ''))
    return Resp(status, body, headers if with_headers else headers, proc.returncode, ms)


def split_headers(raw: str) -> tuple[str, str]:
    """curl -i 把响应头拼在正文前面；100-continue 之类的中间响应会有多段。

    注意：subprocess 用 text=True 时 universal newlines 会把 \\r\\n 归一成 \\n，
    所以两种分隔符都要认，否则头永远剥不掉（body 里混进 "HTTP/1.1 200 OK"，
    上层 jcode 拿到 None，表现就是"登录失败 code=None"）。
    """
    if not raw:
        return '', ''
    if not raw.lstrip()[:5].upper().startswith('HTTP/'):
        return '', raw
    heads: list[str] = []
    body = raw
    for _ in range(4):
        if not body.lstrip()[:5].upper().startswith('HTTP/'):
            break
        cut, width = -1, 0
        for sep in ('\r\n\r\n', '\n\n'):
            i = body.find(sep)
            if i >= 0 and (cut < 0 or i < cut):
                cut, width = i, len(sep)
        if cut < 0:
            heads.append(body)
            body = ''
            break
        heads.append(body[:cut])
        body = body[cut + width:]
    return '\n'.join(h.replace('\r\n', '\n') for h in heads), body


_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0


def parse_set_cookies(raw_headers: str, full: bool = False) -> str:
    """默认只留 sessionid / csrftoken；full=True 时保留响应里的全部 cookie。

    为什么要 full：App 登录下发的凭据不一定叫 sessionid。实测 App 登录
    (POST /api/v3/user/login/app) 回的是 sessionid，但 App 体系以后可能加别的
    cookie，只挑白名单会把凭据存丢，看起来像「登录成功但 cookie 为空」。
    """
    vals: dict[str, str] = {}
    set_auth = ''
    for line in (raw_headers or '').replace('\r\n', '\n').split('\n'):
        low = line.lower()
        if low.startswith('set-auth:'):
            set_auth = line[len('set-auth:'):].strip()
            continue
        if not low.startswith('set-cookie:'):
            continue
        v = line[len('set-cookie:'):].strip()
        semi = v.find(';')
        if semi > 0:
            v = v[:semi]
        eq = v.find('=')
        if eq <= 0:
            continue
        vals[v[:eq].strip()] = v
    if full:
        keep = list(vals.values())
    else:
        keep = [v for k, v in vals.items() if k.lower() in ('sessionid', 'csrftoken')]
    cookie = '; '.join(keep)
    if not cookie and set_auth:
        cookie = 'Bearer ' + set_auth
    return cookie


# ------------------------------------------------------------------ JSON 取值

def js(root, *names, default=''):
    """按候选键名取字符串（雨课堂不同接口大小写不统一）。"""
    if not isinstance(root, dict):
        return default
    for n in names:
        if n in root and root[n] is not None:
            v = root[n]
            if isinstance(v, bool):
                return 'true' if v else 'false'
            if isinstance(v, (int, float, str)):
                return str(v)
            return v
    return default


def jcode(body: str) -> int | None:
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    for k in ('code', 'errcode', 'errCode'):
        if k in d:
            try:
                return int(d[k])
            except (TypeError, ValueError):
                return None
    return None


def jdata(body: str):
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    return d.get('data')


def jstr(body: str, *keys) -> str:
    """取一段文案/凭据。

    先找顶层再找 data——雨课堂的 msg/errmsg 都在顶层，lessonToken 才在 data 里。
    以前只翻 data，结果 signin_full 里 `签到成功`、`ALREADY`、`DYNAMIC_QR`、
    `LESSON_END` 这些文案判断全部形同虚设：msg 永远是空串。
    """
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return ''
    if not isinstance(d, dict):
        return ''
    v = js(d, *keys)
    if v:
        return str(v)
    inner = d.get('data')
    return str(js(inner, *keys)) if isinstance(inner, dict) and js(inner, *keys) else ''


# --------------------------------------------------------------------- 凭据存储

class _Blob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.c_void_p)]


def _dpapi(data: bytes, unprotect: bool) -> bytes | None:
    """Windows DPAPI：只有当前用户能解开，和 C# 版一致。失败返回 None。

    必须显式声明 argtypes/restype：Win64 上默认按 int 传指针，LocalFree 会
    报 "int too long to convert"（64 位地址塞不进 32 位 int）。
    """
    if sys.platform != 'win32':
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        fn = crypt32.CryptUnprotectData if unprotect else crypt32.CryptProtectData
        fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                       ctypes.POINTER(_Blob)]
        fn.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        buf = ctypes.create_string_buffer(data, len(data))
        src = _Blob(len(data), ctypes.cast(buf, ctypes.c_void_p))
        dst = _Blob()
        if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(dst)):
            return None
        try:
            out = ctypes.string_at(dst.pbData, dst.cbData) if dst.pbData else b''
        finally:
            if dst.pbData:
                kernel32.LocalFree(ctypes.c_void_p(dst.pbData))
        return out
    except (OSError, AttributeError):
        return None


class Store:
    """config.json 的读写：cookie 加密，签到标记明文，写盘用临时文件 + 原子替换。"""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self._lock = threading.RLock()

    def read_raw(self) -> dict:
        with self._lock:
            try:
                return json.loads(self.path.read_text('utf-8'))
            except (OSError, ValueError):
                return {}

    def write_raw(self, obj: dict) -> bool:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix('.json.tmp')
                tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), 'utf-8')
                tmp.replace(self.path)
                return True
            except OSError as exc:
                log(f'[配置] 写入失败 {exc}')
                return False

    # -- cookie -----------------------------------------------------------
    # 两套凭据分开存，这是硬规则：
    #   cookie      web 身份（浏览器扫码 / 粘贴 cookie 得到）
    #   app_cookie  App 身份（/api/v3/user/login/app 得到）
    # 早先 App 登录直接覆盖 cookie，结果一套 App 体系的 sessionid 把 web 身份
    # 顶掉，on-lesson 连浏览器头都回 50000，签到整条链路被弄废。分开存之后，
    # 哪套不可用只影响它自己。
    def _get_enc(self, key: str) -> str:
        raw = self.read_raw().get(key) or ''
        if not raw:
            return ''
        if isinstance(raw, str) and raw.startswith('enc:'):
            import base64
            try:
                plain = _dpapi(base64.b64decode(raw[4:]), True)
            except (ValueError, OSError):
                return ''
            # DPAPI 能解开但明文是空：说明当初写进去的就是空串（老版本的空写
            # bug 留下的残骸）。当成"没有"处理，别让它冒充有效凭据。
            if not plain:
                log(f'[配置] {key} 是空凭据（加密时就是空的），已忽略')
                return ''
            return plain.decode('utf-8', 'replace')
        return str(raw)

    def _save_enc(self, key: str, value: str) -> bool:
        obj = self.read_raw()
        import base64
        enc = _dpapi(value.encode('utf-8'), False)
        if enc:
            obj[key] = 'enc:' + base64.b64encode(enc).decode('ascii')
        else:
            obj[key] = value
        return self.write_raw(obj)

    def get_cookie(self) -> str:
        raw = self._get_enc('cookie')
        if raw:
            return raw
        try:
            return COOKIE_FILE.read_text('utf-8').strip()
        except OSError:
            return ''

    def save_cookie(self, cookie: str) -> bool:
        cookie = trim_cookie(cookie)
        if not cookie:
            # 空串绝不落盘：它会把一份好凭据抹掉，而且看起来"登录成功了"。
            log('[配置] 拒绝保存空 cookie')
            return False
        ok = self._save_enc('cookie', cookie)
        log(f'[配置] web cookie 已保存 {len(cookie)} 字符 {cookie[:14]}…')
        try:
            COOKIE_FILE.write_text(cookie, 'utf-8')
        except OSError:
            pass
        return ok

    def get_app_cookie(self) -> str:
        return self._get_enc('app_cookie')

    def save_app_cookie(self, cookie: str) -> bool:
        cookie = trim_cookie(cookie)
        if not cookie:
            log('[配置] 拒绝保存空 App cookie')
            return False
        ok = self._save_enc('app_cookie', cookie)
        log(f'[配置] App cookie 已保存 {len(cookie)} 字符 {cookie[:14]}…')
        return ok

    def clear_app_cookie(self) -> bool:
        obj = self.read_raw()
        obj.pop('app_cookie', None)
        return self.write_raw(obj)

    def clear_cookie(self) -> bool:
        obj = self.read_raw()
        obj.pop('cookie', None)
        try:
            COOKIE_FILE.unlink()
        except OSError:
            pass
        return self.write_raw(obj)

    # -- 当前身份（App / web），必须落盘，否则重启后 App 登录白做
    def get_app_identity(self) -> bool:
        return bool(self.read_raw().get('app_identity'))

    def save_app_identity(self, on: bool) -> bool:
        obj = self.read_raw()
        obj['app_identity'] = bool(on)
        return self.write_raw(obj)

    # -- 登录用户（姓名/学校/头像地址），明文存，不敏感，用来在界面里显示"是谁"
    def get_user(self) -> dict:
        u = self.read_raw().get('user')
        return u if isinstance(u, dict) else {}

    def save_user(self, user: dict) -> bool:
        obj = self.read_raw()
        if user:
            obj['user'] = {k: user.get(k, '') for k in ('userId', 'name', 'school', 'avatar')}
        else:
            obj.pop('user', None)
        return self.write_raw(obj)

    # -- 手动标记的"已签到"
    def load_checked(self) -> dict[str, str]:
        d = self.read_raw().get('checked')
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}

    def mark_checked(self, lesson_id: str, when: str | None = None) -> bool:
        obj = self.read_raw()
        checked = obj.get('checked')
        if not isinstance(checked, dict):
            checked = {}
        checked[lesson_id] = when or f'{datetime.now():%m-%d %H:%M}'
        obj['checked'] = checked
        return self.write_raw(obj)

    def unmark_checked(self, lesson_id: str) -> bool:
        obj = self.read_raw()
        checked = obj.get('checked')
        if isinstance(checked, dict):
            checked.pop(lesson_id, None)
            obj['checked'] = checked
        return self.write_raw(obj)


def trim_cookie(raw: str) -> str:
    """用户从浏览器整段粘过来时，去掉 "Cookie:" 前缀和多余空白。"""
    s = (raw or '').strip()
    if s.lower().startswith('cookie:'):
        s = s[7:].strip()
    return re.sub(r'\s+', ' ', s).strip()