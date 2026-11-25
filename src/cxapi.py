"""cxapi.py —— 学习通（超星）签到协议层。

从 course_helper（Flutter/Dart，GPL-3.0）的 lib/api/*.dart 逐条移植，
只保留协议本身，不碰它的界面。原项目地址：
https://github.com/AneryCoft/course_helper

移植时按「一个接口一段注释」保留了原始 URL 和参数名，方便对着改；
Dart 的 null 语义在 Python 里用 '' 代替，接口对空串和缺字段的处理是一样的。

三条容易踩空的规矩，先写在前面：

1. 所有签到接口都是 application/x-www-form-urlencoded，不是 JSON。
   走 yktcore.request(form=...) 这条分支。

2. 签名参数 sc / cid 来自 getUserInfo 的 clientId——那段是拿 RSA 公钥
   做模幂「解密」出来的，见 cxcrypto.rsa_decrypt_with_public。
   没有 sc 就没法签位置签到，所以登录后必须调一次 getUserInfo。

3. deviceCode 每次现算（AES-ECB 一个随机 UUID），不用持久化；
   而 uniqueId 必须持久化——它进了 schild 和 User-Agent，
   每次请求换一个会被服务端当异常设备。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import uuid as _uuid
from pathlib import Path

import cxcrypto as cc
import cxdes
import yktcore as core

# --------------------------------------------------------------------- 常量

# 各种 key / 盐，全部来自 encrypt.dart 的 Constant 类
SALT_SCHILD = r'ipL$TkeiEmfy1gTXb2XHrdLN0a@7c^vu'
SALT_FACE = 'uWwjeEKsri'
KEY_WEB_LOGIN = 'u2oh6Vu^HWe4_AES'
KEY_SEND_CAPTCHA = 'jsDyctOCnay7uotq'
KEY_APP_LOGIN = 'z4ok6lu^oWp4_AES'
KEY_DEVICE_CODE = 'QrCbNY@MuK1X8HGw'
RSA_KEY = ('MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC79d8Ot0hCbxxSISC6x8SCwTBspFSz'
           'lLKHJUYqoFNu1TSRaw4hEYkOnvEaL1VyoxV6HXcDrzwYvaFZaZaPQPFnfCHZy5dQwx'
           'cmifgSHqS+oKXw40Ys4cVIqnU5d90S7EWSRdBglX489jlqVaNcQSkDx2TYmC+DbAq'
           '9FV/BU09ISQIDAQAB')
TOKEN_INF_ENC = '4faa8662c59590c6f43ae9fe5b002b42'
KEY_INF_ENC = 'Z(AfY@XS'
KEY_IM = 'SL2(M/eD'
CAPTCHA_ID = 'Qt9FIw9o4pwRjOyqM6yizZBh682qN2TU'

MOBILE = 'https://mobilelearn.chaoxing.com'
PASSPORT = 'https://passport2.chaoxing.com'
PASSPORT_API = 'https://passport2-api.chaoxing.com'
SSO = 'https://sso.chaoxing.com'
MOOC_API = 'https://mooc1-api.chaoxing.com'
PAN = 'https://pan-yz.chaoxing.com'
CAPTCHA_HOST = 'https://captcha.chaoxing.com'

SIGN_URL = MOBILE + '/pptSign/stuSignajax'
GROUP_SIGN_URL = MOBILE + '/sign/stuSignajax'

# 设备伪装：整套 App 头要一致，改一个就会在 schild 上对不上
_DEV_MODEL = 'Pixel 9 Pro'
_DEV_BRAND = 'google'
_DEV_SYS_VER = '16'
_DEV_BUILD = '1610'
_DEV_INCREMENTAL = '14624737'
_DEV_SYS_AGENT = ('Dalvik/2.1.0 (Linux; U; Android 16; Pixel 9 Pro '
                  'Build/BP4A.260205.002)')
_CX_PRODUCT = '3'
_CX_VERSION = '6.7.4'
_CX_VERSION_CODE = '10940'
_CX_API_VERSION = '314'


# --------------------------------------------------------------- 凭据存储

def _config_file() -> Path:
    return core.CONFIG_FILE.parent / 'cx.json'


class CxStore:
    """学习通登录态。和雨课堂那份 config.json 分开存，互不干扰。

    cookie 明文存是本机单用户场景的取舍：这台机器上要加密也只是 DPAPI
    对当前用户，换个进程还是能解，收益有限。
    """

    def __init__(self, path: Path | None = None):
        self.path = path or _config_file()

    def read(self) -> dict:
        try:
            obj = json.loads(self.path.read_text('utf-8'))
            return obj if isinstance(obj, dict) else {}
        except (OSError, ValueError):
            return {}

    def write(self, obj: dict) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), 'utf-8')
            tmp.replace(self.path)
            return True
        except OSError as exc:
            core.log(f'[学习通] 配置写入失败 {exc}')
            return False

    def get_cookie(self) -> str:
        return str(self.read().get('cookie') or '')

    def save_cookie(self, cookie: str) -> bool:
        obj = self.read()
        obj['cookie'] = cookie
        return self.write(obj)

    def get_unique_id(self) -> str:
        """设备唯一标识。第一次生成后必须固定，它进了 schild 和 UA。"""
        obj = self.read()
        uid = str(obj.get('uniqueId') or '')
        if not uid:
            uid = _uuid.uuid4().hex
            obj['uniqueId'] = uid
            self.write(obj)
        return uid

    def get_user(self) -> dict:
        u = self.read().get('user')
        return u if isinstance(u, dict) else {}

    def save_user(self, user: dict) -> bool:
        obj = self.read()
        obj['user'] = user
        return self.write(obj)

    def clear(self) -> bool:
        """只清登录态，保留 uniqueId——设备指纹不该跟着账号变。"""
        obj = self.read()
        obj.pop('cookie', None)
        obj.pop('user', None)
        return self.write(obj)


# ------------------------------------------------------------------ 请求头

def build_headers(unique_id: str) -> dict:
    """拼学习通 App 的请求头。schild 是 UA 的指纹，算法见 encrypt.dart。

    注意不设 Accept-Encoding：curl 不像浏览器那样自动解 gzip，
    带上这个头服务端会回压缩过的二进制，json.loads 直接炸。
    真需要压缩就在 request() 里加 --compressed，别在头里写。
    """
    temp = (f'(device:{_DEV_MODEL}) Language/zh_CN com.chaoxing.mobile/'
            f'ChaoXingStudy_{_CX_PRODUCT}_{_CX_VERSION}_android_phone_'
            f'{_CX_VERSION_CODE}_{_CX_API_VERSION} (@Kalimdor)_{unique_id}')
    schild = cc.md5(f'(schild:{SALT_SCHILD}) {temp}')
    ua = f'{_DEV_SYS_AGENT} (schild:{schild}) {temp}'
    return {
        'User-Agent': ua,
        'Accept-Language': 'zh_CN',
        'Connection': 'keep-alive',
        'content-type': 'application/x-www-form-urlencoded',
    }


def device_code() -> str:
    """设备指纹：每次现算，一个随机 UUID 过 AES-ECB。"""
    return cc.aes_ecb_encrypt(_uuid.uuid4().hex, KEY_DEVICE_CODE)


# -------------------------------------------------------------------- 会话

def parse_all_cookies(raw_headers: str) -> dict[str, str]:
    """把响应里所有 Set-Cookie 收成 dict。

    不能复用 yktcore.parse_set_cookies：那个只留 sessionid/csrftoken（雨课堂
    够用），学习通的登录态散在 _uid / _d / uf / fid / DSSTASH_LOG 一堆键上，
    少一个就退成未登录。这里全收。
    """
    jar: dict[str, str] = {}
    for line in (raw_headers or '').replace('\r\n', '\n').split('\n'):
        low = line.lower()
        if not low.startswith('set-cookie:'):
            continue
        v = line[len('set-cookie:'):].strip()
        semi = v.find(';')
        if semi > 0:
            v = v[:semi]
        eq = v.find('=')
        if eq <= 0:
            continue
        jar[v[:eq].strip()] = v[eq + 1:].strip()
    return jar


class Cx:
    """一个学习通账号的会话。

    典型用法：
        cx = Cx()
        if cx.cookie and cx.refresh_user():   # 有登录态且还能用
            ...
        else:
            cx.login_by_password(phone, pwd)  # 或 login_qr() 扫码
    """

    def __init__(self, store: CxStore | None = None):
        self.store = store or CxStore()
        self.cookie = self.store.get_cookie()
        self.unique_id = self.store.get_unique_id()
        self.headers = build_headers(self.unique_id)
        self._jt_lock = threading.Lock()
        try:
            cached = self.store.read().get('joinTimes')
            self._join_time_cache: dict[str, str] = (
                dict(cached) if isinstance(cached, dict) else {}
            )
        except Exception:                                     # noqa: BLE001
            self._join_time_cache = {}
        u = self.store.get_user()
        self.uid = str(u.get('uid') or '')
        self.name = str(u.get('name') or '')
        self.school = str(u.get('school') or '')
        self.avatar = str(u.get('avatar') or '')
        # sc / cid 是签名参数，没有它们位置签到签不了
        self.device = u.get('deviceInfo') if isinstance(u.get('deviceInfo'), dict) else {}

    # -- 基础 --------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return bool(self.cookie and self.uid)

    def _get(self, url: str, params: dict | None = None, **kw) -> core.Resp:
        if params:
            url = url + ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
        return core.request('GET', url, cookie=self.cookie,
                            extra_headers=self.headers, tag='cx', **kw)

    def _post(self, url: str, form: dict | None = None, **kw) -> core.Resp:
        return core.request('POST', url, cookie=self.cookie, form=form or {},
                            extra_headers=self.headers, tag='cx', **kw)

    def _json(self, r: core.Resp) -> dict:
        try:
            obj = json.loads(r.body)
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}

    def _absorb_cookies(self, r: core.Resp) -> None:
        """把 set-cookie 并进当前 cookie 串。登录链路全靠这个攒登录态。"""
        fresh = parse_all_cookies(r.headers)
        if not fresh:
            return
        jar = {}
        for part in (self.cookie or '').split(';'):
            if '=' in part:
                k, v = part.split('=', 1)
                jar[k.strip()] = v.strip()
        jar.update(fresh)
        self.cookie = '; '.join(f'{k}={v}' for k, v in jar.items() if k)
        self.store.save_cookie(self.cookie)

    # -- 登录 --------------------------------------------------------------

    def login_by_password(self, phone: str, password: str) -> tuple[bool, str]:
        """账号密码登录。学习通 web 登录把账号密码都 AES-CBC 加密后提交。"""
        form = {
            'fid': '-1',
            'uname': cc.aes_cbc_encrypt(phone, KEY_WEB_LOGIN),
            'password': cc.aes_cbc_encrypt(password, KEY_WEB_LOGIN),
            't': 'true',
            'forbidotherlogin': '0',
            'validate': '',
        }
        r = core.request('POST', PASSPORT + '/fanyalogin', form=form,
                         extra_headers=self.headers, with_headers=True,
                         follow=True, tag='cx-login')
        self._absorb_cookies(r)
        d = self._json(r)
        # {"status":true,"msg2":"验证通过","mes":"...","url":"..."}
        if d.get('status') is True or str(d.get('result')) == '1':
            ok, msg = self.refresh_user()
            return (ok, msg if ok else '登录成功但取用户信息失败')
        return False, str(d.get('msg2') or d.get('mes') or d.get('msg') or '登录失败')

    def login_qr_new(self) -> dict:
        """取二维码登录用的 uuid/enc。前端自己把 uuid 画成二维码。"""
        r = self._post(PASSPORT + '/refreshQRCode', {}, with_headers=True)
        self._absorb_cookies(r)
        d = self._json(r)
        return {'uuid': str(d.get('uuid') or ''), 'enc': str(d.get('enc') or '')}

    def login_qr_poll(self, qr_uuid: str, enc: str) -> tuple[int, str]:
        """查扫码状态。返回 (状态, 说明)，状态取 waiting/scanned/ok/fail。

        getauthstatus 的返回在不同版本里字段名变过，这里按语义兜住：
        status/type 为真且带 url 就是登录成功。
        """
        r = self._post(PASSPORT + '/getauthstatus/v2', {
            'enc': enc, 'uuid': qr_uuid,
            'doubleFactorLogin': '0', 'forbidotherlogin': '0',
        }, with_headers=True)
        self._absorb_cookies(r)
        d = self._json(r)
        status = d.get('status')
        if status is True or str(status) == '2' or d.get('url'):
            ok, msg = self.refresh_user()
            return ('ok', '登录成功') if ok else ('fail', msg)
        if str(status) == '1':
            return 'scanned', '已扫码，请在手机上确认'
        return 'waiting', str(d.get('msg') or '等待扫码')

    def refresh_user(self) -> tuple[bool, str]:
        """拉用户信息。顺带把 RSA 解出来的 sc/cid 存下来——签到签名要它。"""
        dev_id = self.unique_id
        device_info = {
            'app_name': 'com.chaoxing.mobile',
            'app_ver': _CX_VERSION,
            'board': 'caiman',
            'brand': _DEV_BRAND,
            'cdid': dev_id,
            'cdtype': _DEV_MODEL,
            'cpu_ar': 'arm64-v8a,armeabi-v7a,armeabi',
            'device_id': dev_id,
            'dpi': '440',
            'hardware': 'caiman',
            'mediaDrmId': '',
            'oaid': '1004',
            'os_lang': '',
            'os_name': 'REL',
            'os_ver': _DEV_SYS_VER,
            'platform': 'android',
            'resolution': '1080*2243',
            'time_stamp': int(time.time() * 1000),
        }
        form = {'data': cc.rsa_encrypt(json.dumps(device_info, separators=(',', ':')),
                                      RSA_KEY)}
        r = core.request('POST', SSO + '/apis/login/userLogin4Uname.do', form=form,
                         cookie=self.cookie, extra_headers=self.headers,
                         with_headers=True, follow=True, tag='cx-userinfo')
        self._absorb_cookies(r)
        d = self._json(r)
        if str(d.get('result')) != '1':
            return False, str(d.get('errorMsg') or d.get('msg') or '获取用户信息失败')
        msg = d.get('msg') or {}
        if not isinstance(msg, dict):
            return False, '用户信息格式异常'

        self.uid = str(msg.get('puid') or '')
        self.name = str(msg.get('name') or '')
        self.school = str(msg.get('schoolname') or '')
        self.avatar = str(msg.get('pic') or '')

        # clientId 是 RSA「解密」出来的 {"sc":..,"cid":..}，没有它签不了到
        plain = cc.rsa_decrypt_with_public(str(msg.get('clientId') or ''), RSA_KEY)
        try:
            self.device = json.loads(plain) if plain else {}
        except ValueError:
            self.device = {}
        if not isinstance(self.device, dict):
            self.device = {}

        # 群聊签到要用的 IM 账号：登录响应 msg.accountInfo.imAccount
        #   username  明文
        #   password  DES-ECB 十六进制密文，key = "SL2(M/eD"
        # 这条照抄 course_helper（lib/api/login.dart:122-127）。
        im = {}
        acct = msg.get('accountInfo')
        if isinstance(acct, dict):
            im_raw = acct.get('imAccount')
            if isinstance(im_raw, dict) and im_raw.get('username'):
                pwd = str(im_raw.get('password') or '')
                plain = ''
                if pwd:
                    try:
                        plain = cxdes.decrypt_hex(pwd, IM_KEY)
                    except Exception as exc:                 # noqa: BLE001
                        core.log(f'[学习通] IM 密码解密失败：{exc}')
                im = {'userName': str(im_raw.get('username') or ''),
                      'password': plain}
                core.log('[学习通] 拿到 IM 账号' + ('（密码已解出）' if plain else '（密码未解出）'))

        self.store.save_user({
            'uid': self.uid, 'name': self.name, 'school': self.school,
            'avatar': self.avatar, 'deviceInfo': self.device,
            'imAccount': im,
        })
        return True, ''

    def logout(self) -> None:
        self.store.clear()
        self.cookie = ''
        self.uid = self.name = self.school = self.avatar = ''
        self.device = {}

    def profile(self) -> dict:
        return {'ok': bool(self.uid), 'uid': self.uid, 'name': self.name,
                'school': self.school, 'avatar': self.avatar,
                'hasDevice': bool(self.device.get('sc'))}

    # -- 课程与活动 --------------------------------------------------------

    def courses(self, include_rest: bool = False) -> list[dict]:
        """课程列表。

        state 为 0 的班**也要留着**：教务后台安排的课常常还没"激活"，
        或者压根不走课程中心那套状态位，原来的过滤会把它们直接丢掉，
        于是老师发起签到了这边什么都扫不到（用户反馈："手动加课几乎没卵用，
        普通用户从 UI 怎么拿 courseId"）。真正的判据应该是活动时间窗口，
        那件事在 activities() 里做，不该在课程列表这层提前砍。

        include_rest=True 时连 state=0 的一起返回，每项带 state 与 inactive 标记。
        """
        r = self._get(MOOC_API + '/mycourse/backclazzdata',
                      {'view': 'json', 'getTchClazzType': '1', 'mcode': ''})
        d = self._json(r)
        if str(d.get('result')) != '1':
            return []
        out = []
        for ch in d.get('channelList') or []:
            if not isinstance(ch, dict):
                continue
            content = ch.get('content') or {}
            st_raw = content.get('state')
            inactive = str(st_raw) in ('0', 'False', 'false', 'None') or st_raw in (0, False)
            if inactive and not include_rest:
                continue
            # course 有两种形态：旧版是扁平 dict（id/clazzid/name），新版包了一层
            # {"data": [{...}]}。商益学这类走新版，按老结构取字段会全空，
            # 导致活动请求带着空 courseId 打出去直接 500，签到自然检测不到。
            course = content.get('course') or {}
            data = course.get('data')
            if isinstance(data, list) and data and isinstance(data[0], dict):
                c0 = data[0]
                course_id = c0.get('id')
                class_id = ch.get('id') or c0.get('classId') or content.get('id')
                name = c0.get('name')
            else:
                course_id = course.get('id')
                class_id = course.get('clazzid') or content.get('id')
                name = course.get('name')
            cpi = content.get('cpi') or ch.get('cpi') or content.get('personid')
            if not course_id:
                continue
            # 老师名在新版里是 course.data[0].teacherfactor，旧版是 teacher.name
            teacher = ''
            if isinstance(data, list) and data:
                teacher = str((data[0] or {}).get('teacherfactor') or '')
            else:
                t = content.get('teacher')
                teacher = str((t or {}).get('name') or '') if isinstance(t, dict) else ''
            out.append({
                'courseId': str(course_id),
                'classId': str(class_id or ''),
                'cpi': str(cpi or ''),
                'name': str(name or content.get('name') or ''),
                'teacher': teacher,
                'state': st_raw,
                'inactive': inactive,
            })
        return out

    def join_class_time(self, course_id: str, class_id: str, cpi: str) -> str:
        """进班时间。taskactivelist 把它当必填参数，缺了拿不到列表。

        同一门班级的进班时间是静态属性，内存 + cx.json 缓存后不再每次扫描重复查。
        """
        key = f'{self.uid}|{course_id}|{class_id}'
        with self._jt_lock:
            hit = self._join_time_cache.get(key)
        if hit is not None:
            return hit
        r = self._get(MOOC_API + '/gas/clazzperson', {
            'courseid': course_id, 'clazzid': class_id, 'userid': self.uid,
            'personid': cpi, 'view': 'json',
            'fields': 'clazzid,popupagreement,personid,clazzname,createtime',
        })
        d = self._json(r)
        data = d.get('data') or []
        val = ''
        if isinstance(data, list) and data and isinstance(data[0], dict):
            val = str(data[0].get('createtime') or '')
        if val:
            with self._jt_lock:
                self._join_time_cache[key] = val
                snap = dict(self._join_time_cache)
            try:
                blob = self.store.read()
                blob['joinTimes'] = snap
                self.store.write(blob)
            except Exception:                                 # noqa: BLE001
                pass
        return val

    def _sign_enc_params(self, params: dict) -> dict:
        """给一组参数补上 inf_enc 那套校验字段。"""
        extra = {
            '_c_0_': _uuid.uuid4().hex,
            'token': TOKEN_INF_ENC,
            '_time': str(int(time.time() * 1000)),
        }
        merged = dict(params)
        merged.update(extra)
        qs = '&'.join(f'{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}'
                      for k, v in merged.items())
        extra['inf_enc'] = cc.md5(f'{qs}&DESKey={KEY_INF_ENC}')
        return extra

    def activities(self, course_id: str, class_id: str, cpi: str) -> list[dict]:
        """活动列表。两路接口合并：App 那份带 otherId（签到类型），
        Web 那份带 nameFour（活动描述），缺一边都判不准签到类型。

        只返回「进行中且未签」的签到。两路的 activeList 都混着已结束的历史
        （web 路 showNotStartedActive=0 只过滤未开始的），旧逻辑全量透传导致
        面板堆一堆旧签到。过滤规则：
          1. 窗口判进行中：startTime <= now <= endTime（endTime 缺失时 now >= start）
          2. attend_info 判未签：status==1 的（已签/代签/请假/缺勤）直接丢
        代价是每门课多打一次 attend_info 预检，但只打窗口内的活动，量很小。
        """
        join_time = self.join_class_time(course_id, class_id, cpi)

        app_params = {
            'courseId': course_id, 'classId': class_id, 'uid': self.uid,
            'cpi': cpi, 'joinclasstime': join_time,
        }
        app_params.update(self._sign_enc_params(app_params))
        r1 = self._get(MOBILE + '/ppt/activeAPI/taskactivelist', app_params)
        d1 = self._json(r1)
        now = time.time() * 1000

        def _is_in_window(a: dict) -> bool:
            t = _int(a.get('activeType'), 0)
            if t not in (2, 74, 54):
                return False
            start = _int(a.get('startTime'), 0)
            end = _int(a.get('endTime'), 0)
            st = _int(a.get('status'), 0)
            if not start:
                return False
            if end:
                return start <= now <= end
            return (st == 1) and (start <= now)

        d1_list = d1.get('activeList') or [] if isinstance(d1, dict) else []
        d1_valid = isinstance(d1, dict) and ('activeList' in d1 or 'groupList' in d1)
        has_live_in_d1 = any(isinstance(a, dict) and _is_in_window(a) for a in d1_list)

        # 只有第一路查到进行中签到（需补 Web 路 nameFour/endTime）或第一路异常时，
        # 才补打第二路 student/activelist；绝大多数无签到的课直接省掉 1 次 RTT。
        if has_live_in_d1 or not d1_valid:
            r2 = self._get(MOBILE + '/v2/apis/active/student/activelist', {
                'fid': '0', 'courseId': course_id, 'classId': class_id,
                'showNotStartedActive': '0',
                '_': str(int(time.time() * 1000)),
            })
            d2 = self._json(r2)
        else:
            d2 = {}

        merged: dict[str, dict] = {}
        for a in (d1_list + ((d2.get('data') or {}).get('activeList') or [])):
            if not isinstance(a, dict):
                continue
            aid = str(a.get('id') or '')
            if not aid:
                continue
            if not _is_in_window(a):
                continue
            t = _int(a.get('activeType'), 0)
            start = _int(a.get('startTime'), 0)
            end = _int(a.get('endTime'), 0)
            row = merged.get(aid)
            if row is None:
                merged[aid] = {
                    'activeId': aid,
                    'type': t,
                    'name': str(a.get('nameOne') or a.get('nameTwo') or ''),
                    'desc': str(a.get('nameTwo') or a.get('nameFour') or ''),
                    'startTime': start,
                    'endTime': end,
                    'url': str(a.get('url') or ''),
                    'attendNum': _int(a.get('attendNum')),
                    'status': True,
                    'signType': None,
                }
            else:
                if not row['name']:
                    row['name'] = str(a.get('nameOne') or a.get('nameTwo') or '')
                if not row['desc']:
                    row['desc'] = str(a.get('nameTwo') or a.get('nameFour') or '')
                if not row['endTime']:
                    row['endTime'] = end
            # otherId 判签到类型（0 普通/2 二维码/3 手势/4 位置/5 签到码）
            other = a.get('otherId')
            if other is not None and t in (2, 74):
                st = _sign_type_from_index(_int(other))
                if st and (not merged[aid]['signType'] or st == 'location'):
                    merged[aid]['signType'] = st

        # 预检过滤：窗口内但已签的（status==1 已签/代签/请假/缺勤）丢。
        out = []
        for row in merged.values():
            attend = {}
            try:
                attend = self.attend_info(row['activeId'])
            except Exception:                                 # noqa: BLE001
                pass
            if _int(attend.get('status'), 0) == 1:
                continue
            row['_attend'] = attend
            out.append(row)
        return out

    # -- 签到详情 ----------------------------------------------------------

    def sign_detail(self, active_id: str, code: str = '') -> dict:
        """签到详细。二维码那条要带 msg=code 才能判过期。"""
        url = f'{MOBILE}/newsign/signDetail?activePrimaryId={active_id}&type=1'
        if code:
            url += '&msg=' + urllib.parse.quote(code)
        return self._json(self._get(url))

    def active_info(self, active_id: str) -> dict:
        """活动详细：otherId 定签到类型，showVCode 定要不要验证码。"""
        d = self._json(self._get(MOBILE + '/v2/apis/active/getPPTActiveInfo',
                                 {'activeId': active_id}))
        if str(d.get('result')) != '1':
            return {}
        data = d.get('data')
        return data if isinstance(data, dict) else {}

    def attend_info(self, active_id: str) -> dict:
        """我的签到状态。status 0=未签 1=已签 其他见 STATUS_TEXT。"""
        d = self._json(self._get(MOBILE + '/v2/apis/sign/getAttendInfo', {
            'activeId': active_id, 'moreClassAttendEnc': '',
        }))
        if str(d.get('result')) != '1':
            return {}
        data = d.get('data')
        return data if isinstance(data, dict) else {}

    def check_sign_code(self, active_id: str, sign_code: str) -> tuple[bool, str]:
        """预检手势/签到码。先问一次再提交，省得把验证次数用光被锁一小时。"""
        d = self._json(self._get(
            MOBILE + '/widget/sign/pcStuSignController/checkSignCode',
            {'activeId': active_id, 'signCode': sign_code}))
        if str(d.get('result')) == '1':
            return True, ''
        return False, str(d.get('errorMsg') or '验证失败')

    def group_sign_detail(self, active_id: str) -> dict:
        return self._json(self._get(MOBILE + '/sign/getSignDetail',
                                    {'id': active_id}))

    # -- 签名工具 ----------------------------------------------------------

    def sign_token(self, fields: dict) -> str:
        """学习通那套 signToken：键名排序拼「键+值」，末尾接 sc 再 MD5。"""
        buf = ''.join(f'{k}{fields[k]}' for k in sorted(fields))
        return cc.md5(buf + str(self.device.get('sc') or ''))

    def location_payload(self, address: str, lat: float, lng: float) -> tuple[str, str]:
        """位置签到要的两段 JSON：location 和 locationResult。"""
        loc = json.dumps({'result': 1, 'address': address,
                          'longitude': lng, 'latitude': lat},
                         ensure_ascii=False, separators=(',', ':'))
        ts = str(int(time.time() * 1000))
        cid = str(self.device.get('cid') or '')
        inner = (f'{{"latitude":{lat},"longitude":{lng},'
                 f'"address":"{address}"}}')
        token = self.sign_token({'data': inner, 'cxtime': ts, 'cxcid': cid})
        result = json.dumps({
            'result': 1, 'latitude': lat, 'longitude': lng,
            'mockData': {'strategy': 0, 'probability': -1},
            'locType': 161, 'address': address,
            'signConfig': {'signToken': token, 'cxcid': cid, 'cxtime': ts},
        }, ensure_ascii=False, separators=(',', ':'))
        return loc, result


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# otherId -> 签到类型。0 普通 / 2 二维码 / 3 手势 / 4 位置 / 5 签到码
SIGN_TYPES = {0: 'normal', 2: 'qrcode', 3: 'pattern', 4: 'location', 5: 'code'}
# 学习通 IM 密码的 DES key（course_helper: utils/encrypt.dart:40）
IM_KEY = 'SL2(M/eD'


def _sign_type_from_index(idx: int) -> str | None:
    return SIGN_TYPES.get(idx)


# ------------------------------------------------------- 签到提交（五种）

def _base_sign_form(self, course_id: str, active_id: str) -> dict:
    """五种签到共用的那批字段。appType=15 是 Android 客户端。"""
    return {
        'activeId': active_id,
        'courseId': course_id,
        'uid': self.uid,
        'clientip': '',
        'latitude': '-1',
        'longitude': '-1',
        'appType': '15',
        'fid': '0',
        'name': self.name,
        'deviceCode': device_code(),
    }


def _judge(r, active_id: str) -> tuple[bool, str]:
    """把签到接口的纯文本回执翻译成 (成功?, 说明)。"""
    text = (r.body or '').strip()
    core.log(f'[学习通签到] active={active_id} resp={text[:120]}')
    if text == 'success':
        return True, ''
    if text == 'success2':
        return False, '已过截止时间'
    if text.startswith('validate'):
        return False, text                     # 交给上层走验证码
    if text.startswith('签到失败['):
        # 90002 是手势/签到码不对，带方括号的码原样给出去便于排查
        return False, text
    if not text:
        return False, f'签到无响应（HTTP {r.status}）'
    return False, text


def _submit(self, form: dict, referer: str, method: str = 'GET') -> tuple[bool, str]:
    """提交签到并翻译结果。

    接口回的是纯文本，不是 JSON：
      success                成功
      success2               已过截止时间
      签到失败[90002]        手势/签到码不对（或过期）
      签到失败，请重新扫描。 二维码过期
      validate_xxx           要验证码，xxx 是 enc2

    **一律用 GET**。实测（2026-11-23）：同一套字段 POST 给
    /pptSign/stuSignajax 会被 tengine 直接打 500 HTML，GET 则正常
    返回业务文案。位置签到、手势签到、普通签到都是这个规矩。
    """
    if method.upper() == 'GET':
        url = SIGN_URL + '?' + urllib.parse.urlencode(form)
        r = core.request('GET', url, cookie=self.cookie, referer=referer,
                         extra_headers=self.headers, timeout=25, tag='cx-sign')
    else:
        r = core.request('POST', SIGN_URL, cookie=self.cookie, form=form,
                         referer=referer, extra_headers=self.headers,
                         timeout=25, tag='cx-sign')
    return _judge(r, str(form.get('activeId') or ''))


def sign_normal(self, course_id: str, active_id: str, referer: str,
                object_id: str = '', validate: str = '') -> tuple[bool, str]:
    """普通签到（可带照片）。有 objectId 走 POST，没有走 GET——原项目如此。"""
    form = _base_sign_form(self, course_id, active_id)
    if validate:
        form['validate'] = validate
    if object_id:
        form['objectId'] = object_id
        return _submit(self, form, referer)
    url = SIGN_URL + '?' + urllib.parse.urlencode(form)
    r = core.request('GET', url, cookie=self.cookie, extra_headers=self.headers,
                     referer=referer, timeout=25, tag='cx-sign')
    return _judge(r, active_id)


def sign_code(self, course_id: str, active_id: str, sign_code: str, referer: str,
              validate: str = '', address: str = '', lat: float | None = None,
              lng: float | None = None) -> tuple[bool, str]:
    """手势签到和签到码签到共用这个接口，区别只在 signCode 的内容。"""
    form = _base_sign_form(self, course_id, active_id)
    form['signCode'] = sign_code
    if validate:
        form['validate'] = validate
    if address and lat is not None and lng is not None:
        form['latitude'] = f'{lat:.6f}'
        form['longitude'] = f'{lng:.6f}'
        loc, result = self.location_payload(address, lat, lng)
        form['location'] = loc
        form['locationResult'] = result
    return _submit(self, form, referer)


def sign_location(self, course_id: str, active_id: str, address: str,
                  lat: float, lng: float, referer: str, validate: str = '',
                  face_id: str = '', face_enc: str = '') -> tuple[bool, str]:
    """位置签到。防作弊字段留空是原项目的做法，服务端不校验空值。"""
    form = _base_sign_form(self, course_id, active_id)
    form.update({
        'address': address,
        'latitude': f'{lat:.6f}',
        'longitude': f'{lng:.6f}',
        'ifTiJiao': '1',
        'vpProbability': '-1',
        'vpStrategy': '',
        'currentFaceId': '',
        'ifCFP': '0',
        'faceEnc': '',
    })
    if validate:
        form['validate'] = validate
    if face_id:
        form['currentFaceId'] = face_id
        form['ifCFP'] = '1'
    if face_enc:
        form['faceEnc'] = face_enc
    _, result = self.location_payload(address, lat, lng)
    form['locationResult'] = result
    # _submit 默认 GET——位置签到 POST 会被打 500，见 _submit 的实测记录
    return _submit(self, form, referer)


def sign_qrcode(self, course_id: str, active_id: str, enc: str, referer: str,
                address: str = '', lat: float | None = None,
                lng: float | None = None, enc2: str = '', validate: str = '',
                face_id: str = '', face_enc: str = '') -> tuple[bool, str]:
    """二维码签到。enc 从二维码 URL 里取，enc2/validate 是验证码那轮才有的。"""
    form = _base_sign_form(self, course_id, active_id)
    form.update({
        'enc': enc,
        'location': '',
        'vpProbability': '',
        'vpStrategy': '',
        'currentFaceId': '',
        'ifCFP': '0',
        'faceEnc': '',
    })
    if address and lat is not None and lng is not None:
        loc, result = self.location_payload(address, lat, lng)
        form['location'] = loc
        form['locationResult'] = result
    if enc2 and validate:
        form['enc2'] = enc2
        form['validate'] = validate
    if face_id:
        form['currentFaceId'] = face_id
        form['ifCFP'] = '1'
    if face_enc:
        form['faceEnc'] = face_enc
    return _submit(self, form, referer)


def sign_group(self, active_id: str, object_id: str = '', address: str = '',
               lat: float | None = None, lng: float | None = None
               ) -> tuple[bool, str]:
    """群聊签到。另一套接口，没有签到码和防作弊，鉴权也松。"""
    params = {'activeId': active_id, 'uid': self.uid, 'clientip': ''}
    if object_id:
        params['objectId'] = object_id
    elif address:
        params.update({'address': address, 'fid': '', 'ifTiJiao': '1'})
        if lat is not None and lng is not None:
            params['latitude'] = f'{lat:.6f}'
            params['longitude'] = f'{lng:.6f}'
    r = core.request('GET', GROUP_SIGN_URL + '?' + urllib.parse.urlencode(params),
                     cookie=self.cookie, extra_headers=self.headers,
                     timeout=25, tag='cx-group-sign')
    text = (r.body or '').strip()
    if text == 'success':
        return True, ''
    return False, text or f'签到无响应（HTTP {r.status}）'


for _fn in (sign_normal, sign_code, sign_location, sign_qrcode, sign_group):
    setattr(Cx, _fn.__name__, _fn)


# ---------------------------------------------- 验证码 / 人脸 / 图片上传

class CxCaptcha:
    """滑动验证码。学习通验证码是 JSONP 套一层 cx_captcha_function(...)。

    流程：取配置拿 t -> 用 t 算 captchaKey/token/iv -> 取图 -> 提交 x 坐标
    -> 服务端回 extraData.validate，这个 validate 就是签到接口要的字段。
    """

    _RE = None

    def __init__(self):
        import re
        self._re = re.compile(r'cx_captcha_function\((.+)\)', re.S)
        self.timestamp = 0
        self.iv = ''

    def _uuid(self) -> str:
        return _uuid.uuid4().hex

    def fetch(self, referer: str) -> dict:
        """取验证码图片。返回 {ok, bg, slice, token, y, error}。"""
        self.timestamp = int(time.time() * 1000)
        r = core.request('GET', CAPTCHA_HOST + '/captcha/get/conf?' +
                         urllib.parse.urlencode({
                             'callback': 'cx_captcha_function',
                             'captchaId': CAPTCHA_ID,
                             '_': str(self.timestamp),
                         }), timeout=20, tag='cx-captcha')
        conf = self._parse(r.body)
        if not conf:
            return {'ok': False, 'error': '验证码配置解析失败'}

        service_time = _int(conf.get('t'))
        captcha_key = cc.md5(f'{service_time}{self._uuid()}')
        token = (f'{cc.md5(f"{service_time}{CAPTCHA_ID}slide{captcha_key}")}:'
                 f'{service_time + 300000}')
        self.iv = cc.md5(f'{CAPTCHA_ID}slide{self.timestamp}{self._uuid()}')

        r2 = core.request('GET', CAPTCHA_HOST + '/captcha/get/verification/image?' +
                          urllib.parse.urlencode({
                              'callback': 'cx_captcha_function',
                              'captchaId': CAPTCHA_ID,
                              'type': 'slide',
                              'version': '1.1.20',
                              'captchaKey': captcha_key,
                              'token': token,
                              'referer': referer,
                              'iv': self.iv,
                              '_': str(self.timestamp + 1),
                          }), timeout=20, tag='cx-captcha-img')
        img = self._parse(r2.body)
        if not img:
            return {'ok': False, 'error': '验证码图片解析失败'}
        # 真实结构：{token, imageVerificationVo: {type, shadeImage, cutoutImage}}
        # shadeImage = 背景（带缺口阴影），cutoutImage = 滑块那块拼图，都是远程 URL。
        vo = img.get('imageVerificationVo') if isinstance(img.get('imageVerificationVo'), dict) else {}
        return {
            'ok': True,
            'bg': str(vo.get('shadeImage') or img.get('bg') or ''),
            'slice': str(vo.get('cutoutImage') or img.get('slice') or ''),
            'token': str(img.get('token') or token),
            'iv': self.iv,
            'ts': self.timestamp,
            'width': _int(vo.get('originalWidth')) or 0,
            'y': _int(img.get('y')),
            'error': '',
        }

    def submit(self, x: float, token: str, referer: str,
               iv: str = '', ts: int = 0) -> tuple[str, str]:
        """提交滑动位置，成功返回 (validate, '')。

        iv/ts 可以从外部传回来：服务端是无状态的，取图和验证是两次请求，
        中间可能落在不同线程/实例上。
        """
        if iv:
            self.iv = iv
        if ts:
            self.timestamp = int(ts)
        if not self.iv or not self.timestamp:
            return '', '验证码未初始化'
        r = core.request('GET', CAPTCHA_HOST + '/captcha/check/verification/result?' +
                         urllib.parse.urlencode({
                             'callback': 'cx_captcha_function',
                             'captchaId': CAPTCHA_ID,
                             'type': 'slide',
                             'token': token,
                             'textClickArr': json.dumps([{'x': round(x)}],
                                                        separators=(',', ':')),
                             'coordinate': '[]',
                             'runEnv': '10',
                             'version': '1.1.20',
                             't': 'a',
                             'iv': self.iv,
                             '_': str(self.timestamp + 2),
                         }), referer=referer, timeout=20, tag='cx-captcha-ok')
        d = self._parse(r.body)
        if not d:
            return '', '验证码结果解析失败'
        if _int(d.get('error'), -1) == 0 and d.get('result') is True:
            try:
                extra = json.loads(str(d.get('extraData') or '{}'))
            except ValueError:
                return '', '验证码 extraData 异常'
            return str(extra.get('validate') or ''), ''
        return '', str(d.get('msg') or '验证码不通过')

    def _parse(self, body: str) -> dict:
        m = self._re.search(body or '')
        if not m:
            return {}
        try:
            obj = json.loads(m.group(1).strip())
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}


class CxFace:
    """人脸凭据。用账号里已有的历史人脸，不采集。

    getUserFaceid 取那张图 -> 改字节重传换新 objectId -> check-face-result
    换 faceEnc。流程允许失败，上层会退回不带人脸提交。
    """

    def __init__(self, cx: Cx):
        self.cx = cx

    def face_id(self) -> tuple[str, str]:
        enc = cc.md5(self.cx.uid + SALT_FACE)
        r = self.cx._get(f'{PASSPORT_API}/api/getUserFaceid?enc={enc}')
        d = self.cx._json(r)
        if str(d.get('result')) != '1':
            return '', '取历史人脸失败'
        data = d.get('data') or {}
        url = str(data.get('http') or '')
        original = str(data.get('objectid') or '')
        if not url:
            return '', '历史人脸没有图片地址'
        new_id, err = upload_image(self.cx, url, tag='face', vary=True)
        return (new_id or original), ('' if (new_id or original) else err)

    def face_enc(self, active_id: str, face_id: str) -> tuple[str, str]:
        ts = str(int(time.time() * 1000))
        result = {
            'currentFaceId': face_id,
            'LiveDetectionStatus': '1',
            'collectStatus': '1',
            'cxcid': str(self.cx.device.get('cid') or ''),
            'cxtime': ts,
        }
        result['signToken'] = self.cx.sign_token(result)
        r = self.cx._get(MOBILE + '/pptSign/check-face-result?' +
                         urllib.parse.urlencode({
                             'DB_STRATEGY': 'PRIMARY_KEY',
                             'STRATEGY_PARA': 'activeId',
                             'activeId': active_id,
                             'faceResult': json.dumps(result, separators=(',', ':')),
                         }))
        d = self.cx._json(r)
        if _int(d.get('status')) == 1:
            return str(d.get('enc') or ''), ''
        return '', '人脸校验不通过'


def upload_image(cx: Cx, url_or_path: str, tag: str = 'img',
                 vary: bool = False) -> tuple[str, str]:
    """把一张图传到学习通云盘，返回 objectId。

    url_or_path 是 http 地址就先下到临时文件。上传前查 CRC，已经传过就复用
    objectId。

    vary=True 时上传前追加两个字节。人脸必须开：同一张脸原样重传，CRC 命中，
    objectId 与上次相同，服务端判「环境异常」。原项目改 EXIF 的 UserComment，
    作用相同，都是让文件不同从而换到新 objectId。
    """
    import tempfile

    tmp_path = ''
    try:
        if url_or_path.startswith('http'):
            fd, tmp_path = tempfile.mkstemp(suffix='.jpg')
            os.close(fd)
            # 图床对 http:// 一律 301 到 https：不跟随就是空 body，
            # 以前这里先 request 探一下状态，301 直接当失败，人脸就废了。
            if not core.fetch_bytes(url_or_path, cookie=cx.cookie,
                                    headers=cx.headers, timeout=30,
                                    out_path=tmp_path, follow=True):
                return '', '下载图片失败'
            src = tmp_path
        else:
            src = url_or_path
            if not os.path.exists(src):
                return '', '图片文件不存在'

        if vary:
            with open(src, 'ab') as f:
                f.write(b'\x00' + str(int(time.time() * 1000)).encode())

        # 拿上传 token
        r = cx._get(PAN + '/api/token/uservalid')
        token = str(cx._json(r).get('_token') or '')
        if not token:
            return '', '取上传 token 失败'

        # 先查 CRC，命中就复用
        crc = _file_crc(src)
        r = cx._get(PAN + '/api/crcStorageStatus?' + urllib.parse.urlencode({
            'puid': cx.uid, 'crc': crc, '_token': token,
        }))
        d = cx._json(r)
        if d.get('result') and d.get('exist'):
            data = d.get('data') or {}
            oid = str(data.get('objectid') or '')
            if oid:
                return oid, ''

        r = core.request('POST', f'{PAN}/upload?_from=mobilelearn&_token={token}',
                         cookie=cx.cookie, form={'puid': cx.uid},
                         files={'file': src}, extra_headers=cx.headers,
                         timeout=60, tag=f'cx-upload-{tag}')
        d = cx._json(r)
        oid = str(((d.get('data') or {}) if isinstance(d.get('data'), dict) else {})
                  .get('objectId') or '')
        return (oid, '') if oid else ('', '上传图片失败')
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _file_crc(path: str) -> str:
    """学习通的非标准 CRC：超 1MB 取首尾各 512KB，末尾接文件大小的十六进制。"""
    import hashlib
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        if size > 1048576:
            head = f.read(524288)
            f.seek(size - 524288)
            tail = f.read(524288)
            blob = head + tail
        else:
            blob = f.read(size)
    blob += format(size, 'x').encode('utf-8')
    return hashlib.md5(blob).hexdigest()


for _fn in (_base_sign_form, _submit):
    setattr(Cx, _fn.__name__, _fn)


# 签到状态码 -> 文案，来自原项目 sign_in.dart 的 _statusTexts
STATUS_TEXT = {
    1: '签到成功', 2: '教师代签', 4: '请假', 5: '缺勤', 7: '病假',
    8: '事假', 9: '迟到', 10: '早退', 11: '签到已过期', 12: '公假',
}
