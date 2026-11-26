"""HFUTone 本地服务 —— 给网页 UI 提供数据与动作。

纯标准库：http.server + 已逆向复刻的 hfutlive.HfutLive。
凭据沿用原程序的注册表位置 HKCU\\Software\\HFUTLiveTool（明文，与原版一致）。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import sys
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 桌面版：把同级 src/ 加进导入路径。Android(Chaquopy) 里模块已由 apk 自带，
# 而且这里的 __file__ 是虚拟 assets 路径 —— resolve() 在那种路径上会抛
# FileNotFoundError，所以既不 resolve，也先确认目录真实存在。
_src_dir = Path(__file__).parent.parent / 'src'
try:
    if _src_dir.is_dir():
        sys.path.insert(0, str(_src_dir))
except OSError:
    pass
from hfutlive import HfutLive, HfutLiveError, check_intranet, friendly_error  # noqa: E402
import hfutplay                                                     # noqa: E402
import wsserve                                                      # noqa: E402
import yktapi                                                       # noqa: E402
import yktcore                                                      # noqa: E402
import cxapi                                                        # noqa: E402
import cxsign                                                       # noqa: E402
import cxchat                                                       # noqa: E402
import cxim                                                         # noqa: E402
import yktapplogin                                                  # noqa: E402
import yktqr                                                        # noqa: E402
import autosign                                                     # noqa: E402

PPT_RELAY = hfutplay.RELAY                       # PPT 抽帧转发（一路）


def qr_from_relay(tries: int = 4, gap: float = 0.6,
                  hosts=None, label: str = '雨课堂') -> dict:
    """从正在播的课件画面里读二维码。

    动态码签到（50070）要的是"把码交给服务器"，不是"伪装成 App 客户端"。
    手机扫码 = 相机读屏幕；我们本来就在拉老师的画面，截帧解码等价，还不用掏手机。

    学习通的签到码同样长在老师屏幕/课件上，机制一模一样，所以 hosts 可切到
    yktqr.CX_HOSTS —— 解码器不区分平台，之前只是被雨课堂白名单挡在外面。

    返回 {ok, urls, note}。PPT_RELAY 没在跑就直接说清楚，别让用户以为码不存在。
    """
    if not PPT_RELAY.running:
        return {'ok': False, 'urls': [],
                'note': '课件画面没在播，先打开课件再看二维码'}
    seq = int(PPT_RELAY.snapshot().get('seq') or 0)
    last = ''
    for _ in range(max(1, tries)):
        seq, frame, _at = PPT_RELAY.wait_frame(seq, timeout=max(0.2, gap))
        if not frame:
            continue
        got = yktqr.decode_frame(frame, hosts=hosts or yktqr.YKT_HOSTS, label=label)
        if got.get('ok'):
            return got
        last = got.get('note') or last
    return {'ok': False, 'urls': [], 'note': last or '画面里没找到二维码'}

if getattr(sys, 'frozen', False):                # PyInstaller 打包后
    HERE = Path(sys._MEIPASS) / 'web'
else:
    HERE = Path(__file__).resolve().parent
# Android 上网页资源打进了 apk 的 assets，启动时解到 filesDir/web，
# 这个变量把服务指向那份解出来的目录（assets 里的文件不能用 open() 直接读）。
_web_override = os.environ.get('HFUT_WEB_DIR')
if _web_override:
    HERE = Path(_web_override)
REG_PATH = r'Software\HFUTLiveTool'
try:
    from hfutlive import CONFIG_DIR as _CFG
except Exception:                                     # noqa: BLE001
    _CFG = Path(__file__).resolve().parent.parent / '.config'
CRED_DIR = _CFG
CRED_FILE = CRED_DIR / 'creds.json'
COURSES_CACHE = CRED_DIR / 'courses.json'
CLIENT_LOG = CRED_DIR / 'client-errors.log'
CLIENT_LOG_LOCK = threading.Lock()
DEFAULT_PORT = 8765
SETTINGS_FILE = CRED_DIR / 'settings.json'

# PPT 默认只嵌进侧栏、不开窗口；勾上 streams.ppt 就退回原始行为，三路各开一个
# 播放器窗口。老师流默认开（要听声音），学生流默认关——学生流没有音频，实测还会
# 持续报错拖慢其他两路。
#
# ykt.signMode 决定雨课堂签到走哪条路：
#   app  只走伪装 Android App 头那条（能过动态码 50070，但没在真课上验证过）
#   scan 只走 /api/v3/app/scan，把二维码 URL 交给服务器解析
#   both 先 scan，不通再回退 app（默认，两条腿走路最稳）
#
# cx.location 是学习通位置签到的默认坐标。签到那一栏没有让人当场输坐标的地方，
# 因为 5 秒窗口里根本来不及打字——坐标得提前配好。
DEFAULT_SETTINGS = {
    'autoPlay': True,
    'streams': {'teacher': True, 'student': False, 'ppt': False},
    'ppt': {'fps': 20, 'quality': 4},
    # signMode 默认 app：实测（2026-11-23）App 登录拿到的凭据配 App 头，
    # on-lesson 直接 200，动态码考勤不再要求交码；而且同一套凭据连 web 头也能通。
    # 所以 App 身份就是主形态，"先扫码再 App 头对照"除了多一次失败请求没意义。
    'ykt': {'signMode': 'app', 'autoSign': False},
    'cx': {'location': {'address': '', 'lat': 0.0, 'lng': 0.0},
            'extraCourses': [],
            # 自动刷新签到信息：程序开着时定时扫一遍（两端都支持）
            'autoScan': True,
            # 自动提交签到：由后端守护线程执行（桌面 + 安卓两端均支持，默认关）
            'autoSign': False},
    'autoSign': {
        'pollIntervalSec': autosign.DEFAULT_POLL_INTERVAL,
        'webhookUrl': '',
    },
}

YKT_SIGN_MODES = ('app', 'scan', 'both')


def _clamp(v, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:                                    # NaN
        return 0.0
    return max(lo, min(hi, f))


def _clean_extra_courses(items) -> list:
    """手动加课：每条形如 {'courseId','classId','cpi','name'}。

    学习通的课程中心（backclazzdata）不收录"老师临时建、未开课"的课
    （如商益学），但它的签到活动照常在。这类课只能手动把三个 ID
    填进设置，扫描时合并进去。cpi 可留空——空的话扫描时现查
    clazzperson 补上（一次查询，结果缓存在 signer）。
    只留字段非空的合法条目，去重（按 courseId+classId）。
    """
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cid = str(it.get('courseId') or '').strip()
        lid = str(it.get('classId') or '').strip()
        if not cid or not lid:
            continue
        key = (cid, lid)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            'courseId': cid,
            'classId': lid,
            'cpi': str(it.get('cpi') or '').strip(),
            'name': str(it.get('name') or '').strip(),
        })
    return out


def read_settings() -> dict:
    blob = json.loads(json.dumps(DEFAULT_SETTINGS))        # 深拷贝
    try:
        raw = json.loads(SETTINGS_FILE.read_text('utf-8'))
    except (OSError, ValueError):
        return blob
    if isinstance(raw, dict):
        if isinstance(raw.get('autoPlay'), bool):
            blob['autoPlay'] = raw['autoPlay']
        streams = raw.get('streams')
        if isinstance(streams, dict):
            for k in ('teacher', 'student', 'ppt'):
                if isinstance(streams.get(k), bool):
                    blob['streams'][k] = streams[k]
        ppt = raw.get('ppt')
        if isinstance(ppt, dict):
            try:
                blob['ppt']['fps'] = max(5, min(30, int(ppt.get('fps') or 20)))
                blob['ppt']['quality'] = max(1, min(10, int(ppt.get('quality') or 4)))
            except (TypeError, ValueError):
                pass
        ykt = raw.get('ykt')
        if isinstance(ykt, dict):
            mode = str(ykt.get('signMode') or '').strip().lower()
            if mode in YKT_SIGN_MODES:
                blob['ykt']['signMode'] = mode
            if isinstance(ykt.get('autoSign'), bool):
                blob['ykt']['autoSign'] = ykt['autoSign']
        auto_cfg = raw.get('autoSign')
        if isinstance(auto_cfg, dict):
            if auto_cfg.get('pollIntervalSec') is not None:
                try:
                    sec = int(auto_cfg['pollIntervalSec'])
                    blob['autoSign']['pollIntervalSec'] = max(
                        autosign.MIN_POLL_INTERVAL,
                        min(autosign.MAX_POLL_INTERVAL, sec),
                    )
                except (TypeError, ValueError):
                    pass
            if isinstance(auto_cfg.get('webhookUrl'), str):
                wh = auto_cfg['webhookUrl'].strip()
                if not wh or wh.startswith(('http://', 'https://')):
                    blob['autoSign']['webhookUrl'] = wh
        cx = raw.get('cx')
        if isinstance(cx, dict):
            loc = cx.get('location')
            if isinstance(loc, dict):
                blob['cx']['location'] = {
                    'address': str(loc.get('address') or '').strip(),
                    'lat': _clamp(loc.get('lat'), -90, 90),
                    'lng': _clamp(loc.get('lng'), -180, 180),
                }
            ec = cx.get('extraCourses')
            if isinstance(ec, list):
                blob['cx']['extraCourses'] = _clean_extra_courses(ec)
            # 这两个开关必须也读回来，否则保存进去的 false 在下次读取时
            # 被忽略、退回默认的 true —— 表现就是「取消勾选后又被自动勾上」。
            if isinstance(cx.get('autoScan'), bool):
                blob['cx']['autoScan'] = cx['autoScan']
            if isinstance(cx.get('autoSign'), bool):
                blob['cx']['autoSign'] = cx['autoSign']
    return blob


def im_session() -> dict:
    """给前端环信 Web SDK 用的连接参数（uid + access token）。

    环信域名必须是 VIP6 的 a1-vip6.easecdn.com —— 老域名下任何用户名都
    返回 user not found，容易误判成账号问题。
    """
    store = CX.signer.api.store
    user_obj = store.get_user() or {}
    im = user_obj.get('imAccount') or {}
    user = str(im.get('userName') or '')
    pwd = str(im.get('password') or '')

    # 老版本登录过的 store 里没有 imAccount 字段（那时候还没解析它）。
    # 光提示"重新登录"没用——用户重登后如果 refresh 失败，store 还是旧的。
    # 这里现场补一次：refresh_user 会重新拉用户信息、解析 accountInfo.imAccount 并写盘。
    if not user or not pwd:
        try:
            ok, why = CX.signer.api.refresh_user()
            yktcore.log(f'[群聊] 现场补 IM 账号：ok={ok} {why}')
        except Exception as e:                           # noqa: BLE001
            yktcore.log(f'[群聊] 现场补 IM 账号失败：{e}')
        user_obj = store.get_user() or {}
        im = user_obj.get('imAccount') or {}
        user = str(im.get('userName') or '')
        pwd = str(im.get('password') or '')

    if not user or not pwd:
        # 把诊断信息带回前端：有 cookie 但服务端没给 accountInfo，和根本没登录是两回事
        diag = {
            'hasCookie': bool(store.get_cookie()),
            'userKeys': sorted(user_obj.keys()),
            'imKeys': sorted(im.keys()) if isinstance(im, dict) else [],
        }
        yktcore.log(f'[群聊] 仍然没有 IM 账号：{diag}')
        return {'ok': False, 'error': '服务端没给 IM 账号（cookie '
                + ('在' if diag['hasCookie'] else '不在') + '，user 字段 '
                + ','.join(diag['userKeys']) + '）', 'diag': diag}
    try:
        tok, err = cxchat.login(user, pwd)
    except Exception as e:                               # noqa: BLE001
        return {'ok': False, 'error': friendly_error(e)}
    if err:
        return {'ok': False, 'error': err}
    groups: list[dict] = []
    try:
        groups = cxchat.joined_groups(tok, user)[:20]
    except Exception as e:                               # noqa: BLE001
        yktcore.log(f'[群聊] 取群列表失败：{e}')
    return {'ok': True, 'uid': user, 'token': tok, 'appKey': cxchat.APP_KEY,
            'groups': [{'groupId': g.get('groupId'), 'name': g.get('name')} for g in groups]}


def write_settings(patch: dict) -> dict:
    cfg = read_settings()
    if isinstance(patch.get('autoPlay'), bool):
        cfg['autoPlay'] = patch['autoPlay']
    streams = patch.get('streams') or {}
    if isinstance(streams, dict):
        for k in ('teacher', 'student', 'ppt'):
            if isinstance(streams.get(k), bool):
                cfg['streams'][k] = streams[k]
    ppt = patch.get('ppt') or {}
    if isinstance(ppt, dict):
        for k, lo, hi in (('fps', 5, 30), ('quality', 1, 10)):
            if ppt.get(k) is not None:
                try:
                    cfg['ppt'][k] = max(lo, min(hi, int(ppt[k])))
                except (TypeError, ValueError):
                    pass
    ykt = patch.get('ykt') or {}
    if isinstance(ykt, dict):
        mode = str(ykt.get('signMode') or '').strip().lower()
        if mode in YKT_SIGN_MODES:
            cfg['ykt']['signMode'] = mode
        if isinstance(ykt.get('autoSign'), bool):
            cfg['ykt']['autoSign'] = ykt['autoSign']
    auto_cfg = patch.get('autoSign') or {}
    if isinstance(auto_cfg, dict):
        if auto_cfg.get('pollIntervalSec') is not None:
            try:
                sec = int(auto_cfg['pollIntervalSec'])
                cfg['autoSign']['pollIntervalSec'] = max(
                    autosign.MIN_POLL_INTERVAL,
                    min(autosign.MAX_POLL_INTERVAL, sec),
                )
            except (TypeError, ValueError):
                pass
        if isinstance(auto_cfg.get('webhookUrl'), str):
            wh = auto_cfg['webhookUrl'].strip()
            if not wh or wh.startswith(('http://', 'https://')):
                cfg['autoSign']['webhookUrl'] = wh
    cx = patch.get('cx') or {}
    if isinstance(cx, dict):
        loc = cx.get('location')
        if isinstance(loc, dict):
            cur = cfg['cx']['location']
            if 'address' in loc:
                cur['address'] = str(loc.get('address') or '').strip()
            if loc.get('lat') is not None:
                cur['lat'] = _clamp(loc.get('lat'), -90, 90)
            if loc.get('lng') is not None:
                cur['lng'] = _clamp(loc.get('lng'), -180, 180)
        # 下面这几项与 location 无关，必须在外层判断里 —— 之前误缩进到
        # `if isinstance(loc, dict)` 内部，导致只有同时改坐标时才生效：
        # 单独关「自动刷新」、单独加课都会被静默丢掉。
        ec = cx.get('extraCourses')
        if isinstance(ec, list):
            cfg['cx']['extraCourses'] = _clean_extra_courses(ec)
        if isinstance(cx.get('autoScan'), bool):
            cfg['cx']['autoScan'] = cx['autoScan']
        if isinstance(cx.get('autoSign'), bool):
            cfg['cx']['autoSign'] = cx['autoSign']
    try:
        CRED_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
    except OSError:
        pass
    if 'AUTOSIGN' in globals():
        AUTOSIGN.notify_config_changed()
    return cfg


LIVE_WORDS = ('正在直播', '直播中', '直播', '进行中')


def live_courses(rows: list) -> list:
    """只要真在直播的场次（列表里也含未开始/已结束的）。"""
    out = []
    for c in rows or []:
        try:
            if str(c.get('statusName') or '').strip() in LIVE_WORDS:
                out.append(c)
        except AttributeError:
            continue
    return out


def ppt_stream(streams: list) -> dict | None:
    for s in streams or []:
        if str(s.get('name') or '').strip().upper() == 'PPT':
            return s
    return None


def open_players(api, streams: list, cfg: dict,
                 skip=()) -> tuple[int, str, list, list]:
    """按设置拉起播放器窗口。

    默认只拉老师/学生，PPT 走侧栏内嵌；勾上 streams.ppt 就把 PPT 也开成独立
    窗口，回到最初版本的三路全开（侧栏那路不动，页面布局靠它）。

    skip 是已经开过的流名——自动推流每 12 秒轮一次，靠它避免同一路开两遍。
    返回 (启动数, 提示, 未开播名单, 本次启动的名字)。
    """
    skip = {str(x) for x in (skip or ())}
    want = []
    for s in streams or []:
        name = str(s.get('name') or '')
        if name in skip:
            continue
        if name.upper() == 'PPT':
            if not cfg['streams'].get('ppt', False):
                continue                                # 默认固定走内嵌
        elif name == '老师' and not cfg['streams'].get('teacher', True):
            continue
        elif name == '学生' and not cfg['streams'].get('student', False):
            continue
        want.append(s)
    if not want:
        return 0, '', [], []
    alive, not_yet = [], []
    for s in want:
        if HfutLive.stream_alive(s.get('hls') or '') is False:
            not_yet.append(s)
        else:
            alive.append(s)
    if not alive:
        return (0, '、'.join(s['name'] for s in not_yet[:3]) + ' 尚未开播',
                [s['name'] for s in not_yet], [])
    opened, names = 0, []
    for idx, s in enumerate(alive):
        if idx:
            time.sleep(1.2)              # 错开启动，别让三路同时抢 GPU 上下文
        proc = api.play_proc(s['rtmp'], title=f'HFUTone-{s["name"]}',
                             low_latency=True, no_audio=not HfutLive.wants_audio(s['name']))
        if proc:
            opened += 1
            names.append(s['name'])
            try:
                with STATE.lock:
                    STATE.player_pids.append(proc.pid)
            except Exception:            # noqa: BLE001
                pass
    return opened, '', [s['name'] for s in not_yet], names


# ------------------------------------------------------------- 自动推流
def autopilot_loop(seconds: float = 12.0) -> None:
    """课程一开播就把 PPT 流接上来。

    设置里的「课程直播开始时自动推流」控制这里：默认开着，每 12 秒看一眼
    有没有新开播的课；有就把 PPT 流转码接进侧栏，并按设置顺手拉起
    老师/学生流的播放器。课全部结束就自动收掉。
    """
    while True:
        time.sleep(seconds)
        autopilot_tick()


def autopilot_tick() -> None:
    """自动推流跑一轮。抽成函数是为了能单独跑一次做验证。"""
    cfg = read_settings()
    if not cfg['autoPlay']:
        # 只收自动接的那一路。手动拉的画面（PPT_RELAY.manual）是用户自己点
        # 出来的，关开关不该顺手掐掉；状态也别覆盖成 off——否则用户刚手动
        # 接上的画面，侧栏却写着"自动推流已关闭"，看着像坏了。
        if PPT_RELAY.running and not PPT_RELAY.manual:
            PPT_RELAY.stop()
        with STATE.lock:
            if PPT_RELAY.running and PPT_RELAY.manual:
                # 手动画面还在跑：保持"在播"，且把 lessonId 一起写上——
                # 快速路径靠 lessonId 判"这一场已处理过"，缺了它会每 12 秒重跑一遍。
                STATE.auto.update(phase='playing', message='',
                                  lessonId=str(STATE.auto.get('lessonId') or ''))
            else:
                STATE.auto.update(phase='off', message='自动推流已关闭')
        return
    try:
        api = STATE.ensure_api()
        rows = STATE.courses
        if not rows:
            rows = api.list_lives()
            with STATE.lock:
                STATE.courses = rows
        lives = live_courses(rows)
        if not lives:
            # 手动拉的画面不归自动推流管，别把用户刚接上的画面关掉
            if PPT_RELAY.running and not PPT_RELAY.manual:
                PPT_RELAY.stop()
            with STATE.lock:
                if PPT_RELAY.running and PPT_RELAY.manual:
                    # 手动接的画面还在跑，状态保持"在播"，别写成"等待开播"；
                    # lessonId 一并带上，避免快速路径每轮重跑
                    STATE.auto.update(phase='playing', message='',
                                      lessonId=str(STATE.auto.get('lessonId') or ''))
                else:
                    STATE.auto.update(phase='waiting', lessonId='', title='',
                                      message='等待课程开播', players=0,
                                      playerNames=[])
            return
        cur = lives[0]
        lid = str(cur.get('id') or '')
        with STATE.lock:
            auto = dict(STATE.auto)
        # 这一场已经处理过就别重复开播放器。
        # 注意判据不能用 PPT_RELAY.running：勾了「PPT 独立窗口」时 relay 是停的
        # （PPT 交给 mpv），用 relay 判会每 12 秒重开一遍 mpv 窗口。
        # 改成看状态里的 lessonId 是否已对上。
        if auto.get('lessonId') == lid and auto.get('phase') in ('playing', 'external'):
            done = list(auto.get('playerNames') or [])
            opened, _, _, names = open_players(
                api, get_streams(api, lid), cfg, skip=done)
            if names:
                with STATE.lock:
                    STATE.auto.update(playerNames=done + names,
                                      players=len(done) + len(names))
            return                                        # 已经在推这一场
        streams = get_streams(api, lid)
        ppt = ppt_stream(streams)
        res = {'ok': False, 'error': ''}
        want_ppt_window = bool(cfg['streams'].get('ppt'))
        if not ppt:
            res['error'] = '这堂课没有 PPT 流'
        elif HfutLive.stream_alive(ppt.get('hls') or '') is False:
            res['error'] = '画面还没推上来，继续等'
        elif want_ppt_window:
            # 勾了「PPT 独立窗口」：PPT 交给 mpv 播，侧栏那路就不再开 ffmpeg。
            # 早先这里无条件 PPT_RELAY.start()，等于同一路流被 ffmpeg 和 mpv
            # 各解一遍，CPU 白烧一倍；而且内嵌失败还会被下面的判断静默放过，
            # 用户只看到侧栏一片黑。
            if PPT_RELAY.running and not PPT_RELAY.manual:
                PPT_RELAY.stop()
            res = {'ok': True, 'error': '', 'external': True}
        else:
            res = PPT_RELAY.start(ppt.get('rtmp') or ppt.get('hls') or '',
                                  title=str(cur.get('name') or ''),
                                  fps=cfg['ppt']['fps'], quality=cfg['ppt']['quality'])
        # 内嵌那路没就绪、且用户也没要独立 PPT 窗口 —— 这一场就整场不接，
        # 别让侧栏黑着还显示"直播中"。
        if not res.get('ok') and not want_ppt_window:
            with STATE.lock:
                STATE.auto.update(phase='no-ppt' if not ppt else 'starting',
                                  lessonId=lid, title=str(cur.get('name') or ''),
                                  message=res.get('error', ''))
            return
        # 这一场已经开过的播放器别再开一遍（PPT 反复重试时尤其要防）
        done = list(auto.get('playerNames') or []) if auto.get('lessonId') == lid else []
        opened, note, _, names = open_players(api, streams, cfg, skip=done)
        # PPT 交给独立窗口时，侧栏没有内嵌画面，状态要说清楚（别显示"直播中"）
        if res.get('external'):
            phase = 'external'
        elif res.get('ok'):
            phase = 'playing'
        else:
            phase = 'error'
        with STATE.lock:
            STATE.auto.update(phase=phase,
                              lessonId=lid, title=str(cur.get('name') or ''),
                              message=('' if res.get('ok') else res.get('error', '')),
                              players=len(done) + opened,
                              playerNames=done + names, at=time.time())
    except Exception as e:                            # noqa: BLE001
        with STATE.lock:
            STATE.auto.update(phase='error', message=friendly_error(e))


# --------------------------------------------------------------------- 凭据
def read_creds() -> tuple[str, str]:
    """读取凭据：先看自己的配置文件，再回落到原程序的注册表项。"""
    try:
        if CRED_FILE.exists():
            blob = json.loads(CRED_FILE.read_text('utf-8'))
            u, p = blob.get('username') or '', blob.get('password') or ''
            if u and p:
                return str(u), str(p)
    except (OSError, ValueError):
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as k:
            u = winreg.QueryValueEx(k, 'Username')[0]
            p = winreg.QueryValueEx(k, 'Password')[0]
            return str(u), str(p)
    except Exception:
        return os.environ.get('HFUT_USER', ''), os.environ.get('HFUT_PASS', '')


def write_creds(username: str, password: str) -> str:
    """保存凭据：优先写自己的配置文件（原注册表项可能无写权限）。

    返回实际使用的存储位置，便于前端提示。
    """
    try:
        CRED_DIR.mkdir(parents=True, exist_ok=True)
        CRED_FILE.write_text(json.dumps({
            'username': username, 'password': password,
        }, ensure_ascii=False), 'utf-8')
        saved = 'file'
    except OSError:
        saved = ''
    # 尽力同步到注册表（原程序也读这里），失败不影响保存
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, 'Username', 0, winreg.REG_SZ, username)
            winreg.SetValueEx(k, 'Password', 0, winreg.REG_SZ, password)
        if not saved:
            saved = 'registry'
    except Exception:
        pass
    return saved or 'none'


# --------------------------------------------------------------------- 状态
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.api: HfutLive | None = None
        self.courses: list[dict] = []
        self.loaded = False
        self.loading = False
        self.error = ''
        self.status = '正在初始化…'
        self.username = ''
        self._done = threading.Event()
        self._done.set()
        self.offline = False
        self.cached_at = ''
        self.net = {}
        self.stream_cache: dict[str, tuple[float, list]] = {}
        self.player_pids: list[int] = []          # 我们拉起的播放器，供一键关闭
        self.stream_ttl = 45          # 流地址短时间内不变，缓存 45 秒
        self.auto = {'phase': 'waiting', 'lessonId': '', 'title': '',
                     'message': '等待课程开播', 'players': 0,
                     'playerNames': [], 'at': 0}

    def ensure_api(self, fresh: bool = False) -> HfutLive:
        if fresh:
            self.api = None
        if self.api is None:
            u, p = read_creds()
            self.username = u
            if not u or not p:
                raise HfutLiveError('还没有保存学号和密码，点右上角「账号」填一下。')
            self.api = HfutLive(u, p)
        return self.api

    def load(self, force: bool = False) -> None:
        with self.lock:
            if self.loading:
                pending = self._done
            else:
                pending = None
                self.loading = True
                self._done = threading.Event()
                self.error = ''
                self.status = '正在获取最新课表…'
        if pending is not None:
            pending.wait(timeout=30)          # 已经有一轮在跑，等它结束再返回
            return

        # 先花 ~4 秒探测校内可达性。校外宽带下 CAS 能过、内网必超时，
        # 直接走离线缓存，省掉 30 秒的登录等待。
        net = check_intranet()
        with self.lock:
            self.net = net
        if not net.get('api'):
            cached = _read_courses_cache()
            if cached:
                with self.lock:
                    self.courses = cached['courses']
                    self.cached_at = cached['ts']
                    self.offline = True
                    self.loaded = True
                    self.error = ''
                    self.status = f'离线数据 · {cached["ts"]} 缓存'
                    self.loading = False
                    self._done.set()
                return

        try:
            api = self.ensure_api(fresh=force)
            if force:
                api.login(force=True)
            else:
                api.login()
            courses = api.list_lives()
            with self.lock:
                self.courses = courses
                self.loaded = True
                self.offline = False
                self.status = f'就绪 · {len(courses)} 场直播'
            try:
                COURSES_CACHE.write_text(json.dumps({
                    'ts': time.strftime('%m-%d %H:%M'), 'courses': courses,
                }, ensure_ascii=False), 'utf-8')
            except OSError:
                pass
        except Exception as e:                       # noqa: BLE001
            net = check_intranet()
            cached = _read_courses_cache()
            with self.lock:
                self.net = net
                self.error = friendly_error(e)
                if cached and not self.courses:
                    self.courses = cached['courses']
                    self.cached_at = cached['ts']
                    self.offline = True
                    self.status = f'离线数据 · {cached["ts"]} 缓存'
                else:
                    self.status = '加载失败'
        finally:
            with self.lock:
                self.loading = False
                self._done.set()


def get_streams(api, live_id: str) -> list:
    """带缓存的取流：点播放时省掉一次网络往返（约 200–500ms）。"""
    now = time.time()
    with STATE.lock:
        hit = STATE.stream_cache.get(live_id)
        if hit and now - hit[0] < STATE.stream_ttl:
            return hit[1]
    streams = api.streams(live_id)
    with STATE.lock:
        STATE.stream_cache[live_id] = (now, streams)
        if len(STATE.stream_cache) > 200:
            oldest = min(STATE.stream_cache.items(), key=lambda kv: kv[1][0])[0]
            STATE.stream_cache.pop(oldest, None)
    return streams


def _read_courses_cache() -> dict | None:
    try:
        blob = json.loads(COURSES_CACHE.read_text('utf-8'))
        if blob.get('courses'):
            return blob
    except (OSError, ValueError):
        pass
    return None


def append_client_log(body: dict) -> None:
    """只接收白名单诊断字段；不记录表单正文、凭据或完整请求头。"""
    entry = {
        'at': str(body.get('at') or time.strftime('%Y-%m-%dT%H:%M:%S'))[:40],
        'version': str(body.get('version') or '')[:40],
        'kind': str(body.get('kind') or 'error')[:30],
        'message': str(body.get('message') or '')[:1000],
        'stack': str(body.get('stack') or '')[:4000],
        'source': str(body.get('source') or '')[:500],
        'line': body.get('line') if isinstance(body.get('line'), int) else 0,
        'href': str(body.get('href') or '')[:500],
    }
    line = json.dumps(entry, ensure_ascii=False) + '\n'
    with CLIENT_LOG_LOCK:
        CLIENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        # 单文件最多约 512 KB，异常刷屏也不会无限占盘。
        if CLIENT_LOG.exists() and CLIENT_LOG.stat().st_size > 512 * 1024:
            CLIENT_LOG.write_text('', encoding='utf-8')
        with CLIENT_LOG.open('a', encoding='utf-8') as f:
            f.write(line)


STATE = State()


# --------------------------------------------------------------- 雨课堂会话
class YktState:
    """雨课堂那一栏的会话状态。

    cookie 落在 yktcore.Store（DPAPI 加密），这里只放内存态：课程列表、
    二维码 token、忙碌标记。协议细节全在 src/yktapi.py。
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.ykt = yktapi.Ykt()
        self.lessons: list[dict] = []
        self.loaded = False
        self.loading = False
        self.error = ''
        self.busy = False

    @property
    def logged_in(self) -> bool:
        return bool(self.ykt.cookie)

    def refresh(self, force: bool = False) -> None:
        """拉一次进行中的课堂。cookie 失效就把状态标成过期。"""
        with self.lock:
            if self.loading:
                return
            self.loading = True
            self.error = ''
        try:
            if not self.logged_in:
                with self.lock:
                    self.lessons = []
                    self.loaded = True
                    self.status_text = '未登录'
                return
            self.ykt.ensure_user()
            rows = self.ykt.on_lesson()
            with self.lock:
                self.lessons = rows
                self.loaded = True
                self.status_text = f'就绪 · {len(rows)} 节在播'
        except yktapi.AuthExpired as e:
            self.ykt.clear_cookie()              # 凭据死透了，清掉，让用户重扫
            with self.lock:
                self.lessons = []
                self.loaded = True
                self.error = str(e)
                self.status_text = '登录已过期'
        except Exception as e:                   # noqa: BLE001
            with self.lock:
                self.error = str(e)
                self.status_text = '加载失败'
                self.loaded = True
        finally:
            with self.lock:
                self.loading = False

    def start_monitor(self, interval: float = 25.0) -> None:
        """后台盯盘：定时拉一次"进行中的课堂"，让前端那 15 秒一次的 pull
        能拿到最新状态（红点、未签到提示都靠它）。

        签到只有开课那几分钟有效，界面停在直播那栏也该被叫起来。
        """
        if getattr(self, '_monitor_on', False):
            return
        self._monitor_on = True

        def loop():
            while True:
                time.sleep(interval)
                try:
                    if self.logged_in and not self.busy:
                        self.refresh()
                except Exception:            # noqa: BLE001  后台线程不能因为一次失败就死
                    pass

        threading.Thread(target=loop, name='ykt-monitor', daemon=True).start()

    def payload(self) -> dict:
        with self.lock:
            return {
                'loggedIn': self.logged_in,
                'status': getattr(self, 'status_text', '未登录'),
                'error': self.error,
                'loaded': self.loaded,
                'busy': self.busy,
                'lessons': self.lessons,
                'user': dict(self.ykt.user) if self.ykt.user else {},
                'identity': 'app' if yktcore.APP_IDENTITY else 'web',
            }


YKT = YktState()

# 身份恢复必须放在模块导入这一层：打包入口是 app_qt.pyw，Qt 自己起服务，
# 根本不走 main()。放 main() 里等于永不执行，App 登录重启后就白做（identity
# 退回 web，界面上看着"什么也没变"）。
try:
    yktcore.restore_identity(YKT.ykt.store)
    if yktcore.APP_IDENTITY:
        yktcore.set_app_uid(str((YKT.ykt.user or {}).get('userId') or ''))
except Exception:                                    # noqa: BLE001
    pass


class AppLoginState:
    """雨课堂 App 登录的临时状态（方案 A）。

    支持三种登录方式（kind）：1=手机号+密码  2=邮箱+密码  3=手机号+短信验证码。
    短信那条是两步，每步都要一次人机验证：
        1) stage='send'   —— 发码，POST /api/v3/user/code/send
        2) stage='login'  —— 核码 + 登录，POST /code/verify 再 POST /login/app
    `stage` 就是告诉 callback 这次滑完的人机验证该拿去哪一步用。

    账号密码/验证码只在内存里放几分钟，用完立刻清掉，不落盘。
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.account = ''
        self.password = ''
        self.kind = 1
        self.stage = ''              # ''/send/login —— 这次人机验证给哪一步用
        self.state = 'idle'          # idle/waiting/sent/verifying/ok/fail
        self.message = ''
        self.user = {}
        self.verify = {}             # verify_identity() 的结果
        self.at = 0.0

    def begin(self, account: str, password: str, kind: int = 1,
              stage: str = 'login') -> None:
        with self.lock:
            self.account = account
            self.password = password
            self.kind = kind
            self.stage = stage
            self.state = 'waiting'
            self.message = '等待人机验证'
            self.user = {}
            self.verify = {}
            self.at = time.time()

    def mark_sent(self) -> None:
        with self.lock:
            self.state = 'sent'
            self.password = ''
            self.stage = ''
            self.message = '验证码已发送'

    def mark_sending(self, message: str = '正在发送验证码…') -> None:
        """发码请求已发出、还在等服务端回执。

        没有这个中间态时 state 一直停在 waiting，前端只能显示「请在下面完成
        人机验证」——人早滑完了，看着像什么都没发生。send_sms 的 timeout 是
        25 秒，这段静默足够让人以为卡死。
        """
        with self.lock:
            self.state = 'sending'
            self.message = message

    def clear(self) -> None:
        with self.lock:
            self.account = ''
            self.password = ''
            self.stage = ''
            self.state = 'idle'
            self.message = ''

    def payload(self) -> dict:
        with self.lock:
            return {'ok': True, 'state': self.state, 'message': self.message,
                    'account': self.account, 'stage': self.stage,
                    'kind': self.kind, 'user': dict(self.user),
                    'verify': dict(self.verify), 'at': self.at}


APPLOGIN = AppLoginState()


# ------------------------------------------------------------- 学习通签到
class CxState:
    """学习通签到状态。协议在 src/cxapi.py + cxsign.py，这里只管并发和缓存。

    跟雨课堂分开一个对象，因为它是另一套账号体系（学习通 cookie vs
    雨课堂 sessionid），两边可以同时登录、互不影响。
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.signer = cxsign.CxSigner()
        self.qr_uuid = ''
        self.qr_enc = ''
        self.qr_state = 'idle'
        self.qr_msg = ''
        self.qr_born = 0.0
        self.last_scan = 0.0
        self.error = ''

    @property
    def logged_in(self) -> bool:
        return self.signer.ready

    def payload(self) -> dict:
        """给前端的一份完整快照。不触发网络请求，扫不扫由 /api/cx/scan 决定。"""
        with self.lock:
            try:
                prof = self.signer.profile()
            except Exception:                        # noqa: BLE001
                prof = {'ok': False}
            snap = self.signer._snapshot() if self.signer._cache else {
                'ok': True, 'courses': [], 'signs': []}
            return {
                'ok': True,
                'loggedIn': self.logged_in,
                'profile': prof,
                'courses': snap.get('courses') or [],
                'signs': snap.get('signs') or [],
                'lastScan': self.last_scan,
                'error': self.error,
                'qr': {'state': self.qr_state, 'msg': self.qr_msg,
                       'uuid': self.qr_uuid},
            }

    def sync_location(self) -> None:
        """把设置里的默认坐标灌给签到器。

        每次扫描/签到前调一次，这样在设置里改完坐标不用重启就生效。
        """
        loc = read_settings()['cx']['location']
        self.signer.default_location = dict(loc or {})

    def sync_extra_courses(self) -> None:
        """把设置里的手动加课灌给签到器（课程中心没收录的课，如商益学）。"""
        self.signer.extra_courses = list(
            read_settings()['cx']['extraCourses'] or [])

    def scan(self, force: bool = True) -> dict:
        with self.lock:
            try:
                self.sync_location()
                self.sync_extra_courses()
                data = self.signer.scan(force=force)
                self.last_scan = time.time()
                self.error = '' if data.get('ok') else str(data.get('error') or '')
                if data.get('ok') and 'AUTOSIGN' in globals():
                    AUTOSIGN.sync_cx_signs(data.get('signs') or [])
                return data
            except Exception as exc:                 # noqa: BLE001
                self.error = friendly_error(exc)
                return {'ok': False, 'courses': [], 'signs': [], 'error': self.error}


CX = CxState()


def _on_backend_im_activities(items: list[dict], realtime: bool) -> None:
    """后端环信长连接收到群聊签到后，直接并入列表并触发秒签（不依赖 WebView）。"""
    added = cxsign.set_chat_signs(items)
    queued = AUTOSIGN.on_im_events(items, realtime=realtime)
    if added or queued:
        tag = '实时推送' if realtime else '漫游补录'
        yktcore.log(f'[后端IM] {tag}：新增 {added} 条，入秒签队 {queued} 条')


CX_IM = cxim.BackendIMClient(
    get_cx_state=lambda: CX,
    on_activities=_on_backend_im_activities,
)

AUTOSIGN = autosign.AutoSignEngine(
    get_settings=read_settings,
    get_cx=lambda: CX,
    get_ykt=lambda: YKT,
    qr_grabber=qr_from_relay,
    im_status_fn=CX_IM.status_payload,
)


def _num(v):
    """把前端传来的坐标转成 float；空串/None 一律当「没给」。"""
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def state_payload() -> dict:
    with STATE.lock:
        return {
            'status': STATE.status,
            'error': STATE.error,
            'username': STATE.username,
            'loaded': STATE.loaded,
            'offline': STATE.offline,
            'cachedAt': STATE.cached_at,
            'net': STATE.net,
            'courses': [
                {
                    'id': c.get('id'),
                    'courseId': c.get('courseId'),
                    'userId': c.get('userId'),
                    'name': c.get('name'),
                    'statusName': c.get('statusName'),
                    'startTime': c.get('startTime'),
                    'endTime': c.get('endTime'),
                    'classroomName': c.get('classroomName'),
                    'organizationName': c.get('organizationName'),
                    'moocWebPath': c.get('moocWebPath'),
                }
                for c in STATE.courses
            ],
        }


# --------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = 'HFUTone/1.0'

    # 静默日志，只在出错时打
    def log_message(self, fmt, *args):               # noqa: A003
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8')

    def _body(self) -> dict:
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return {}

    def _cx_get(self, path: str):
        """学习通 GET 路由。签到是写操作，全在 POST 那边。"""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        def one(name: str, default: str = '') -> str:
            return (q.get(name) or [default])[0]

        if path == '/api/cx/captcha/start':
            # 取一张滑块验证码：返回背景/拼图地址 + token/iv/ts，前端拖完再回来换 validate
            ref = one('referer') or 'https://mobilelearn.chaoxing.com/'
            try:
                data = cxapi.CxCaptcha().fetch(ref)
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': friendly_error(e)})
                return
            if not data.get('ok'):
                self._json(data)
                return
            def _proxy(u: str) -> str:
                return '/api/cx/captcha/img?u=' + urllib.parse.quote(u, safe='') if u else ''
            data['bg'] = _proxy(data.get('bg') or '')
            data['slice'] = _proxy(data.get('slice') or '')
            self._json(data)
            return

        if path == '/api/cx/captcha/img':
            # 验证码图在 captcha-b.chaoxing.com，带 Referer 才给，走本地代理也避开前端跨域
            u = one('u')
            if not u.startswith(('http://', 'https://')):
                self._send(400, b'bad url', 'text/plain; charset=utf-8')
                return
            try:
                raw = yktcore.fetch_bytes(u, referer='https://captcha.chaoxing.com/',
                                          timeout=20)
            except Exception:                        # noqa: BLE001
                raw = None
            if not raw:
                self._send(502, b'no image', 'text/plain; charset=utf-8')
                return
            self._send(200, raw, 'image/png')
            return

        if path == '/api/cx/im/session':
            self._json(im_session())
            return

        if path == '/api/cx/im/status':
            self._json(CX_IM.status_payload())
            return

        if path == '/api/cx/state':
            self._json(CX.payload())
            return

        if path == '/api/cx/qr':
            # 取一个二维码登录用的 uuid/enc。图由前端用 uuid 画，不依赖外链。
            try:
                data = CX.signer.api.login_qr_new()
                with CX.lock:
                    CX.qr_uuid = data['uuid']
                    CX.qr_enc = data['enc']
                    CX.qr_state = 'waiting'
                    CX.qr_msg = '等待扫码…'
                    CX.qr_born = time.time()
                if not data['uuid']:
                    self._json({'ok': False, 'error': '取二维码失败'})
                    return
                self._json({'ok': True, 'uuid': data['uuid'], 'enc': data['enc']})
            except Exception as e:                   # noqa: BLE001
                with CX.lock:
                    CX.qr_state = 'error'
                    CX.qr_msg = friendly_error(e)
                self._json({'ok': False, 'error': friendly_error(e)})
            return

        if path == '/api/cx/qr/poll':
            # 前端每 2 秒问一次。扫码成功后就拉用户信息（拿 sc/cid）。
            with CX.lock:
                uid, enc = CX.qr_uuid, CX.qr_enc
            if not uid:
                self._json({'ok': False, 'state': 'idle', 'message': '没有二维码'})
                return
            try:
                state, msg = CX.signer.api.login_qr_poll(uid, enc)
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'state': 'error',
                            'message': friendly_error(e)})
                return
            with CX.lock:
                CX.qr_state = state
                CX.qr_msg = msg
                if state in ('ok', 'fail'):
                    CX.qr_uuid = ''
            if state == 'ok':
                CX_IM.notify_login_changed()
            self._json({'ok': state == 'ok', 'state': state, 'message': msg,
                        'loggedIn': CX.logged_in,
                        'profile': CX.signer.profile() if CX.logged_in else {}})
            return

        if path == '/api/cx/scan':
            # 只读地扫一遍课程活动。放 GET 是因为它不改服务端状态。
            self._json(CX.scan(force=(one('force') != '0')))
            return

        self._json({'error': 'unknown endpoint'}, 404)

    def _cx_post(self, path: str):
        body = self._body()

        if path == '/api/cx/login':
            phone = str(body.get('phone') or '').strip()
            pwd = str(body.get('password') or '')
            if not phone or not pwd:
                self._json({'ok': False, 'error': '要手机号和密码'})
                return
            try:
                ok, msg = CX.signer.api.login_by_password(phone, pwd)
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': friendly_error(e)})
                return
            with CX.lock:
                CX.error = '' if ok else msg
            if ok:
                CX_IM.notify_login_changed()
            self._json({'ok': ok, 'error': '' if ok else msg,
                        'profile': CX.signer.profile() if ok else {}})
            return

        if path == '/api/cx/logout':
            try:
                CX.signer.api.logout()
            except Exception:                        # noqa: BLE001
                pass
            with CX.lock:
                CX.qr_uuid = CX.qr_enc = ''
                CX.qr_state = 'idle'
            CX_IM.notify_login_changed()
            self._json({'ok': True})
            return

        if path == '/api/cx/im/reconnect':
            CX_IM.notify_login_changed()
            self._json(CX_IM.status_payload())
            return

        if path == '/api/cx/scan':
            self._json(CX.scan(force=bool(body.get('force', True))))
            return

        if path == '/api/cx/sign':
            item = body.get('item') or {}
            if not isinstance(item, dict) or not item.get('activeId'):
                self._json({'ok': False, 'error': '缺少签到项'})
                return
            enc_arg = str(body.get('enc') or '')
            code_arg = str(body.get('code') or '')
            qr_note = ''
            # 群聊里拿到的活动只给了 activeId：签到类型（普通/二维码/手势/位置/
            # 签到码）得问一次活动详情。cxsign 内部对"课程中心扫出来的"项会自己
            # 补，但这条路进来的项没有 signType，先在这儿补齐再走同一套分支。
            if not str(item.get('signType') or ''):
                try:
                    info = CX.signer.api.active_info(str(item.get('activeId') or ''))
                    other = info.get('otherId')
                    if other is not None:
                        st = cxapi.SIGN_TYPES.get(cxapi._int(other), 'normal')
                        item = dict(item, signType=st)
                        if not item.get('url'):
                            item['url'] = str(info.get('url') or '')
                        if not item.get('courseId'):
                            item['courseId'] = str(info.get('courseId') or '')
                        if not item.get('name'):
                            item['name'] = str(info.get('name') or '')
                        yktcore.log(f'[学习通] 群聊活动 {item.get("activeId")} 判定为 {st}')
                except Exception as e:               # noqa: BLE001
                    yktcore.log(f'[学习通] 群聊活动补类型失败：{e}')
            # 二维码签到：没给 enc 就先去正在播的课件画面里找码（学习通签到码
            # 也是打在老师屏幕/PPT 上的，和雨课堂动态码同一种东西）。
            # 找不到再退回前端弹框手填，不把球直接踢回用户。
            if str(item.get('signType') or '') == 'qrcode' and not enc_arg:
                grab = qr_from_relay(tries=5, hosts=yktqr.CX_HOSTS, label='学习通')
                if grab.get('ok'):
                    try:
                        qr_data, qr_err = CX.signer.parse_qr(grab['urls'][0])
                    except Exception as e:           # noqa: BLE001
                        qr_data, qr_err = {}, friendly_error(e)
                    if qr_err:
                        qr_note = '画面里的码解析失败：' + qr_err
                    else:
                        aid_in_qr = str(qr_data.get('activeId') or '')
                        if aid_in_qr and aid_in_qr != str(item.get('activeId') or ''):
                            # 抓到的可能是别的课/上一场的码，宁可不动
                            qr_note = f'画面里的码属于另一场签到（{aid_in_qr}），没有提交'
                        else:
                            enc_arg = str(qr_data.get('enc') or '')
                            code_arg = code_arg or str(qr_data.get('code') or '')
                            qr_note = 'ok'
                else:
                    qr_note = grab.get('note') or '画面里没找到二维码'
            try:
                CX.sync_location()
                res = CX.signer.sign(
                    item,
                    pattern=str(body.get('pattern') or ''),
                    code=code_arg,
                    enc=enc_arg,
                    address=str(body.get('address') or ''),
                    lat=_num(body.get('lat')), lng=_num(body.get('lng')),
                    validate=str(body.get('validate') or ''),
                    photo_path=str(body.get('photo') or ''),
                    with_face=bool(body.get('face')),
                )
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': friendly_error(e)})
                return
            if qr_note:
                res['qrNote'] = qr_note
                res['autoQr'] = (qr_note == 'ok')
                if qr_note != 'ok' and not res.get('ok'):
                    res['detail'] = ((res.get('detail') or '')
                                     + ' · ' + qr_note).strip(' ·')
            if res.get('ok'):
                AUTOSIGN.mark_resolved(
                    'cx', str(item.get('activeId') or ''),
                    detail=res.get('detail') or '手动签到成功',
                    count_signed=False,
                )
            self._json(res)
            return

        if path == '/api/cx/sign_all':
            try:
                CX.sync_location()
                res = CX.signer.sign_all_auto(
                    address=str(body.get('address') or ''),
                    lat=_num(body.get('lat')), lng=_num(body.get('lng')))
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': friendly_error(e)})
                return
            self._json({'ok': True, **res})
            return

        if path == '/api/cx/im/events':
            # 前端环信 Web SDK 收到的群消息，把里头的签到活动交过来。
            # 群消息只能由客户端 SDK 拿（REST 没有历史消息接口），所以这条路
            # 是「群聊签到」唯一的数据来源。同时触发后端 IM 秒签队列。
            items = body.get('items')
            if not isinstance(items, list):
                self._json({'ok': False, 'error': 'items 要数组'})
                return
            realtime = bool(body.get('realtime', True))
            added = cxsign.set_chat_signs(items)
            queued = AUTOSIGN.on_im_events(items, realtime=realtime)
            if added:
                yktcore.log(f'[群聊] 收到 {added} 条新活动（共 {len(cxsign._CHAT_SIGNS)} 条，入秒签队 {queued} 条）')
            self._json({'ok': True, 'added': added, 'queued': queued,
                        'total': len(cxsign._CHAT_SIGNS)})
            return

        if path == '/api/cx/captcha/verify':
            x = body.get('x')
            token = str(body.get('token') or '')
            iv = str(body.get('iv') or '')
            ts = int(body.get('ts') or 0)
            ref = str(body.get('referer') or 'https://mobilelearn.chaoxing.com/')
            if x is None or not token:
                self._json({'ok': False, 'error': '缺少 x 或 token'})
                return
            try:
                val, err = cxapi.CxCaptcha().submit(float(x), token, ref, iv=iv, ts=ts)
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': friendly_error(e)})
                return
            self._json({'ok': bool(val), 'validate': val, 'error': err})
            return

        if path == '/api/cx/chat/scan':
            # 群聊里的签到通知（环信 IM）。课程中心不收录的班只有这条路能看到。
            # 只做"刷新一次最近消息"，不是常驻监听。
            im = (CX.signer.api.store.get_user() or {}).get('imAccount') or {}
            user = str(im.get('userName') or '')
            pwd = str(im.get('password') or '')
            if not user or not pwd:
                self._json({'ok': False, 'error': '没有 IM 账号：在学习通里重新登录一次即可'})
                return
            try:
                tok, err = cxchat.login(user, pwd)
                if err:
                    self._json({'ok': False, 'error': err})
                    return
                groups = cxchat.joined_groups(tok, user)
                limit = int(body.get('limit') or 20)
                acts: list = []
                for g in groups[:int(body.get('groups') or 30)]:
                    try:
                        msgs = cxchat.group_messages(tok, g['groupId'], limit=limit)
                    except Exception:                # noqa: BLE001
                        continue
                    for a in cxchat.activities_from_messages(msgs):
                        a['groupName'] = g.get('name') or ''
                        acts.append(a)
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': friendly_error(e)})
                return
            self._json({'ok': True, 'groups': len(groups), 'items': acts,
                        'at': int(time.time())})
            return

        if path == '/api/cx/qr_image':
            # 从一张图里读学习通签到码。桌面版可以直接截屏/存图，安卓可以截屏后
            # 分享/粘贴进来 —— 没有直播流的学校靠这条也能免手抄。
            b64 = str(body.get('image') or '')
            local = str(body.get('path') or '')
            raw = b''
            try:
                if b64:
                    if ',' in b64:
                        b64 = b64.split(',', 1)[1]
                    raw = base64.b64decode(b64)
                elif local:
                    raw = Path(local).read_bytes()
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': friendly_error(e)})
                return
            if not raw:
                self._json({'ok': False, 'error': '没有图片'})
                return
            if not yktqr.decoder_available():
                self._json({'ok': False, 'error': '本机缺少图片二维码解码库（请安装 opencv-python-headless）'})
                return
            codes = yktqr.decode_bytes(raw)
            cx_codes = yktqr.pick_cx(codes)
            if not cx_codes:
                self._json({'ok': False, 'error': '图里没找到学习通签到码',
                            'codes': codes[:3]})
                return
            try:
                data, err = CX.signer.parse_qr(cx_codes[0])
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': friendly_error(e)})
                return
            self._json({'ok': not err, 'data': data, 'error': err,
                        'code': cx_codes[0]})
            return

        if path == '/api/cx/qr_parse':
            text = str(body.get('text') or '')
            if not text:
                self._json({'ok': False, 'error': '没有二维码内容'})
                return
            try:
                data, err = CX.signer.parse_qr(text)
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': friendly_error(e)})
                return
            self._json({'ok': not err, 'data': data, 'error': err})
            return

        self._json({'error': 'unknown endpoint'}, 404)

    # ------------------------------------------------------ 雨课堂 GET 路由
    def _ykt_get(self, path: str):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        def one(name: str, default: str = '') -> str:
            return (q.get(name) or [default])[0]

        if path == '/api/ykt/applogin/page':
            # 人机验证页：内嵌 TCaptcha，滑完 POST 回 /applogin/callback
            html = yktapplogin.captcha_html('/api/ykt/applogin/callback')
            self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
            return

        # 人机验证流程的进度查询。只读，走 GET。
        # 之前只注册在 _ykt_post 里，而前端 appPoll() 用 api() 不带 body 调用 → 发的是
        # GET → 落到这里 404（api() 拿到 !r.ok 直接 throw，而那处 .then 没有 .catch），
        # 于是「验证完不回填页面」：state 从来没被读到过一次，iframe 里的验证页只能干等。
        # 端点同时保留 POST 分支做兼容，但真正的修复是这里必须有。
        if path == '/api/ykt/applogin/state':
            self._json(APPLOGIN.payload())
            return

        if path == '/api/ykt/state':
            self._json(YKT.payload())
            return

        # 扫码登录已删除：实测扫码只能拿 web 凭据，而 App 登录一次就能同时
        # 走 App 与网页两套接口（on-lesson 两种头都 200）。同一个账号
        # 只保留一套登录态，留着扫码只会把 App 身份顶掉。

        if path == '/api/ykt/qrframe':
            # 手动/调试用：从正在播的画面里读一次码
            self._json(qr_from_relay(tries=4))
            return

        if path == '/api/ykt/img':
            # 课件页/二维码图片是七牛上的外链，走本地代理避开前端跨域/防盗链
            url = one('u')
            if not url:
                self._send(400, b'no url', 'text/plain; charset=utf-8')
                return
            data = YKT.ykt.download(url, referer=yktcore.SERVER + '/')
            if not data:
                yktcore.log(f'[课件] 下载失败 {url[:120]}')
                self._send(404, b'download failed', 'text/plain; charset=utf-8')
                return
            # 按魔数给类型：七牛上课件页基本是 jpeg，早先写死 image/png
            # 虽然浏览器多半能认出，但严格模式/懒加载下会当成坏图。
            if data[:3] == b'\xff\xd8\xff':
                ctype = 'image/jpeg'
            elif data[:8] == b'\x89PNG\r\n\x1a\n':
                ctype = 'image/png'
            elif data[:4] == b'RIFF':
                ctype = 'image/webp'
            else:
                ctype = 'application/octet-stream'
            self._send(200, data, ctype)
            return

        if path == '/api/ykt/profile':
            try:
                user = YKT.ykt.profile() if YKT.logged_in else {}
            except Exception as e:               # noqa: BLE001
                self._json({'ok': False, 'error': str(e), 'user': {}})
                return
            self._json({'ok': bool(user.get('name')), 'user': user})
            return

        if path == '/api/ykt/lessons':
            if not YKT.logged_in:
                self._json({'ok': False, 'error': '未登录'}, 401)
                return
            YKT.refresh()
            self._json({'ok': not YKT.error, 'error': YKT.error,
                        'lessons': YKT.lessons})
            return

        if path == '/api/ykt/pres':
            lid = one('lesson')
            pid = one('pres')
            try:
                items = YKT.ykt.lesson_info(lid)
                if not items:
                    self._json({'ok': False, 'error': '这堂课还没有推送过课件'})
                    return
                # 指定了 pres 就挑那一份，否则取最新的一份
                pick = items[0]
                if pid:
                    for it in items:
                        if it.get('id') == pid:
                            pick = it
                            break
                self._json({'ok': True, 'lessonId': lid, 'presId': pick.get('id'),
                            'title': pick.get('title') or '课堂课件',
                            'slides': pick.get('slides') or [],
                            'total': len(items)})
            except Exception as e:               # noqa: BLE001
                self._json({'ok': False, 'error': str(e)})
            return

        if path == '/api/ykt/preslist':
            lid = one('lesson')
            try:
                items = YKT.ykt.lesson_info(lid)
                self._json({'ok': True, 'items': items})
            except Exception as e:               # noqa: BLE001
                self._json({'ok': False, 'error': str(e), 'items': []})
            return

        if path == '/api/ykt/questions':
            lid = one('lesson')
            if not lid:
                self._json({'ok': False, 'error': '缺少 lesson', 'items': []})
                return
            try:
                wait = float(one('wait', '12') or 12)
            except ValueError:
                wait = 12.0
            try:
                items, err = YKT.ykt.questions(lid, wait)
            except Exception as e:               # noqa: BLE001
                items, err = [], str(e)
            self._json({'ok': not err, 'error': err, 'lessonId': lid,
                        'items': items, 'at': int(time.time())})
            return

        self._json({'error': 'unknown ykt endpoint'}, 404)

    # ----------------------------------------------------- 雨课堂 POST 路由
    def _ykt_post(self, path: str):
        body = self._body()

        # ---- 方案 A：App 登录（免扫动态码的前置条件）----
        if path == '/api/ykt/applogin/start':
            acc = str(body.get('account') or '').strip()
            pwd = str(body.get('password') or '')
            kind = int(body.get('kind') or 1)
            if not acc or not pwd:
                self._json({'ok': False,
                            'error': '手机号/账号和验证码（或密码）都要填'})
                return
            APPLOGIN.begin(acc, pwd, kind, stage='login')
            self._json({'ok': True, 'state': 'waiting',
                        'page': '/api/ykt/applogin/page'})
            return

        if path == '/api/ykt/applogin/sendsms':
            # 短信登录第一步：发码。这一步也要人机验证凭据，所以先把手机号
            # 暂存进状态，等 callback 拿到 ticket/rand 再真正发出。
            phone = str(body.get('phone') or body.get('account') or '').strip()
            if not phone:
                self._json({'ok': False, 'error': '先填手机号'})
                return
            APPLOGIN.begin(phone, '', 3, stage='send')
            self._json({'ok': True, 'state': 'waiting',
                        'page': '/api/ykt/applogin/page'})
            return

        if path == '/api/ykt/applogin/callback':
            # 验证页滑完回传 ticket/rand。按 stage 决定这次验证服务哪一步：
            # send = 反短信登录的发码请求；login = 核码/登录。
            ticket = str(body.get('ticket') or '')
            rand = str(body.get('rand') or '')
            with APPLOGIN.lock:
                acc, pwd, kind = APPLOGIN.account, APPLOGIN.password, APPLOGIN.kind
                stage = APPLOGIN.stage
                if stage != 'send':
                    APPLOGIN.state = 'verifying'
                    APPLOGIN.message = '正在登录…'
            if stage == 'send':
                if not acc:
                    with APPLOGIN.lock:
                        APPLOGIN.state = 'fail'
                        APPLOGIN.message = '发码会话已失效，请重新填手机号'
                    self._json({'ok': False, 'error': APPLOGIN.message})
                    return
                # 先落中间态：send_sms 最长 25 秒，这段时间前端要能显示「正在发送」，
                # 否则用户看到的是「还没验证」的旧文案。
                APPLOGIN.mark_sending()
                try:
                    sent, why = yktapplogin.send_sms(acc, ticket, rand)
                except Exception as e:               # noqa: BLE001
                    sent, why = False, friendly_error(e)
                if sent:
                    APPLOGIN.mark_sent()
                    self._json({'ok': True, 'sent': True,
                                'message': '验证码已发送'})
                else:
                    with APPLOGIN.lock:
                        APPLOGIN.state = 'fail'
                        APPLOGIN.message = why or '验证码发送失败'
                    self._json({'ok': False, 'error': APPLOGIN.message})
                return
            if not acc or not pwd:
                with APPLOGIN.lock:
                    APPLOGIN.state = 'fail'
                    APPLOGIN.message = '登录会话已失效，请重新填写账号密码'
                self._json({'ok': False, 'error': APPLOGIN.message})
                return
            try:
                ok, err, user = yktapplogin.login(acc, pwd, ticket, rand, kind)
            except Exception as e:                   # noqa: BLE001
                ok, err, user = False, friendly_error(e), {}
            if ok:
                # 登录成功 ≠ App 身份可用。先自检，只有自检通过才切身份。
                # 早先无条件切，结果一套对 on-lesson 无效的 App 凭据把整个
                # 雨课堂会话顶成 50000，用户看到的就是「登录成功但什么也没变」。
                try:
                    ver = yktapplogin.verify_identity()
                except Exception as e:               # noqa: BLE001
                    ver = {'ok': False, 'note': friendly_error(e)}
                usable = bool(ver.get('ok'))
                with APPLOGIN.lock:
                    APPLOGIN.state = 'ok' if usable else 'fail'
                    APPLOGIN.user = user
                    APPLOGIN.verify = ver
                    APPLOGIN.message = ('App 身份可用' if usable
                                        else ('App 登录成功，但 ' + (ver.get('note') or '身份不可用')))
                    APPLOGIN.password = ''           # 密码不留在内存里
                # 身份开关跟着自检结果走，并且落盘（重启后仍然生效）
                yktcore.set_identity(usable)
                if usable:
                    yktcore.set_app_uid(user.get('userId') or '')
                    try:
                        YKT.refresh(force=True)
                    except Exception:                # noqa: BLE001
                        pass
                else:
                    # App 身份不可用：把身份钉回 web，别让整个面板一起废掉
                    yktcore.set_app_uid('')
                    try:
                        YKT.refresh(force=True)
                    except Exception:                # noqa: BLE001
                        pass
                self._json({'ok': True, 'name': user.get('name') or '',
                            'school': user.get('school') or '',
                            'identity': 'app' if usable else 'web',
                            'verify': ver})
            else:
                with APPLOGIN.lock:
                    APPLOGIN.state = 'fail'
                    APPLOGIN.message = err or '登录失败'
                self._json({'ok': False, 'error': err or '登录失败'})
            return

        if path == '/api/ykt/applogin/state':
            self._json(APPLOGIN.payload())
            return

        if path == '/api/ykt/applogin/cancel':
            APPLOGIN.clear()
            self._json({'ok': True})
            return

        # 粘贴 web cookie 的入口已删除：App 登录一次就同时覆盖 App 与网页
        # 两套接口，手工粘 cookie 只会带来第二套会互相踢掉的登录态。

        if path == '/api/ykt/logout':
            YKT.ykt.clear_cookie()
            YKT.ykt.store.save_user({})
            with YKT.lock:
                YKT.lessons = []
                YKT.loaded = False
                YKT.status_text = '未登录'
            self._json({'ok': True})
            return

        if path == '/api/ykt/refresh':
            YKT.refresh(force=True)
            self._json(YKT.payload())
            return

        if path == '/api/ykt/checkin':
            lid = str(body.get('lessonId') or '')
            if not lid:
                self._json({'ok': False, 'error': '缺少 lessonId'})
                return
            with YKT.lock:
                known = next((r for r in YKT.lessons if r['lessonId'] == lid), {})
            try:
                cfg = read_settings()
                res = YKT.ykt.signin_full(
                    lid, str(body.get('classroomId') or ''),
                    str(body.get('name') or ''),
                    allow_wait=bool(body.get('wait')),
                    signin_status=known.get('signinStatus') or 0,
                    sign_mode=cfg['ykt']['signMode'],
                    qr_url=str(body.get('qr') or ''))
            except Exception as e:               # noqa: BLE001
                self._json({'ok': False, 'error': str(e)})
                return
            # 需要动态码：不把球踢回给用户，直接从正在播的画面里读一次码再签
            if res.get('status') == 'need_scan' and not str(body.get('qr') or ''):
                grab = qr_from_relay()
                if grab.get('ok'):
                    try:
                        res2 = YKT.ykt.signin_full(
                            lid, str(body.get('classroomId') or ''),
                            str(body.get('name') or ''),
                            allow_wait=False,
                            signin_status=known.get('signinStatus') or 0,
                            sign_mode='scan', qr_url=grab['urls'][0])
                        res2['autoQr'] = True
                        res = res2
                    except Exception as e:       # noqa: BLE001
                        res['detail'] = (res.get('detail') or '') + \
                            f' · 拿到画面里的码但签到失败：{e}'
                else:
                    res['detail'] = ((res.get('detail') or '')
                                     + ' · ' + (grab.get('note') or ''))
            # 只有服务器确认签到（checked/already）才回填"已签到"；
            # joined 是"进班成功、签到还没落"，不能提前把红点关掉。
            if res.get('status') in ('checked', 'already'):
                with YKT.lock:
                    for row in YKT.lessons:
                        if row['lessonId'] == lid:
                            row['checkedAt'] = YKT.ykt.store.load_checked().get(lid, '')
                            row['signinStatus'] = 1
                AUTOSIGN.mark_resolved(
                    'ykt', lid,
                    detail=res.get('detail') or '手动签到成功',
                    count_signed=False,
                )
            self._json(res)
            return

        if path == '/api/ykt/checkin_all':
            ids = body.get('items') or []
            if not isinstance(ids, list) or not ids:
                self._json({'ok': False, 'error': '没有要签到的课堂'})
                return
            if YKT.busy:
                self._json({'ok': False, 'error': '上一轮签到还没跑完'})
                return
            with YKT.lock:
                YKT.busy = True
            results = []
            try:
                for item in ids:
                    lid = str((item or {}).get('lessonId') or '')
                    if not lid:
                        continue
                    try:
                        res = YKT.ykt.signin_full(
                            lid, str(item.get('classroomId') or ''),
                            str(item.get('name') or ''),
                            allow_wait=bool(body.get('wait')),
                            signin_status=item.get('signinStatus') or 0,
                            sign_mode=read_settings()['ykt']['signMode'],
                            qr_url=str((item or {}).get('qr') or ''))
                    except Exception as e:       # noqa: BLE001
                        res = {'ok': False, 'status': 'failed', 'lessonId': lid,
                               'name': item.get('name') or '', 'detail': str(e)}
                    # 批量签到同样自动取码：动态码课堂不会因为"没给码"整批失败
                    if res.get('status') == 'need_scan' and not str((item or {}).get('qr') or ''):
                        grab = qr_from_relay(tries=3)
                        if grab.get('ok'):
                            try:
                                res2 = YKT.ykt.signin_full(
                                    lid, str(item.get('classroomId') or ''),
                                    str(item.get('name') or ''),
                                    allow_wait=False,
                                    signin_status=item.get('signinStatus') or 0,
                                    sign_mode='scan', qr_url=grab['urls'][0])
                                res2['autoQr'] = True
                                res = res2
                            except Exception as e:       # noqa: BLE001
                                res['detail'] = (res.get('detail') or '') + \
                                    f' · 拿到画面里的码但签到失败：{e}'
                        else:
                            res['detail'] = ((res.get('detail') or '')
                                             + ' · ' + (grab.get('note') or ''))
                    results.append(res)
            finally:
                with YKT.lock:
                    YKT.busy = False
            YKT.refresh()
            self._json({'ok': True, 'results': results, 'lessons': YKT.lessons})
            return

        if path == '/api/ykt/mark':
            lid = str(body.get('lessonId') or '')
            if not lid:
                self._json({'ok': False, 'error': '缺少 lessonId'})
                return
            if body.get('on', True):
                YKT.ykt.store.mark_checked(lid)
            else:
                YKT.ykt.store.unmark_checked(lid)
            with YKT.lock:
                for row in YKT.lessons:
                    if row['lessonId'] == lid:
                        row['checkedAt'] = YKT.ykt.store.load_checked().get(lid, '')
            self._json({'ok': True})
            return

        if path == '/api/ykt/answer':
            lid = str(body.get('lessonId') or '')
            pid = str(body.get('problemId') or '')
            try:
                ptype = int(body.get('type') or 1)
            except (TypeError, ValueError):
                ptype = 1
            text = str(body.get('text') or '')
            result = yktapi.normalize_result(ptype, text)
            fallback = int(body.get('fallbackDt') or time.time())
            dt = yktapi.norm_dt(int(body.get('dt') or 0), fallback)
            bearer = ''
            try:
                cred = YKT.ykt.creds_for(lid)
                bearer = cred.bearer
            except Exception:                    # noqa: BLE001
                pass
            out = YKT.ykt.submit_auto(pid, ptype, result, dt, bearer=bearer)
            self._json(out)
            return

        self._json({'error': 'unknown ykt endpoint'}, 404)

    # ------------------------------------------------------------ 静态
    def _static(self, path: str):
        rel = path.lstrip('/') or 'index.html'
        if rel in ('', '/'):
            rel = 'index.html'
        # Android 上 /data/user/0 是指向 /data/data 的符号链接：resolve() 会把
        # 目标规范化成 /data/data/...，而 HERE 还是 /data/user/0/...，用 startswith
        # 比较就会把所有静态资源判成"越界"（整站 404）。两边都比 resolve 后的路径。
        try:
            base = HERE.resolve()
            target = (base / rel).resolve()
            inside = target.is_relative_to(base)
        except (OSError, ValueError):
            inside = False
            target = HERE / rel
        if not inside or not target.is_file():
            self._send(404, b'not found', 'text/plain; charset=utf-8')
            return
        ext = target.suffix.lower()
        ctype = {
            '.html': 'text/html; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.js': 'application/javascript; charset=utf-8',
            '.svg': 'image/svg+xml',
            '.png': 'image/png',
            '.ico': 'image/x-icon',
            '.json': 'application/json; charset=utf-8',
        }.get(ext, 'application/octet-stream')
        self._send(200, target.read_bytes(), ctype)

    # ------------------------------------------------------------ 路由
    def do_GET(self):                                # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == '/api/state':
            # 异步加载：立即返回当前状态（骨架屏 + 前端轮询），
            # 不让页面为了等一次网络探测而白屏
            if not STATE.loaded and not STATE.loading:
                threading.Thread(target=STATE.load, daemon=True).start()
            self._json(state_payload())
            return
        if path.startswith('/api/ykt/'):
            self._ykt_get(path)
            return
        if path.startswith('/api/cx/'):
            self._cx_get(path)
            return
        if path == '/api/settings':
            self._json({'ok': True, 'settings': read_settings()})
            return
        if path == '/api/autosign/state':
            self._json(AUTOSIGN.status_payload())
            return
        if path == '/api/live/state':
            with STATE.lock:
                auto = dict(STATE.auto)
                lives = [{'id': str(c.get('id') or ''), 'name': str(c.get('name') or ''),
                          'statusName': str(c.get('statusName') or '')}
                         for c in live_courses(STATE.courses)]
            self._json({'ok': True, 'auto': auto, 'lives': lives,
                        'ppt': PPT_RELAY.snapshot(), 'settings': read_settings()})
            return
        if path == '/api/live/ppt/ws':
            self._serve_ppt_ws()
            return
        if path.startswith('/api/'):
            self._json({'error': 'unknown endpoint'}, 404)
            return
        self._static(path)

    def _serve_ppt_ws(self):
        """把 PPT 帧按 WebSocket 推给页面。帧率靠 relay 的节流，这里只管转发。"""
        key = self.headers.get('Sec-WebSocket-Key') or ''
        if not key:
            self._json({'ok': False, 'error': 'not a websocket handshake'}, 400)
            return
        self.send_response(101, 'Switching Protocols')
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', wsserve.accept_key(key))
        self.end_headers()
        self.close_connection = True
        sock = self.connection
        try:
            sock.settimeout(8)        # 只防写死锁；读用 select，不吃超时
        except OSError:
            return
        seq = 0
        empty_rounds = 0
        try:
            while True:
                seq, frame, at = PPT_RELAY.wait_frame(seq, timeout=2.0)
                if frame:
                    sock.sendall(wsserve.frame(frame, stamp=at))
                    empty_rounds = 0
                else:
                    empty_rounds += 1
                    # 断流超过 10 秒，或根本没在转码，就让页面自己重连
                    if empty_rounds > 5 or not PPT_RELAY.running:
                        break
                if not wsserve.peek_close(sock):
                    break
        except OSError:
            pass

    def do_HEAD(self):                               # noqa: N802
        self.do_GET()

    def do_POST(self):                               # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith('/api/ykt/'):
            self._ykt_post(path)
            return
        if path.startswith('/api/cx/'):
            self._cx_post(path)
            return
        body = self._body()

        if path == '/api/settings':
            cfg = write_settings(body or {})
            self._json({'ok': True, 'settings': cfg})
            return

        if path == '/api/autosign/trigger':
            platform = str(body.get('platform') or 'all')
            res = AUTOSIGN.trigger_now(platform=platform)
            self._json({**res, 'state': AUTOSIGN.status_payload()})
            return

        if path == '/api/autosign/test_webhook':
            url = str(body.get('url') or '')
            ok, msg = AUTOSIGN.test_webhook(url=url)
            self._json({'ok': ok, 'detail': msg})
            return

        if path == '/api/autosign/clear':
            AUTOSIGN.clear_events()
            self._json({'ok': True, 'state': AUTOSIGN.status_payload()})
            return

        if path == '/api/live/ppt/stop':
            PPT_RELAY.stop()
            with STATE.lock:
                STATE.auto.update(phase='stopped', message='已停止拉取画面')
            self._json({'ok': True, 'ppt': PPT_RELAY.snapshot()})
            return

        if path == '/api/live/ppt/start':
            cfg = read_settings()
            url = str(body.get('url') or '')
            lid = str(body.get('id') or '')
            title = str(body.get('title') or '')
            if not url:
                try:
                    api = STATE.ensure_api()
                    if not lid:
                        lives = live_courses(STATE.courses)
                        if not lives:
                            rows = api.list_lives()
                            with STATE.lock:
                                STATE.courses = rows
                            lives = live_courses(rows)
                        if not lives:
                            self._json({'ok': False, 'error': '现在没有正在直播的课程', 'ppt': PPT_RELAY.snapshot()})
                            return
                        lid = str(lives[0].get('id') or '')
                        title = title or str(lives[0].get('name') or '')
                    streams = get_streams(api, lid)
                    ppt = ppt_stream(streams)
                    if not ppt:
                        self._json({'ok': False, 'error': '这堂课没有 PPT 流', 'ppt': PPT_RELAY.snapshot()})
                        return
                    url = ppt.get('rtmp') or ppt.get('hls') or ''
                except Exception as e:                    # noqa: BLE001
                    self._json({'ok': False, 'error': friendly_error(e), 'ppt': PPT_RELAY.snapshot()})
                    return
            res = PPT_RELAY.start(url, title=title, manual=True,
                                  fps=cfg['ppt']['fps'], quality=cfg['ppt']['quality'])
            if res.get('ok'):
                with STATE.lock:
                    STATE.auto.update(phase='playing', lessonId=lid, title=title,
                                      message='', at=time.time())
            self._json({**res, 'ppt': PPT_RELAY.snapshot()})
            return

        if path == '/api/client_log':
            try:
                append_client_log(body)
                self._json({'ok': True})
            except OSError:
                self._json({'ok': False}, 500)
            return

        if path == '/api/diagnose':
            net = check_intranet()
            with STATE.lock:
                STATE.net = net
            self._json({
                'ok': True, 'net': net,
                'hint': ('校内服务可达，正常使用。' if net.get('api')
                         else '校内服务不可达：请连接校园网 WiFi，或启动 EasyConnect VPN 后重试。'),
            })
            return

        if path == '/api/refresh':
            # 只是重新拉一次列表：复用现有 token，避免频繁 CAS 登录被限流。
            # 真的过期时 _api() 会自动 force 重登一次。
            STATE.load(force=False)
            self._json(state_payload())
            return

        if path == '/api/relogin':
            STATE.load(force=True)
            self._json(state_payload())
            return

        if path == '/api/login':
            u = (body.get('username') or '').strip()
            p = body.get('password') or ''
            if not u or not p:
                self._json({'ok': False, 'error': '学号和密码都要填。'})
                return
            try:
                api = HfutLive(u, p, use_cache=False)
                api.login(force=True)                # 只登这一次
                where = write_creds(u, p)
                with STATE.lock:
                    STATE.api = api
                    STATE.username = u
                    STATE.loaded = False
                    STATE.error = ''
                    STATE.status = '正在获取最新课表…'
                courses = api.list_lives()           # 复用刚登录的会话
                with STATE.lock:
                    STATE.courses = courses
                    STATE.loaded = True
                    STATE.status = f'就绪 · {len(courses)} 场直播'
                self._json({'ok': True, 'saved': where})
            except Exception as e:                   # noqa: BLE001
                with STATE.lock:
                    STATE.error = str(e)
                    STATE.status = '加载失败'
                self._json({'ok': False, 'error': str(e)})
            return

        if path == '/api/streams':
            try:
                api = STATE.ensure_api()
                streams = get_streams(api, body.get('id') or '')
                self._json({'ok': True, 'streams': streams})
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'error': str(e), 'streams': []})
            return

        if path == '/api/play':
            # 已知校内不可达时秒回，不让用户干等超时
            with STATE.lock:
                net = dict(STATE.net)
            if net.get('api') is False:
                self._json({
                    'ok': False,
                    'message': '校内服务不可达：连上校园网 WiFi 或 EasyConnect VPN 后再播放',
                })
                return
            try:
                api = STATE.ensure_api()
                lid = str(body.get('id') or '')
                title = str(body.get('name') or '')
                streams = get_streams(api, lid)
                if not streams:
                    self._json({'ok': False, 'message': '当前教室内无直播信号'})
                    return
                cfg = read_settings()

                # PPT 先嵌进侧栏；勾了 streams.ppt 的话，open_players 会再开一个
                # 独立窗口，三路就和最初版本一致了
                ppt = ppt_stream(streams)
                ppt_ok, ppt_msg = False, ''
                if ppt:
                    if HfutLive.stream_alive(ppt.get('hls') or '') is False:
                        ppt_msg = 'PPT 流尚未开播'
                    else:
                        res = PPT_RELAY.start(ppt.get('rtmp') or ppt.get('hls') or '',
                                              title=title, fps=cfg['ppt']['fps'],
                                              quality=cfg['ppt']['quality'], manual=True)
                        ppt_ok = bool(res.get('ok'))
                        ppt_msg = '' if ppt_ok else str(res.get('error') or '')
                        if ppt_ok:
                            with STATE.lock:
                                STATE.auto.update(phase='playing', lessonId=lid,
                                                  title=title, message='', at=time.time())

                want_players = bool(cfg['streams'].get('teacher')
                                    or cfg['streams'].get('student')
                                    or cfg['streams'].get('ppt'))
                if want_players and not HfutLive.find_player():
                    self._json({
                        'ok': ppt_ok,
                        'message': ('PPT 已嵌进侧栏；' if ppt_ok else '')
                                   + '没有找到播放器（装 mpv / VLC / PotPlayer 任一即可）',
                        'ppt': ppt_ok,
                    })
                    return

                opened, note, not_yet, names = (open_players(api, streams, cfg)
                                               if want_players else (0, '', [], []))
                if names:
                    with STATE.lock:
                        STATE.auto.update(lessonId=lid, title=title,
                                          playerNames=names)
                bits = []
                if ppt_ok:
                    bits.append('PPT 已嵌入侧栏'
                                + ('（另开了独立窗口）' if cfg['streams'].get('ppt') else ''))
                elif ppt_msg:
                    bits.append(f'PPT：{ppt_msg}')
                if opened:
                    bits.append(f'{opened} 路播放器已启动')
                if not_yet:
                    bits.append(f'{len(not_yet)} 路未开播（{"、".join(not_yet)}）')
                if not bits:
                    bits.append('设置里没有勾选任何附加流')
                self._json({'ok': ppt_ok or opened > 0, 'message': '；'.join(bits),
                            'ppt': ppt_ok, 'players': opened})
            except Exception as e:                   # noqa: BLE001
                self._json({'ok': False, 'message': friendly_error(e)})
            return

        if path == '/api/close_players':
            import os as _os
            import signal as _signal
            with STATE.lock:
                pids, STATE.player_pids = STATE.player_pids, []
            killed = 0
            for pid in pids:
                try:
                    _os.kill(pid, _signal.SIGTERM)
                    killed += 1
                except OSError:
                    pass
            self._json({'ok': True, 'killed': killed,
                        'message': f'已关闭 {killed} 个播放器' if killed else '没有正在运行的播放器'})
            return

        self._json({'error': 'unknown endpoint'}, 404)


class LocalServer(ThreadingHTTPServer):
    """本地服务：端口必须独占。

    Windows 的 SO_REUSEADDR 允许两个进程同时监听同一个端口（上一个没退干净时），
    于是新窗口的请求会被旧进程接走，页面拿到旧版的 css/js，
    表现就是"改了样式却没变化"。这里改成独占绑定，端口被占就换下一个。
    """

    allow_reuse_address = False
    allow_reuse_port = False
    daemon_threads = True


def pick_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise SystemExit('没有可用端口')


def main():
    port = int(os.environ.get('HFUT_PORT') or 0) or pick_port(DEFAULT_PORT)
    srv = LocalServer(('127.0.0.1', port), Handler)
    YKT.start_monitor()
    CX_IM.start()
    AUTOSIGN.start()
    threading.Thread(target=autopilot_loop, daemon=True).start()
    srv.daemon_threads = True
    url = f'http://127.0.0.1:{port}/'
    print(json.dumps({'port': port, 'url': url}), flush=True)

    # 预热：后台先把课表拉下来，页面一打开就有内容
    threading.Thread(target=STATE.load, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
