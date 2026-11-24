"""cxsign.py —— 学习通签到编排层。

cxapi 只管「怎么发一个请求」，这里管「什么时候发、发哪几个、结果怎么算」。

和雨课堂那套的区别，先讲清楚，免得用起来别扭：

雨课堂的签到是「老师开一次，进班就算」——一个 checkin 接口通吃。
学习通的签到是五种形态（普通/二维码/手势/位置/签到码），
每种要的参数不一样，能自动化的程度也不一样：

  普通签到      全自动（有照片要求时用配置里那张图）
  手势签到      半自动——手势得先知道，拿到后自动提交
  签到码签到    半自动——同上，码得先知道
  位置签到      全自动（要坐标，用配置里的默认位置）
  二维码签到    只能扫，扫完自动提交

所以这里的设计是：轮询只负责「发现 + 预检」，把每个签到标成
可自动 / 待输入 / 已签，前端照着渲染。自动的当场签掉，待输入的
把缺什么（手势/码/二维码/验证码）明确写进 missing 字段。

验证码和二维码一样，是「人机」环节，不硬猜。原项目也是弹框让人滑。
"""
from __future__ import annotations

import threading
import time

import cxapi as cx
import yktcore as core

# 签到活动类型：2 签到 / 74 签退 / 54 定时签到
SIGN_ACTIVE_TYPES = (2, 74, 54)

# 能全自动的类型（手势和签到码要人先给，二维码要扫）
AUTO_TYPES = ('normal', 'location')

# 待输入类型缺什么
NEEDS_TEXT = {
    'pattern': '手势',
    'code': '签到码',
    'qrcode': '二维码',
}


# 群聊里收到的签到活动（由前端环信 Web SDK 推送上来，server 调用 set_chat_signs）
_CHAT_SIGNS: list[dict] = []


def set_chat_signs(items: list[dict]) -> int:
    """前端把 IM 消息里解析出的活动交过来，这里存着等 scan() 合并。

    环信的消息只能由客户端 SDK 收（REST 拿不到历史消息），所以「群聊签到」
    的数据来源是前端那条 WebSocket，不是后端。存最近 200 条，按 activeId 去重。
    """
    seen = {str(x.get('activeId') or '') for x in _CHAT_SIGNS}
    added = 0
    for it in items or []:
        aid = str(it.get('activeId') or it.get('aid') or '')
        if not aid or aid in seen:
            continue
        item = {
            'activeId': aid,
            'courseId': str(it.get('courseId') or ''),
            'classId': str(it.get('classId') or ''),
            'courseName': str(it.get('courseName') or ''),
            'title': str(it.get('title') or it.get('atypeName') or '签到'),
            'desc': str(it.get('atypeName') or ''),
            'url': str(it.get('url') or ''),
        }
        _CHAT_SIGNS.append(item)
        seen.add(aid)
        added += 1
    del _CHAT_SIGNS[:-200]
    return added


class CxSigner:
    """一个学习通账号的签到器。线程安全性交给外层 STATE.lock。"""

    def __init__(self, api: cx.Cx | None = None):
        self.api = api or cx.Cx()
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}      # courseId -> 课程
        self._last_scan = 0.0
        self._signing: set[str] = set()        # 正在签的 activeId，防重入
        # 位置签到的默认坐标，由 server 从设置里灌进来。
        # 放在这里而不是让前端每次传：5 秒窗口里没人来得及输坐标。
        self.default_location: dict = {}
        # 手动加课：课程中心没收录但老师在上课的课（如商益学）。
        # 由 server 从设置灌进来，scan 时和 backclazzdata 的课合并。
        self.extra_courses: list[dict] = []
        # cpi 现查缓存：courseId|classId -> cpi
        self._cpi_cache: dict[str, str] = {}

    # -- 账号 --------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.api.ready

    def profile(self) -> dict:
        return self.api.profile()

    # -- 扫描 --------------------------------------------------------------

    def scan(self, force: bool = False, ttl: float = 30.0) -> dict:
        """扫一遍所有在上的课，收集进行中的签到活动。

        返回 {ok, courses, signs, error}。signs 里每项都带 can_auto / missing，
        前端照这个渲染按钮，不用自己猜类型。
        """
        if not self.ready:
            return {'ok': False, 'courses': [], 'signs': [], 'error': '未登录'}
        now = time.time()
        if not force and now - self._last_scan < ttl and self._cache:
            return self._snapshot()

        # 位置签到要拿 sc/cid 做签名，而它们来自 getUserInfo 的 clientId。
        # 老登录态（尤其之前 RSA 解包有 bug 时期登录的）里 device 是空的，
        # 直接签会 500。这里补一次，拿到就落盘，下次不用再补。
        if not self.api.device.get('sc'):
            try:
                self.api.refresh_user()
                core.log(f'[学习通] 补拉 device: sc={bool(self.api.device.get("sc"))}')
            except Exception as exc:                        # noqa: BLE001
                core.log(f'[学习通] 补拉 device 失败: {exc}')

        # include_rest=True：state=0 的班也扫。教务排的课常常还没"激活"，
        # 早先过滤掉它们，结果老师发起签到这边一片空白。
        try:
            courses = self.api.courses(include_rest=True)
        except TypeError:                                  # 老签名兜底
            courses = self.api.courses()
        rest = [c for c in courses if c.get('inactive')]
        if rest:
            core.log(f'[学习通] 其中 {len(rest)} 门未激活（也一并扫签到）')
        # 合并手动加课：课程中心没收录、但老师在上课的课（如商益学"乐"）。
        for ec in self.extra_courses:
            cid = str(ec.get('courseId') or '')
            lid = str(ec.get('classId') or '')
            if not cid or not lid:
                continue
            # 课程中心已有就跳过，避免重扫
            if any(c['courseId'] == cid and c['classId'] == lid for c in courses):
                continue
            cpi = str(ec.get('cpi') or '')
            if not cpi:
                cpi = self._lookup_cpi(cid, lid)
            courses.append({
                'courseId': cid, 'classId': lid, 'cpi': cpi,
                'name': str(ec.get('name') or '') or f'{cid}',
                'teacher': '',
                'manual': True,
            })
        if not courses:
            return {'ok': False, 'courses': [], 'signs': [],
                    'error': '没有取到课程（登录可能已失效）'}

        signs: list[dict] = []
        for c in courses:
            try:
                acts = self.api.activities(c['courseId'], c['classId'], c['cpi'])
            except Exception as exc:                       # noqa: BLE001
                core.log(f'[学习通] 取活动失败 {c.get("name")}: {exc}')
                continue
            for a in acts:
                if a['type'] not in SIGN_ACTIVE_TYPES or not a['status']:
                    continue
                signs.append(self._describe(c, a))

        # 群聊里的签到通知并进同一份列表：普通签到、群聊签到对用户是同一件事，
        # 分成两个列表只会让人不知道该点哪个。去重按 activeId。
        have = {s['activeId'] for s in signs}
        for item in self._chat_signs():
            if item['activeId'] in have:
                continue
            have.add(item['activeId'])
            signs.append(item)

        with self._lock:
            self._cache = {c['courseId']: c for c in courses}
            self._last_scan = now
            self._last_signs = signs
        return self._snapshot()

    def _snapshot(self) -> dict:
        rows = list(self._cache.values())
        return {'ok': True, 'courses': rows,
                'inactive': sum(1 for c in rows if c.get('inactive')),
                'signs': list(getattr(self, '_last_signs', [])), 'error': ''}

    def _lookup_cpi(self, course_id: str, class_id: str) -> str:
        """手动加课没填 cpi 时，用 clazzperson 现查一次（结果缓存）。"""
        key = f'{course_id}|{class_id}'
        if key in self._cpi_cache:
            return self._cpi_cache[key]
        cpi = ''
        try:
            data = self.api._get(
                cx.MOOC_API + '/gas/clazzperson',
                {'courseid': course_id, 'clazzid': class_id, 'userid': self.api.uid,
                 'personid': '', 'view': 'json',
                 'fields': 'clazzid,popupagreement,personid'})
            d = self.api._json(data)
            rows = d.get('data') or []
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                cpi = str(rows[0].get('personid') or '')
        except Exception:                                   # noqa: BLE001
            cpi = ''
        self._cpi_cache[key] = cpi
        return cpi

    def _chat_signs(self, max_groups: int = 10, max_msgs: int = 15) -> list[dict]:
        """从班级群里捞签到通知。

        课程中心不收的班（教务后台直接排的）在活动列表里查不到，但群里会收到
        签到消息——course_helper 覆盖这块就是靠 IM（push/easemob.dart）。
        这里只做"刷新时顺手看一眼"，不是常驻监听。

        进来的活动和扫出来的走同一套 _describe，前端不用区分来源；
        只额外带一个 fromChat，用来在行上标一下"群聊"。
        """
        out: list[dict] = []
        # 前端 Web SDK 推上来的活动（唯一能拿到群消息的途径）
        for it in list(_CHAT_SIGNS):
            course = {'courseId': it.get('courseId') or '',
                      'classId': it.get('classId') or '',
                      'cpi': '', 'name': it.get('courseName') or '',
                      'teacher': '', 'manual': False, 'inactive': False}
            act = {'activeId': it['activeId'], 'name': it.get('title') or '签到',
                   'desc': it.get('desc') or '', 'url': it.get('url') or '',
                   'startTime': 0, 'endTime': 0, 'signType': '', 'status': 0}
            try:
                item = self._describe(course, act)
            except Exception as exc:                       # noqa: BLE001
                core.log(f'[群聊] 活动 {it["activeId"]} 解析失败：{exc}')
                continue
            item['fromChat'] = True
            out.append(item)
        if out:
            return out
        im = (self.api.store.get_user() or {}).get('imAccount') or {}
        user = str(im.get('userName') or '')
        pwd = str(im.get('password') or '')
        if not user or not pwd:
            return []
        try:
            import cxchat
            tok, err = cxchat.login(user, pwd)
            if err:
                core.log(f'[群聊] 登录失败：{err}')
                return []
            groups = cxchat.joined_groups(tok, user)
            for g in groups[:max_groups]:
                try:
                    msgs = cxchat.group_messages(tok, g['groupId'], limit=max_msgs)
                except Exception:                          # noqa: BLE001
                    continue
                for a in cxchat.activities_from_messages(msgs):
                    # 群里也发作业/讨论，只留签到
                    label = (a.get('atypeName') or '') + (a.get('title') or '') \
                        + (a.get('subTitle') or '')
                    if '签到' not in label:
                        continue
                    course = {'courseId': a.get('courseId') or '',
                              'classId': a.get('classId') or '',
                              'cpi': '', 'name': a.get('courseName') or '',
                              'teacher': '', 'manual': False, 'inactive': False}
                    act = {'activeId': a['activeId'],
                           'name': a.get('title') or '签到',
                           'desc': a.get('subTitle') or '',
                           'url': a.get('url') or '',
                           'startTime': 0, 'endTime': 0,
                           'signType': '', 'status': 0}
                    try:
                        item = self._describe(course, act)
                    except Exception as exc:               # noqa: BLE001
                        core.log(f'[群聊] 活动 {a["activeId"]} 解析失败：{exc}')
                        continue
                    item['fromChat'] = True
                    out.append(item)
        except Exception as exc:                           # noqa: BLE001
            core.log(f'[群聊] 拉取失败（不影响课程扫描）：{exc}')
        return out

    def _describe(self, course: dict, act: dict) -> dict:
        """把一个活动补成「前端能直接渲染」的样子。"""
        aid = act['activeId']
        info = {}
        try:
            info = self.api.active_info(aid)
        except Exception:                                  # noqa: BLE001
            pass

        sign_type = act.get('signType')
        if not sign_type and info.get('otherId') is not None:
            sign_type = cx._sign_type_from_index(cx._int(info.get('otherId')))
        sign_type = sign_type or 'normal'

        attend = {}
        try:
            attend = self.api.attend_info(aid)
        except Exception:                                  # noqa: BLE001
            pass
        status = cx._int(attend.get('status'), 0)

        need_captcha = cx._int(info.get('showVCode')) == 1
        need_face = cx._int(info.get('openCheckFaceFlag')) == 1
        need_photo = cx._int(info.get('ifphoto')) == 1
        # 只有位置签到会给这两个
        place = str(info.get('locationText') or '')
        rng = str(info.get('locationRange') or '')

        missing = []
        if status == 1:
            pass
        elif sign_type in NEEDS_TEXT:
            missing.append(NEEDS_TEXT[sign_type])
        if need_captcha and status != 1:
            missing.append('验证码')
        if need_face and status != 1:
            missing.append('人脸')

        # 截止时间：活动列表没给 endTime 的（实测商益学老位置签到是 0），
# 用 active_info 的 starttime + createPageTimeLong 推出来。推出来了
# 前端才能把已截止的剔掉，canAuto 也不会把过期签到算进去。
        end_ts = cx._int(act.get('endTime'), 0)
        if not end_ts:
            st0 = cx._int(info.get('starttime'), 0)
            dur = cx._int(info.get('createPageTimeLong'), 0)
            if st0 and dur:
                end_ts = st0 + dur
        expired = bool(end_ts) and time.time() * 1000 > end_ts

        # 位置签到只有在设置里配了坐标才算能自动——没坐标的话
        # 一键签到跑到它那儿必然停在「缺位置」，不如老实标成要输入。
        loc = self.default_location or {}
        has_loc = bool(str(loc.get('address') or '') and loc.get('lat') is not None
                       and loc.get('lng') is not None)
        can_auto = (status == 0 and sign_type in AUTO_TYPES
                    and not need_captcha and not expired
                    and (sign_type != 'location' or has_loc))
        if sign_type == 'location' and not has_loc and status == 0 and '位置' not in missing:
            missing.append('位置')

        return {
            'activeId': aid,
            'courseId': course['courseId'],
            'classId': course['classId'],
            'cpi': course['cpi'],
            'courseName': course['name'],
            'name': act['name'],
            'desc': act['desc'],
            'url': act['url'],
            'startTime': act['startTime'],
            'endTime': end_ts,
            'signType': sign_type,
            'status': status,
            'statusText': cx.STATUS_TEXT.get(status, '未签到' if status == 0 else ''),
            'signed': status != 0,
            # 要验证码的不算「能自动」：验证码是人机环节，一键自动签不了，
            # 放进 canAuto 只会让一键签到每次多一条必然失败，没意义。
            'canAuto': can_auto,
            'missing': missing,
            'needCaptcha': need_captcha,
            'needFace': need_face,
            'needPhoto': need_photo,
            'numberCount': cx._int(info.get('numberCount')),
            'place': place,
            'range': rng,
        }

    # -- 签到 --------------------------------------------------------------

    def sign(self, item: dict, *, pattern: str = '', code: str = '',
             enc: str = '', address: str = '', lat: float | None = None,
             lng: float | None = None, validate: str = '',
             photo_path: str = '', with_face: bool = False) -> dict:
        """签一个。item 是 scan() 里那一项，缺什么就传什么。

        返回 {ok, status, detail, needs}：
          needs 非空表示还差东西，前端拿它决定弹什么框。
        """
        aid = str(item.get('activeId') or '')
        cid = str(item.get('courseId') or '')
        url = str(item.get('url') or '')
        st = str(item.get('signType') or 'normal')

        if not aid or not cid:
            return {'ok': False, 'status': 'failed', 'detail': '缺少活动或课程 id',
                    'needs': []}

        with self._lock:
            if aid in self._signing:
                return {'ok': False, 'status': 'busy', 'detail': '这一条正在签',
                        'needs': []}
            self._signing.add(aid)
        try:
            return self._do_sign(item, st, aid, cid, url, pattern, code, enc,
                                 address, lat, lng, validate, photo_path, with_face)
        finally:
            with self._lock:
                self._signing.discard(aid)

    def _do_sign(self, item, st, aid, cid, url, pattern, code, enc,
                 address, lat, lng, validate, photo_path, with_face) -> dict:
        api = self.api

        # 人脸：只有明确要求时才去取，取不到就照常提交（服务端会拒再报）
        face_id = face_enc = ''
        if with_face or item.get('needFace'):
            try:
                face_id, ferr = cx.CxFace(api).face_id()
                if face_id:
                    face_enc, _ = cx.CxFace(api).face_enc(aid, face_id)
                elif ferr:
                    core.log(f'[学习通] 人脸不可用：{ferr}')
            except Exception as exc:                       # noqa: BLE001
                core.log(f'[学习通] 人脸流程异常：{exc}')

        # 位置：优先用调用方给的，其次用设置里配好的默认坐标
        addr = address or ''
        la, ln = lat, lng
        if not addr or la is None or ln is None:
            d = self.default_location or {}
            d_addr = str(d.get('address') or '')
            d_lat, d_lng = d.get('lat'), d.get('lng')
            if d_addr and d_lat is not None and d_lng is not None:
                addr, la, ln = d_addr, float(d_lat), float(d_lng)

        try:
            if st == 'normal':
                object_id = ''
                if item.get('needPhoto') and photo_path:
                    object_id, perr = cx.upload_image(api, photo_path, tag='photo')
                    if perr:
                        core.log(f'[学习通] 照片上传失败：{perr}')
                ok, msg = api.sign_normal(cid, aid, url, object_id=object_id,
                                          validate=validate)
            elif st == 'pattern':
                if not pattern:
                    return {'ok': False, 'status': 'need', 'detail': '要手势',
                            'needs': ['手势']}
                ok, msg = api.sign_code(cid, aid, pattern, url, validate=validate,
                                        address=addr,
                                        lat=la if addr else None,
                                        lng=ln if addr else None)
            elif st == 'code':
                if not code:
                    return {'ok': False, 'status': 'need', 'detail': '要签到码',
                            'needs': ['签到码']}
                ok, msg = api.sign_code(cid, aid, code, url, validate=validate,
                                        address=addr,
                                        lat=la if addr else None,
                                        lng=ln if addr else None)
            elif st == 'location':
                if not addr or la is None or ln is None:
                    return {'ok': False, 'status': 'need',
                            'detail': '要位置', 'needs': ['位置']}
                ok, msg = api.sign_location(cid, aid, addr, la, ln, url,
                                            validate=validate, face_id=face_id,
                                            face_enc=face_enc)
            elif st == 'qrcode':
                if not enc:
                    return {'ok': False, 'status': 'need', 'detail': '要扫二维码',
                            'needs': ['二维码']}
                ok, msg = api.sign_qrcode(cid, aid, enc, url, address=addr,
                                          lat=la, lng=ln, validate=validate,
                                          face_id=face_id, face_enc=face_enc)
            else:
                return {'ok': False, 'status': 'failed',
                        'detail': f'不认识的签到类型 {st}', 'needs': []}
        except Exception as exc:                           # noqa: BLE001
            return {'ok': False, 'status': 'failed',
                    'detail': core.head(str(exc), 120), 'needs': []}

        if ok:
            self._refresh_one(item)
            return {'ok': True, 'status': 'signed', 'detail': '签到成功', 'needs': []}

        # 要验证码：回执是 validate_<enc2>
        if msg.startswith('validate'):
            enc2 = msg.split('_', 1)[1] if '_' in msg else ''
            return {'ok': False, 'status': 'need', 'detail': '要验证码',
                    'needs': ['验证码'], 'enc2': enc2}
        return {'ok': False, 'status': 'failed', 'detail': msg, 'needs': []}

    def _refresh_one(self, item: dict) -> None:
        """签完把这一条的状态就地更新，前端不用等下一轮扫描。"""
        try:
            attend = self.api.attend_info(item['activeId'])
            status = cx._int(attend.get('status'), 0)
        except Exception:                                  # noqa: BLE001
            status = 1
        for row in getattr(self, '_last_signs', []):
            if row.get('activeId') == item.get('activeId'):
                row['status'] = status or 1
                row['signed'] = True
                row['canAuto'] = False
                row['missing'] = []
                row['statusText'] = cx.STATUS_TEXT.get(status, '已签到')

    def sign_all_auto(self, address: str = '', lat: float | None = None,
                      lng: float | None = None) -> dict:
        """把所有能自动签的一次签掉。返回 {done, failed, skipped}。

        调用方临时给的坐标会顶掉设置里的默认值——这样带坐标点「一键签到」
        时，位置签到也进得了 canAuto，不至于被跳过。
        """
        if address and lat is not None and lng is not None:
            self.default_location = {'address': address, 'lat': lat, 'lng': lng}
        data = self.scan()
        done, failed, skipped = [], [], []
        for item in data['signs']:
            if item['signed']:
                skipped.append({'activeId': item['activeId'],
                                'name': item['name'], 'why': '已签到'})
                continue
            if not item['canAuto']:
                skipped.append({'activeId': item['activeId'],
                                'name': item['name'],
                                'why': '缺' + '/'.join(item['missing'] or ['输入'])})
                continue
            res = self.sign(item, address=address, lat=lat, lng=lng)
            row = {'activeId': item['activeId'], 'name': item['name'],
                   'courseName': item['courseName'], 'detail': res['detail']}
            (done if res['ok'] else failed).append(row)
        return {'done': done, 'failed': failed, 'skipped': skipped}

    # -- 二维码 ------------------------------------------------------------

    def parse_qr(self, text: str) -> tuple[dict, str]:
        """解析扫到的二维码，返回 (参数, 错误)。

        学习通签到码的 URL 长这样：
          https://mobilelearn.chaoxing.com/widget/sign/e?id=<activeId>&c=<code>&enc=<enc>
        解析完还要拿 signDetail 复核一遍 code 没过期——原项目就这么做的，
        少了这步会把过期码提交上去换一个「签到失败，请重新扫描」。
        """
        import urllib.parse

        try:
            u = urllib.parse.urlparse(text.strip())
        except ValueError:
            return {}, '二维码内容无法解析'
        base = f'{u.scheme}://{u.netloc}{u.path}'
        if base != 'https://mobilelearn.chaoxing.com/widget/sign/e':
            return {}, '不是学习通签到码'
        q = urllib.parse.parse_qs(u.query)
        aid = (q.get('id') or [''])[0]
        enc = (q.get('enc') or [''])[0]
        code = (q.get('c') or [''])[0]
        if not aid or not enc:
            return {}, '签到码缺 id 或 enc'

        try:
            detail = self.api.sign_detail(aid, code)
        except Exception as exc:                           # noqa: BLE001
            return {}, f'复核签到码失败：{exc}'
        server_code = str(detail.get('signCode') or '')
        if server_code and code and code != server_code:
            return {}, '二维码已过期'
        return {'activeId': aid, 'enc': enc, 'code': code}, ''
