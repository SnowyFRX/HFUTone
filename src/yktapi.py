"""yktapi.py —— 雨课堂的业务动作：登录 / 课表 / 签到 / 课件 / 答题。

对应 ykt_ui 里的 YktApi.cs + YktClient 里那几段业务逻辑。传输和解析在
yktcore 里，这里只写"怎么用这些接口"，并把结果整理成前端能直接吃的字典。

接口清单（全部挂在 https://changjiang.yuketang.cn）:

  GET  /api/v3/user/login/pre-info                       取扫码 token + 二维码图
  POST /api/v3/user/login                                {token} 长轮询, 扫码成功回 Set-Cookie
  GET  /api/v3/classroom/on-lesson                        进行中的课堂
  POST /api/v3/lesson/checkin                             {source, lessonId, joinIfNotIn}
  GET  /v2/api/web/lesson/rollcall?lesson_id=&classroom_id=  考勤签到活动（判"我签上了没"）
  GET  /v/lesson/lesson_info_v2?lesson_id=                课件清单（不要 Bearer）
  GET  /api/v3/lesson/presentation/fetch?presentation_id=  课件页（要 Bearer = set-auth）
  POST /api/v3/lesson/problem/answer                      时限内交卷
  POST /api/v3/lesson/problem/retry                       超时补交
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

import yktcore as core
from yktcore import Resp, log

SERVER = core.SERVER
CDN = core.CDN_BASE

AUTH_FAIL_CODES = (50000, 50005, 50026)


class AuthExpired(RuntimeError):
    """登录态失效（cookie 过期 / 被顶号）。调用方应该清掉 cookie 让用户重扫。"""


# ---------------------------------------------------------------- 结果归一化

def normalize_result(problem_type: int, text: str):
    """用户在一行里填的东西 -> 雨课堂要的结构。

    1 单选 / 4 判断: "A" / "B"（大写）
    2 多选:          ["A","B"]（排序去重，服务端按集合收）
    3 填空:          ["空1","空2"]（单空也必须是数组）
    5 主观:          {content, pics:[{pic,thumb}], videos:[]}
    """
    s = (text or '').strip()
    if problem_type == 2:
        seen = []
        for piece in _split_multi(s):
            p = piece.strip().upper()
            if p and p not in seen:
                seen.append(p)
        seen.sort()
        return seen
    if problem_type == 3:
        arr = [p.strip() for p in _split_multi(s, keep_empty=False)]
        return arr or [s]
    if problem_type == 5:
        return {'content': s, 'pics': [{'pic': '', 'thumb': ''}], 'videos': []}
    return s.upper()


def _split_multi(s: str, keep_empty: bool = False):
    import re
    return re.split(r'[,，|；; \n\r\t]+', s)


def norm_dt(dt: int, fallback: int) -> int:
    """作答时间统一成秒。毫秒当秒传上去服务端会当成几万年后。"""
    dt = int(dt or 0)
    if dt > 10_000_000_000:
        dt //= 1000
    if dt <= 0:
        dt = int(fallback or 0)
    if dt > 10_000_000_000:
        dt //= 1000
    return dt


def is_already_answered(code: int | None, raw: str) -> bool:
    """雨课堂用 50028；各端文案不同，所以码和文案都看。"""
    if code == 50028:
        return True
    if not raw:
        return False
    up = raw.upper()
    return ('ALREADY_ANSWERED' in up or 'ALREADY' in up
            or '已作答' in raw or '已提交' in raw)


def can_retry_channel(code: int, err: str) -> bool:
    """时限内通道被拒之后还值不值得再走补交通道。"""
    if code in (50026, 50005):
        return True
    s = (err or '').upper()
    return any(k in s for k in ('50026', 'CLOSED', 'FINISHED', 'HTTP 500',
                                '已收题', '超时', '时限'))


# ------------------------------------------------------------------- 会话

class Cred:
    """一节课的凭据：签到成功才拿得到，课件和答题都要它。"""

    __slots__ = ('lesson_id', 'bearer', 'lesson_token', 'active_pres', 'at')

    def __init__(self, lesson_id: str = '', bearer: str = '', lesson_token: str = ''):
        self.lesson_id = lesson_id
        self.bearer = bearer
        self.lesson_token = lesson_token
        self.active_pres = ''
        self.at = datetime.now()

    @property
    def ready(self) -> bool:
        return bool(self.bearer or self.lesson_token)

    def as_dict(self) -> dict:
        return {'lessonId': self.lesson_id,
                'bearer': bool(self.bearer),
                'lessonToken': bool(self.lesson_token),
                'at': self.at.strftime('%H:%M:%S')}


class Ykt:
    """一个雨课堂账号会话。cookie 存 Store，签到凭据只活在内存里。"""

    def __init__(self, store: core.Store | None = None):
        self.store = store or core.Store()
        self._creds: dict[str, Cred] = {}
        self._lock = threading.RLock()
        self.user = self.store.get_user()          # 上次登录的人，先拿出来显示
        # 把 uid 交给传输层，之后每个雨课堂请求都会带 x-uid。
        # 真 App 每个请求都带它，缺了容易被判成"非官方客户端"。
        try:
            core.set_app_uid(user_id_from_cookie(self.cookie))
        except Exception:                          # noqa: BLE001
            core.set_app_uid('')

    # -- cookie -----------------------------------------------------------
    @property
    def cookie(self) -> str:
        """当前身份对应的凭据：App 身份用 app_cookie，web 身份用 cookie。

        两套永远不互相覆盖。App 身份下如果没有 app_cookie（被清掉/老配置），
        退回 web cookie 并告警——总比拿个空 cookie 把所有接口都打成 50000 好。
        """
        if core.APP_IDENTITY:
            ck = self.store.get_app_cookie()
            if ck:
                return ck
            core.log('[身份] App 身份下缺 app_cookie，临时退回 web 凭据')
            return self.store.get_cookie()
        return self.store.get_cookie()

    def set_cookie(self, cookie: str) -> bool:
        return self.store.save_cookie(cookie)

    def set_app_cookie(self, cookie: str) -> bool:
        return self.store.save_app_cookie(cookie)

    def clear_cookie(self) -> bool:
        """退出登录：清掉当前身份那套。App 身份退出连 app_cookie 一起清。"""
        if core.APP_IDENTITY:
            self.store.clear_app_cookie()
            core.set_identity(False, self.store)
        return self.store.clear_cookie()

    # -- 用户信息 ---------------------------------------------------------
    def profile(self) -> dict:
        """用 cookie 换"我是谁"：姓名 / 学校 / user_id。

        走 App 端那条 /v/course_meta/user_info（一次就够，比 /api/v3/user/profile 稳），
        它要 App 的 UA 和一套自定义头，缺了会回 403。
        """
        cookie = self.cookie
        if not cookie:
            return {}
        hdr = {
            'Accept': 'application/json',
            'User-Agent': 'Android',
            'brand': 'google Pixel 9 Pro',
            'xtua': 'client=app&tag=1.3.3&platform=Android',
            'systemversion': '16',
            'xtbz': 'ykt',
            'x-client': 'app',
        }
        r = core.request('GET', SERVER + '/v/course_meta/user_info', cookie=cookie,
                         referer=SERVER + '/', timeout=15, extra_headers=hdr,
                         tag='user-info')
        if r.status != 200 or r.looks_like_html:
            log(f'[用户] 拿不到用户信息 HTTP {r.status} {core.head(r.body, 120)}')
            return {}
        d = core.jdata(r.body)
        prof = None
        if isinstance(d, dict):
            prof = d.get('user_profile')
            if not isinstance(prof, dict) and ('name' in d or 'user_id' in d):
                prof = d
        if not isinstance(prof, dict):
            log(f'[用户] 响应里没有 user_profile {core.head(r.body, 160)}')
            return {}
        user = {
            'userId': str(core.js(prof, 'user_id', 'userId') or ''),
            'name': str(core.js(prof, 'name', 'nick', 'nickname') or ''),
            'school': str(core.js(prof, 'school', 'school_name') or ''),
            'avatar': str(core.js(prof, 'avatar', 'avatar_url') or ''),
        }
        if user['name']:
            with self._lock:
                self.user = user
            self.store.save_user(user)
            # 登录态刚建立，cookie 里这时才有 uid；同步给传输层带 x-uid
            try:
                uid = user['userId'] or str(user_id_from_cookie(self.cookie) or '')
                core.set_app_uid(uid)
            except Exception:                      # noqa: BLE001
                pass
            log(f'[用户] {user["name"]}'
                + (f' · {user["school"]}' if user['school'] else '')
                + (f' (id={user["userId"]})' if user['userId'] else ''))
        return user

    def ensure_user(self) -> dict:
        """没缓存就去拉一次；拉不到也不影响签到。"""
        if self.user.get('name'):
            return self.user
        return self.profile()

    # -- 课程 -------------------------------------------------------------
    def on_lesson(self) -> list[dict]:
        """进行中的课堂。返回 [{lessonId, name, courseName, classroomName, classroomId}]。"""
        c = self.cookie
        if not c:
            raise RuntimeError('未登录')
        r = core.request('GET', SERVER + '/api/v3/classroom/on-lesson', cookie=c,
                         referer=SERVER + '/lesson/fullscreen/v3/', timeout=20, tag='on-lesson')
        if r.looks_like_html:
            raise RuntimeError('网关返回网页（TLS 指纹被拦），检查 curl 是否内置版')
        code = core.jcode(r.body)
        msg = str(core.jstr(r.body, 'msg') or '')
        if code in AUTH_FAIL_CODES or 'UNAUTHENTICATED' in msg.upper():
            raise AuthExpired('登录已过期，请重新扫码')
        if r.status != 200 or (code is not None and code != 0):
            raise RuntimeError(f'获取课堂列表失败 HTTP {r.status} code={code} '
                               f'{msg or core.head(r.body, 120)}')
        d = core.jdata(r.body)
        rows = []
        if isinstance(d, dict):
            arr = d.get('onLessonClassrooms') or d.get('lessons') or []
        elif isinstance(d, list):
            arr = d
        else:
            arr = []
        hist = self.store.load_checked()
        for item in arr:
            if not isinstance(item, dict):
                continue
            lid = core.js(item, 'lessonId', 'lesson_id', 'id')
            if not lid:
                continue
            course = core.js(item, 'courseName')
            room = core.js(item, 'classroomName')
            name = core.js(item, 'name', 'lesson_name') or \
                (f'{course} · {room}' if room else course)
            # signinStatus 在长江雨课堂的 on-lesson 里**不存在**（实测
            # 2026-11-24，课堂进行中和已结束都一样，item 只有 classroomId/
            # classroomName/courseId/courseName/lessonId/role/openTeacher/
            # teacherAvatar/clone）。这里把常见变体都列出来，是为了将来
            # 服务端真加了字段能自动生效；现在恒为 False，签到判据见
            # signin_full 里的 rollcall 活动数分支。
            signed = _truthy(core.js(item, 'signinStatus', 'signin_status',
                                      'signInStatus', 'attendanceStatus',
                                      'checked', 'isSignin'))
            rows.append({
                'lessonId': str(lid),
                'name': str(name),
                'courseName': str(course),
                'classroomName': str(room),
                'classroomId': str(core.js(item, 'classroomId') or ''),
                'teacher': str(core.js(item, 'teacherName', 'teacher') or ''),
                'checkedAt': hist.get(str(lid), '') or ('服务器' if signed else ''),
                'signinStatus': 1 if signed else 0,
            })
        log(f'[课程] 进行中课堂 {len(rows)} 节')
        return rows

    # -- 签到 -------------------------------------------------------------
    def checkin(self, lesson_id: str, source: int = 21,
                extra_headers: dict | None = None) -> tuple[Resp, Cred]:
        """进班。默认 source=21，带课堂 Referer（web 身份的正常形态）。"""
        data = {'source': source, 'lessonId': lesson_id, 'joinIfNotIn': True}
        r = core.request('POST', SERVER + '/api/v3/lesson/checkin', cookie=self.cookie,
                         referer=SERVER + '/lesson/fullscreen/v3/' + lesson_id,
                         data=data, with_headers=True, timeout=25,
                         extra_headers=extra_headers, tag=f'checkin src={source}')
        cred = Cred(lesson_id,
                    bearer=r.header('set-auth'),
                    lesson_token=str(core.jstr(r.body, 'lessonToken') or ''))
        if cred.ready:
            with self._lock:
                self._creds[lesson_id] = cred
        return r, cred

    # ---- 工小湖同款: 伪装 Android App 客户端签到 ----
    # 关键: 服务端按 x-client/xtua 判定客户端类型。官方 App 的扫码是 UI 层行为,
    # API 层对 App 客户端不强制动态码校验(实测工小湖就是这么过 50070 的)。
    # 三件套缺一不可: 全套 App 头 / x-uid / 不能带桌面 Referer。
    # App 头现在由 yktcore 统一带（每个雨课堂请求都带），这里留个引用方便阅读
    APP_HEADERS = core.APP_HEADERS

    def app_checkin(self, lesson_id: str, user_id=None) -> tuple[Resp, Cred]:
        """伪装 App 客户端进班（设置里 signMode=app 或 both 走这条）。

        只在这一次请求上换 App 头 + 空 Referer，不动整个会话：
        实测长江雨课堂的 web cookie 在纯 App 身份下会 UNAUTHENTICATED，
        所以不能全局伪装，只能"借"一次。能不能过 50070 要看真课。
        """
        uid = str(user_id or '').strip()
        if not uid:
            try:
                uid = str(user_id_from_cookie(self.cookie) or '')
            except Exception:
                uid = ''
        hdr = {}
        if uid and uid != '0':
            hdr['x-uid'] = uid

        data = {'source': 21, 'lessonId': str(lesson_id), 'joinIfNotIn': True}
        # referer 必须留空：App 不会带桌面 Referer，带了就自相矛盾
        r = core.request('POST', SERVER + '/api/v3/lesson/checkin',
                         cookie=self.cookie, referer='',
                         data=data, with_headers=True, timeout=25,
                         extra_headers=hdr, tag='checkin-app')
        cred = Cred(lesson_id,
                    bearer=r.header('set-auth'),
                    lesson_token=str(core.jstr(r.body, 'lessonToken') or ''))
        if cred.ready:
            with self._lock:
                self._creds[lesson_id] = cred
        log(f'[App签到] lesson={lesson_id} uid={uid or "?"} code={core.jcode(r.body)} '
            f'msg={core.jstr(r.body, "msg") or ""}')
        return r, cred

    def rollcall(self, lesson_id: str, classroom_id: str) -> tuple[int, str, bool]:
        """考勤签到活动。返回 (活动数, 明细, 是否确认我有记录)。

        data 实际长这样：{"2688": 56088391}——键是签到活动 id，值是班号/时间戳。
        以前这里写的是"遍历前 5 个键，任意值 > 0 就算我有记录"，那是脑补：
        56088391 是 id 不是"我签了到"，老师一发起点全班都会被判成已签。
        所以 mine 一律返回 False，"我签没签"只认 on-lesson 的 signinStatus。
        """
        if not lesson_id or not classroom_id:
            return -1, '缺少 lessonId / classroomId', False
        q = (f'{SERVER}/v2/api/web/lesson/rollcall?lesson_id='
             f'{core.urllib.parse.quote(lesson_id)}&classroom_id='
             f'{core.urllib.parse.quote(classroom_id)}')
        r = core.request('GET', q, cookie=self.cookie,
                         referer=SERVER + '/lesson/fullscreen/v3/' + lesson_id,
                         timeout=15, tag='rollcall')
        if r.status != 200:
            return -1, f'HTTP {r.status}', False
        d = core.jdata(r.body)
        if not isinstance(d, dict):
            return 0, '(没有签到活动)', False
        detail = []
        for k, v in list(d.items())[:5]:
            detail.append(f'{k}=>{v}')
        log(f'[签到] 签到活动 {len(d)} 个  {"  ".join(detail)}')
        return len(d), '  '.join(detail), False   # mine 见 docstring，不从这份数据推

    def _signed_on_server(self, lesson_id: str) -> bool:
        """问一次 on-lesson：这堂课服务器记我签到了吗。

        on-lesson 里每堂课都带 signinStatus，那才是服务器眼中的"我签没签"。
        rollcall 的 data 是活动清单（键=活动 id），推断不出"我"，别从那里猜。
        """
        try:
            for row in self.on_lesson():
                if row.get('lessonId') == str(lesson_id):
                    return bool(row.get('signinStatus'))
        except (RuntimeError, OSError):
            pass
        return False

    def scan(self, qr_url: str) -> tuple[Resp, Cred]:
        """把扫码得到的原始 URL 交给服务器，由它解析出 lessonId 并进班。

        这是 course_helper 那条路（RCCourseApi.scan）：客户端不必懂二维码里
        是什么，POST /api/v3/app/scan {url} 之后服务器回 data.value = lessonId，
        拿它去 checkin 就行。
        """
        url = str(qr_url or '').strip()
        if not url:
            return Resp(0, '{"code":-1,"msg":"empty url"}', ''), Cred()
        r = core.request('POST', SERVER + '/api/v3/app/scan', cookie=self.cookie,
                         referer='', data={'url': url}, with_headers=True,
                         timeout=20, extra_headers=self.APP_HEADERS, tag='scan')
        code = core.jcode(r.body)
        msg = str(core.jstr(r.body, 'msg') or '')
        log(f'[扫码] code={code} msg={msg} url={url[:80]}')
        if code != 0:
            return r, Cred()
        lesson_id = str(core.jstr(r.body, 'value') or '')
        if not lesson_id:
            return r, Cred()
        # 服务器认了这个码，接着进班拿 Bearer / lessonToken
        return self.checkin(lesson_id, source=21)

    def signin_full(self, lesson_id: str, classroom_id: str = '',
                    name: str = '', allow_wait: bool = False,
                    signin_status: int | bool = 0,
                    sign_mode: str = 'both', qr_url: str = '') -> dict:
        """完整签到判定：进班 -> 签到结论 -> 动态码等窗口 -> 结论。

        返回 {ok, status, detail, code, msg, cred}，status 取
        checked / already / joined / need_scan / ended / failed。

        只有 checked / already 才算签到完成、才写本地"已签到"标记。
        checkin 的 code 0 只说明"进班成功"——老师还没发起签到时它照样返回 0，
        那时候报 joined（已进班 · 待签到），绝不能提前把红点关掉：真签到一开，
        用户要能立刻看到还没签。
        """
        result = {'ok': False, 'status': 'failed', 'detail': '', 'code': None,
                  'msg': '', 'lessonId': lesson_id, 'name': name,
                  'channel': ''}
        signed = _truthy(signin_status)
        mode = (sign_mode or 'both').strip().lower()
        if mode not in ('app', 'scan', 'both'):
            mode = 'both'
        qr = str(qr_url or '').strip()

        r, cred = None, Cred()
        code, msg = None, ''

        # 只走 scan 模式却没给码：直接说清楚，别偷偷退回 App 头
        if mode == 'scan' and not qr:
            result.update(status='need_scan', channel='scan',
                          detail='当前设置只走扫码通道，需要二维码')
            return result

        # 有二维码 URL 且模式允许：先让服务器解析这个码（helper 那条路）
        if qr and mode in ('scan', 'both'):
            r, cred = self.scan(qr)
            code = core.jcode(r.body)
            msg = str(core.jstr(r.body, 'msg') or '')
            if code == 0:
                result['channel'] = 'scan'
            elif mode == 'both':
                log(f'[签到] scan 通道失败(code={code})，回退 App 头通道')

        # scan 没成（或没给码、或只走 app）：用伪装 App 头那条
        if r is None or (mode in ('app', 'both') and code != 0):
            # App 头通道（工小湖同款，能过动态码 50070）
            # user_id 从 cookie 的 JWT 里解，解不出就留空（app_checkin 内部会再兜一次）
            r, cred = self.app_checkin(lesson_id, user_id_from_cookie(self.cookie))
            code = core.jcode(r.body)
            msg = str(core.jstr(r.body, 'msg') or '')
            result['channel'] = 'app'
            # 50070 时再试普通通道做对照（有课可测时看哪个通）
            if code == 50070:
                log('[签到] App 通道仍 50070，回退普通通道对照')
                r, cred = self.checkin(lesson_id, source=21)
                code = core.jcode(r.body)
                msg = str(core.jstr(r.body, 'msg') or '')
                result['channel'] = 'plain'

        # 动态二维码拒绝：换个 source 通道再试一次，多试无副作用
        if code == 50070:
            time.sleep(0.4)
            r2, cred2 = self.checkin(lesson_id, source=9)
            if core.jcode(r2.body) != 50070:
                r, cred, code, msg = r2, cred2, core.jcode(r2.body), str(core.jstr(r2.body, 'msg') or '')

        n_act, detail, _ = self.rollcall(lesson_id, classroom_id)
        result.update(code=code, msg=msg, detail=f'活动{n_act}条 {detail}'.strip())

        def _ok(status: str, why: str):
            result.update(ok=True, status=status, detail=why)
            if cred.ready:
                result['cred'] = cred.as_dict()
            self.store.mark_checked(lesson_id)
            return result

        data = core.jdata(r.body) or {}

        def _already(msg: str) -> bool:
            # 雨课堂三种说法都出现过：ALREADY_*、"已签到, 请勿重复签到"、"您已签到"
            return ('ALREADY' in msg.upper() or '已签到' in msg
                    or '请勿重复' in msg)

        if code == 0:
            if '签到成功' in msg or _truthy(core.js(data, 'status')):
                return _ok('checked', f'签到成功（{msg or "code 0"}）')
            if _already(msg):
                return _ok('already', f'服务器提示已签到（{msg}）')
            if signed or self._signed_on_server(lesson_id):
                return _ok('already', '服务器已记录这堂课的签到')
            # on-lesson 的课堂对象**没有 signinStatus 字段**（实测
            # 2026-11-24：item 只有 classroomId/classroomName/courseId/
            # courseName/lessonId/role/openTeacher/teacherAvatar/clone），
            # 所以上面的 _signed_on_server 永远拿不到东西。真正能问的是
            # rollcall：老师发起签到后会有一条活动记录；此时 App 身份
            # 进班返回 code 0 就等于签到完成（这正是工小湖免扫那条路）。
            # 反过来活动数为 0 说明老师还没发起签到，那才叫"已进班待签到"。
            if n_act > 0:
                return _ok('checked', f'已进班且老师已发起签到（活动 {n_act} 条）')
            result.update(ok=True, status='joined',
                          detail='进班成功，老师还没发起签到')
            return result
        if _already(msg):
            return _ok('already', f'服务器提示已签到（{msg}）')

        is_qr = code == 50070 or 'DYNAMIC_QR' in msg.upper()
        if is_qr:
            if signed or self._signed_on_server(lesson_id):
                return _ok('checked', '动态码课堂：进班被拒(50070)，但服务器记录我已签到')
            if allow_wait:
                # 动态码只在窗口内有效，老师退出窗口后接口立刻放行
                for k in range(12):
                    time.sleep(5)
                    r3, cred3 = self.checkin(lesson_id, source=9)
                    c3 = core.jcode(r3.body)
                    if c3 != 50070:
                        result.update(code=c3, msg=str(core.jstr(r3.body, 'msg') or ''))
                        if c3 == 0:
                            return _ok('checked', f'动态码窗口关闭后第{k + 1}次重试成功')
                        break
            result.update(status='need_scan', detail='动态二维码签到：需手机扫码')
            return result

        if 'LESSON_END' in msg.upper():
            result.update(status='ended', detail='签到已结束（LESSON_END）')
            return result

        result.update(detail=f'签到失败 code={code} {msg}'.strip())
        return result

    # -- 课件 -------------------------------------------------------------
    def creds_for(self, lesson_id: str) -> Cred:
        with self._lock:
            cred = self._creds.get(lesson_id)
        if cred and cred.ready:
            return cred
        log('[课件] 没有凭据，静默签到一次')
        _, cred = self.checkin(lesson_id, source=21)
        return cred

    def lesson_info(self, lesson_id: str) -> list[dict]:
        """课件清单。

        **不要用 /v/lesson/lesson_info_v2**：那个接口对任何请求都返回
        500（body 是服务端自己的异常 NoneType.set_cookie），网页版
        2026-11 起也早就不调它了——实测用 Edge 抓网页版真实请求，
        整条链路里根本没有 lesson_info_v2。

        网页版的做法是：WebSocket 连 /wsapp/ 发 hello，回包里带
        timeline，里面每条 slide 都有 {"type":"slide","pres":"<19位ID>",
        "si":当前页,...}，那个 pres 就是 presentation_id；拿到之后
        再调 /api/v3/lesson/presentation/fetch?presentation_id=<pres>
        取全部页。这里照抄这条路。
        """
        import yktws

        cred = self.creds_for(lesson_id)
        if not cred.lesson_token:
            raise RuntimeError('没有 lessonToken（签到没成功，或这堂课不允许）')

        uid = 0
        try:
            uid = int(str((self.store.get_user() or {}).get('userId') or 0) or 0)
        except (TypeError, ValueError):
            uid = 0

        headers = {
            'Accept': '*/*',
            'User-Agent': core.UA,
            'Origin': SERVER,
            'Referer': SERVER + '/lesson/fullscreen/v3/' + lesson_id,
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }
        if self.cookie:
            headers['Cookie'] = self.cookie

        pres_ids: list[str] = []
        titles: dict[str, str] = {}
        ws = None
        try:
            ws = yktws.WS(_ws_endpoint(), headers, timeout=12)
            ws.connect()
            ws.send_text(json.dumps({
                'op': 'hello', 'userid': str(uid or ''), 'avatar': '',
                'role': 'student', 'auth': cred.lesson_token or '',
                'lessonid': lesson_id or '', 'ver': 1,
            }, ensure_ascii=False, separators=(',', ':')))

            deadline = time.time() + 12
            while time.time() < deadline:
                msg = ws.recv_json(timeout=max(1.0, deadline - time.time()))
                if msg is None:
                    if ws._closed:
                        break
                    continue
                # hello / fetchtimeline 都可能带 timeline
                timeline = msg.get('timeline')
                if isinstance(timeline, list):
                    for ev in timeline:
                        if not isinstance(ev, dict):
                            continue
                        if ev.get('type') == 'event' and ev.get('code') == 'SHOW_PRESENTATION':
                            rep = ev.get('replace')
                            if isinstance(rep, list) and rep:
                                last = ''
                                if pres_ids:
                                    titles[pres_ids[-1]] = str(rep[0])
                        elif ev.get('type') == 'slide':
                            p = str(ev.get('pres') or '')
                            if p and p not in pres_ids:
                                pres_ids.append(p)
                # slidenav 也带当前 pres
                slide = msg.get('slide')
                if isinstance(slide, dict):
                    p = str(slide.get('pres') or '')
                    if p and p not in pres_ids:
                        pres_ids.append(p)
                if msg.get('op') == 'hello' and isinstance(msg.get('timeline'), list):
                    break
        except Exception as exc:                          # noqa: BLE001
            log(f'[课件] WS 取 presentation_id 失败: {exc}')
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:                          # noqa: BLE001
                    pass

        if not pres_ids:
            raise RuntimeError('这堂课还没有推送过课件（老师没放映 PPT）')

        out = []
        for p in pres_ids:
            try:
                title, slides = self.presentation(p, cred.bearer)
            except Exception as exc:                       # noqa: BLE001
                log(f'[课件] pres={p} 取页失败: {exc}')
                continue
            out.append({'id': p, 'title': title or titles.get(p) or '课堂课件',
                        'slideCount': len(slides), 'slides': slides})
        if not out:
            raise RuntimeError('课件页拉取失败')
        return out

    def presentation(self, pres_id: str, bearer: str) -> tuple[str, list[dict]]:
        """拉课件页。cover 可能是相对路径，补成 CDN 绝对地址。"""
        if not pres_id:
            raise RuntimeError('没有 presentation_id')
        hdr = {'Accept': 'application/json'}
        if bearer:
            hdr['Authorization'] = 'Bearer ' + bearer
        r = core.request('GET',
                         f'{SERVER}/api/v3/lesson/presentation/fetch?presentation_id='
                         f'{core.urllib.parse.quote(pres_id)}',
                         cookie=self.cookie, referer=SERVER + '/lesson/fullscreen/v3/',
                         timeout=25, extra_headers=hdr, tag='presentation')
        if r.status != 200:
            raise RuntimeError(f'HTTP {r.status}')
        d = core.jdata(r.body)
        if not isinstance(d, dict):
            raise RuntimeError('没有 data 字段')
        title = str(core.js(d, 'title', 'Title') or '课堂课件')
        arr = d.get('slides')
        if not isinstance(arr, list):
            raise RuntimeError('没有 slides')
        slides = []
        for idx, item in enumerate(arr):
            if not isinstance(item, dict):
                continue
            cover = str(core.js(item, 'cover', 'src', 'image') or '')
            url = cover if cover.startswith('http') else (CDN + cover.lstrip('/') if cover else '')
            slides.append({'index': idx, 'cover': cover, 'url': url,
                           'title': str(core.js(item, 'title') or '')})
        log(f'[课件] pres={pres_id}  title={title}  页数={len(slides)}')
        return title, slides

    def download(self, url: str, referer: str = '') -> bytes | None:
        """下载一页图片（七牛 CDN，不需要 cookie）。回放模式退化成空。"""
        if not url:
            return None
        if core.replay_dir():
            return None
        return core.fetch_bytes(url, referer=referer, timeout=25)

    # -- 随堂题 -----------------------------------------------------------
    def questions(self, lesson_id: str, wait: float = 12.0) -> tuple[list[dict], str]:
        """拉这节课已经下发的随堂题。需要签到拿到的 lessonToken。"""
        if core.replay_dir():
            return [], '离线回放模式：随堂题需要真实网关'
        cred = self.creds_for(lesson_id)
        if not cred.lesson_token:
            return [], '没有 lessonToken（签到没成功，或这堂课不允许）'
        # userid 必须带对，网关按它认人。App 身份的 sessionid 是
        # "sessionid=xxx; sid=xxx"，不是 JWT，user_id_from_cookie 解不出来
        # （返回 0）——于是 hello 里 userid 为空，网关不认，连接一直等到超时。
        # 优先用登录时存下来的 userId，再退回 cookie 解析。
        uid = 0
        try:
            uid = int(str((self.store.get_user() or {}).get('userId') or 0) or 0)
        except (TypeError, ValueError):
            uid = 0
        if not uid:
            uid = user_id_from_cookie(self.cookie)
        return fetch_questions(lesson_id, cred.lesson_token, uid, self.cookie, wait)

    # -- 答题 -------------------------------------------------------------
    def submit(self, problem_id: str, problem_type: int, result, dt: int,
               *, retry: bool = False, bearer: str = '') -> tuple[str | None, int]:
        """返回 (错误文本|None, code)。code=-1 表示没走到服务端。"""
        if not problem_id:
            return '题目 ID 不能为空', -1
        if retry:
            payload = {'problems': [{'problemId': problem_id, 'dt': int(dt),
                                     'problemType': int(problem_type),
                                     'result': result, 'retry_times': None}]}
            path = '/api/v3/lesson/problem/retry'
        else:
            payload = {'problemId': problem_id, 'dt': int(dt),
                       'problemType': int(problem_type), 'result': result}
            path = '/api/v3/lesson/problem/answer'
        hdr = {'Accept': 'application/json'}
        if bearer:
            hdr['Authorization'] = 'Bearer ' + bearer
        r = core.request('POST', SERVER + path, cookie=self.cookie,
                         referer=SERVER + '/', data=payload, timeout=25,
                         extra_headers=hdr, tag='answer' + ('-retry' if retry else ''))
        if r.status != 200:
            return f'HTTP {r.status}' + ('（网关拦截页）' if r.looks_like_html else ''), -1
        code = core.jcode(r.body)
        msg = str(core.jstr(r.body, 'msg') or '')
        if code == 0:
            return None, 0
        if is_already_answered(code, r.body):
            return None, 50028
        return f'code={code} {msg or core.head(r.body, 200)}'.strip(), (code if code is not None else -1)

    def submit_auto(self, problem_id: str, problem_type: int, result, dt: int,
                    bearer: str = '') -> dict:
        """先走时限内通道；被拒（已收题/超时/500）就自动改走补交通道。"""
        err, code = self.submit(problem_id, problem_type, result, dt, bearer=bearer)
        if err is None:
            return {'ok': True, 'code': code, 'retried': False,
                    'note': '已作答过' if code == 50028 else '提交成功'}
        if not can_retry_channel(code, err):
            return {'ok': False, 'code': code, 'retried': False, 'error': err}
        log(f'[答题] 时限内通道被拒(code={code})，改走补交通道 prob={problem_id}')
        err2, code2 = self.submit(problem_id, problem_type, result, dt,
                                  retry=True, bearer=bearer)
        if err2 is None:
            return {'ok': True, 'code': code2, 'retried': True, 'note': '已走补交通道'}
        return {'ok': False, 'code': code2, 'retried': True, 'error': err2}


def _cookie_from_body(body: str) -> str:
    """少数情况下服务端把 sessionid 放 data 里而不是 Set-Cookie。"""
    d = core.jdata(body)
    if not isinstance(d, dict):
        return ''
    parts = []
    for k in ('sessionid', 'csrftoken'):
        v = core.js(d, k)
        if v:
            parts.append(f'{k}={v}')
    return '; '.join(parts)


# ------------------------------------------------------------ 随堂题（WS）

TYPE_NAME = {1: '单选', 2: '多选', 3: '填空', 4: '判断', 5: '主观'}


def _num(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0


def _truthy(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ('true', '1', 'yes')


def _norm_id(s) -> str:
    if not s:
        return ''
    s = str(s).strip()
    if s.lower().startswith('prob-'):
        s = s[5:]
    return s


def _probelm_id_from(t: dict) -> str:
    p = t.get('problem')
    if isinstance(p, dict):
        for k in ('id', 'prob', 'problemId', 'problemid'):
            if p.get(k):
                return str(p[k])
    return ''


def parse_timeline(items: list) -> list[dict]:
    """timeline / unlockedproblem 里的条目 -> 题目列表。

    解析规则照抄客户端：题目类事件带 prob/problemid/problemId/problem；
    收题事件（problemfinished 等）标记 closed；extendtime 累加延时。
    """
    extend: dict[str, list[int]] = {}
    closed: set[str] = set()
    out: list[dict] = []
    seen: set[str] = set()

    for t in items:
        if not isinstance(t, dict):
            continue
        op = str(t.get('op') or t.get('type') or '')
        pid = _norm_id(t.get('prob') or t.get('problemid') or t.get('problemId') or _probelm_id_from(t))
        if op == 'extendtime' and pid:
            box = extend.setdefault(pid, [0, 0])
            ex, lim = _num(t.get('extend')), _num(t.get('limit'))
            if ex > 0:
                box[0] += ex
            if lim > 0:
                box[1] = lim
            continue
        if op in ('problemfinished', 'closeproblem', 'problemoff', 'finishproblem',
                  'stopproblem', 'problemstop', 'close_problem') and pid:
            closed.add(pid)

    close_ops = ('problemfinished', 'closeproblem', 'problemoff', 'finishproblem',
                 'stopproblem', 'problemstop', 'close_problem')

    for t in items:
        if not isinstance(t, dict):
            continue
        typ = str(t.get('type') or t.get('op') or '')
        # extendtime / 收题事件本身不是题目：它们只是修改别的题目的属性，
        # 放进去会凭空多出一条"类型1 dt=0"的假题，还会把真题的 id 占掉。
        if typ == 'extendtime' or typ in close_ops:
            continue
        is_problem = (typ in ('problem', 'unlockproblem')
                      or t.get('prob') is not None or t.get('problemid') is not None
                      or t.get('problemId') is not None)
        if not is_problem:
            continue
        pid = _norm_id(t.get('prob') or t.get('problemid') or t.get('problemId')
                       or t.get('id') or _probelm_id_from(t))
        if not pid or pid in seen:
            continue
        seen.add(pid)

        inner = t.get('problem') if isinstance(t.get('problem'), dict) else None
        ptype = _num(t.get('problemType') or t.get('problemtype') or t.get('problem_type')
                     or (inner or {}).get('problemType') or (inner or {}).get('type')) or 1
        dt = _num(t.get('dt'))
        if dt > 10_000_000_000:
            dt //= 1000
        si = _num(t.get('si'))
        ex = extend.get(pid, [0, 0])

        lim = _num(t.get('limit')) or _num(t.get('timelimit'))
        if not lim and inner:
            lim = _num(inner.get('limit') or inner.get('timelimit'))
        if not lim:
            lim = ex[1]

        is_closed = (pid in closed or _truthy(t.get('isClosed'))
                     or _truthy((inner or {}).get('isClosed')))
        answered = _truthy(t.get('isAnswered') or t.get('is_answered') or t.get('has_answered'))
        if not answered and inner:
            answered = _truthy(inner.get('isAnswered') or inner.get('is_answered')
                               or inner.get('has_answered'))
        if not answered:
            ua = t.get('user_answer') or t.get('userAnswer')
            if ua is None and inner:
                ua = inner.get('user_answer')
            s = '' if ua is None else str(ua).strip()
            answered = bool(s) and s not in ('null', '[]')

        out.append({
            'problemId': pid,
            'problemType': ptype,
            'typeName': TYPE_NAME.get(ptype, f'类型{ptype}'),
            'dt': dt,
            'index': si - 1 if si > 0 else -1,
            'closed': is_closed,
            'answered': answered,
            'unlimited': lim < 0,
            'extendSec': ex[0],
            'limitSec': 0 if lim < 0 else lim,
        })
    out.sort(key=lambda q: (q['index'] < 0, q['index'], q['dt']))
    return out


def user_id_from_cookie(cookie: str) -> int:
    """从 sessionid 里的 JWT 解 user_id；解不出返回 0（hello 的 userid 不严格）。"""
    import base64
    for part in (cookie or '').split(';'):
        k, _, v = part.strip().partition('=')
        if k.strip() != 'sessionid' or v.count('.') != 2:
            continue
        body = v.split('.')[1]
        body += '=' * (-len(body) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(body).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            continue
        for key in ('user_id', 'userId', 'uid', 'id'):
            n = _num(data.get(key))
            if n > 0:
                return n
    return 0


def _ws_endpoint() -> str:
    return SERVER.replace('https://', 'wss://').replace('http://', 'ws://') + '/wsapp/'


def fetch_questions(lesson_id: str, lesson_token: str, user_id: int = 0,
                    cookie: str = '', wait: float = 12.0) -> tuple[list[dict], str]:
    """连一次网关，收 hello 回包里的 timeline / unlockedproblem，解析成题目。

    返回 (题目列表, 错误说明)。同步阻塞，调用方放后台线程。
    """
    import yktws
    headers = {
        'Accept': '*/*',
        'User-Agent': core.UA,
        'Origin': SERVER,
        'Referer': SERVER + '/lesson/fullscreen/v3/',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    }
    if cookie:
        headers['Cookie'] = cookie

    collected: list[dict] = []
    unlocked: list[dict] = []
    auth_fail = ''
    got_hello = False
    ws = None
    try:
        ws = yktws.WS(_ws_endpoint(), headers, timeout=wait)
        ws.connect()
        ws.send_text(json.dumps({
            'op': 'hello', 'userid': str(user_id or ''), 'avatar': '',
            'role': 'student', 'auth': lesson_token or '',
            'lessonid': lesson_id or '', 'ver': 1,
        }, ensure_ascii=False, separators=(',', ':')))
        log(f'[WS] 已发 hello lesson={lesson_id} userid={user_id or "?"}')

        deadline = time.time() + max(4.0, wait)
        while time.time() < deadline:
            msg = ws.recv_json(timeout=max(1.0, deadline - time.time()))
            if msg is None:
                if ws._closed:
                    break
                continue
            op = str(msg.get('op') or '')
            if op == 'hello':
                m = str(msg.get('message') or '')
                st = msg.get('status')
                if 'authorize failed' in m.lower() or str(st).lower() == 'false':
                    auth_fail = m or 'authorize failed'
                    break
                got_hello = True
                for one in (msg.get('timeline') or []):
                    if isinstance(one, dict):
                        collected.append(one)
                for one in (msg.get('unlockedproblem') or []):
                    unlocked.append(one if isinstance(one, dict) else {'prob': one})
                break
            for one in (msg.get('timeline') or []):
                if isinstance(one, dict):
                    collected.append(one)
            for one in (msg.get('unlockedproblem') or []):
                unlocked.append(one if isinstance(one, dict) else {'prob': one})

        if auth_fail:
            return [], f'鉴权失败：{auth_fail}（lessonToken 无效或已过期，重新签到一次再试）'
        if not got_hello and not collected and not unlocked:
            return [], '网关没有回 hello（可能超时或 lessonToken 不被接受）'

        items = list(collected)
        for one in unlocked:
            if not any(i.get('prob') == one.get('prob') for i in items):
                items.append({'op': 'unlockproblem', **one})
        qs = parse_timeline(items)
        log(f'[WS] timeline {len(collected)} 项 + unlocked {len(unlocked)} 项 -> 题目 {len(qs)} 道')
        for q in qs[:6]:
            log(f'     · {q["problemId"]} {q["typeName"]} dt={q["dt"]} '
                f'limit={q["limitSec"]} closed={q["closed"]} answered={q["answered"]}')
        return qs, ''
    except Exception as exc:                     # noqa: BLE001
        return [], f'{type(exc).__name__}: {exc}'
    finally:
        if ws:
            try:
                ws.close()
            except OSError:
                pass


# --------------------------------------------------------------------- 自检

def selftest() -> int:
    """命令行自检：打印环境、跑一次 on-lesson，结论写日志。"""
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')      # 别让控制台把中文编码成 GBK
    except (AttributeError, OSError):
        pass
    out = []
    out.append(f'解释器    {__import__("sys").executable}')
    out.append(f'配置目录  {core.CONFIG_DIR}')
    out.append(f'curl      {core.curl_path()}')
    out.append(f'日志      {core.LOG_DIR}')
    out.append(f'回放模式  {core.replay_dir() or "关"}')
    y = Ykt()
    c = y.cookie
    out.append(f'cookie    {"有(%d 字符)" % len(c) if c else "无"}')
    out.append(f'身份      {"App" if core.APP_IDENTITY else "web"}')
    if c:
        try:
            rows = y.on_lesson()
            out.append(f'课堂列表  {len(rows)} 节')
            for r in rows[:5]:
                out.append(f'   · {r["lessonId"]}  {r["name"]}')
        except RuntimeError as exc:
            out.append(f'课堂列表  失败 {exc}')
    text = '\n'.join(out)
    print(text)
    log('[selftest]\n' + text)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(selftest())