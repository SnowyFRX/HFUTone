"""yktapplogin.py —— 雨课堂 App 登录（方案 A）。

为什么需要它：动态二维码考勤（code 50070）是服务端按「客户端类型」判的。
浏览器身份一律被拒、要求你扫码。想以 App 身份发请求，光换请求头不够——
实测同一个 web sessionid 配上 App 头会直接 UNAUTHENTICATED（见 yktcore.APP_IDENTITY
的注释）。**凭据和身份必须是配套的**，所以要先用 App 的方式登录一次。

已经排除的路线（2026-11-23 实测，别再试）：
  1. 带 App 头去取网页二维码，赌服务端因此下发 App 凭据 —— 不通。
     拿到的仍然是 web sessionid，配 x-client: app 回 50000。
  2. App 端是否存在独立的取码接口 —— 不存在。探了 /api/v3/user/login/app/pre-info
     等 10 个候选路径，全是 404。所以"扫 App 的码"这件事本身没有 API 入口。
  3. 凭据体系由登录接口决定，跟取码时带什么头无关。

结论：拿 App 凭据只有这一条路 —— POST /api/v3/user/login/app。

App 登录的三个要素（来自 furryzirui/ykt_helper 的 rain_api.dart:377）：

    POST /api/v3/user/login/app
    {
      type: 1,                 // 1=账号密码  2=邮箱  3=短信验证码
      phoneNumber: 手机号,
      email: '',
      password: 密码,           // type=3 时留空，改用 code
      code: '',
      pushDeviceId: 微秒时间戳,
      ticket: <腾讯验证码 ticket>,
      rand:   <腾讯验证码 randstr>,
    }

其中 ticket / rand 必须来自官方人机验证（腾讯 TCaptcha，appId 2091064951），
伪造不了。所以这里的做法是：本地起一个页面，把 TCaptcha.js 嵌进去，
用户滑一下，页面把 {ticket, randstr} POST 回本地服务，我们再拿去登录。

凭据落盘规则（重要）：App 登录下发的 set-cookie 存进 Store 的 **app_cookie**，
和 web 的 cookie 分开。早先直接写 cookie 字段，一套 App 体系 sessionid 把
web 身份顶掉了，on-lesson 连浏览器头都变 50000 —— 那才是"登录成功却什么都
没变"的真正原因。现在两套互不覆盖，谁不可用只影响它自己。

登录成功 ≠ App 身份可用。必须再跑 verify_identity()（用 App 凭据 + App 头
打 on-lesson）确认，通过之后才由 server 切 identity。
"""
from __future__ import annotations

import json
import re
import time

import yktcore as core

# 腾讯验证码的 appId。和学习通那套（cxapi.tCaptchaAppId）是同一个。
CAPTCHA_APPID = '2091064951'

LOGIN_URL = core.SERVER + '/api/v3/user/login/app'
PROFILE_URL = core.SERVER + '/v/course_meta/user_info'

# 短信验证码登录的两步（来自 ykt_helper 的 rain_api.dart:365-395）：
#   POST /api/v3/user/code/send    {phoneNumber, email:'', ticket, rand}  发码
#   POST /api/v3/user/code/verify  {phoneNumber, email:'', code}          核码
# 注意 send 也要人机验证凭据：不同名字的路径（sms/send 之类）一律 404，
# 别按字面猜。
SMS_SEND_URL = core.SERVER + '/api/v3/user/code/send'
SMS_VERIFY_URL = core.SERVER + '/api/v3/user/code/verify'


def send_sms(phone: str, ticket: str, rand: str) -> tuple[bool, str]:
    """给手机号发登录验证码。返回 (成功?, 说明)。

    人机验证凭据是这一步的硬门槛：没有 ticket/rand 服务端不会发码。
    """
    p = (phone or '').strip()
    if not re.match(r'^1\d{10}$', p):
        return False, '请输入 11 位手机号'
    if not ticket or not rand:
        return False, '还没过人机验证'
    r = core.request('POST', SMS_SEND_URL, referer='', with_headers=True, timeout=25,
                     data={'phoneNumber': p, 'email': '', 'ticket': ticket, 'rand': rand},
                     extra_headers=dict(core.APP_HEADERS), tag='ykt-sms-send')
    code = core.jcode(r.body)
    msg = str(core.jstr(r.body, 'msg') or '')
    core.log(f'[短信登录] 发码 phone={p[:3]}**** code={code} msg={msg}')
    if code == 0:
        return True, ''
    return False, msg or f'发送失败（code={code}）'


def verify_sms(phone: str, code: str) -> tuple[bool, str]:
    """核验短信验证码。登录前必须先过这一步，login 的 code 字段才作数。"""
    p, c = (phone or '').strip(), (code or '').strip()
    if not c:
        return False, '还没填验证码'
    r = core.request('POST', SMS_VERIFY_URL, referer='', with_headers=True, timeout=25,
                     data={'phoneNumber': p, 'email': '', 'code': c},
                     extra_headers=dict(core.APP_HEADERS), tag='ykt-sms-verify')
    rc = core.jcode(r.body)
    msg = str(core.jstr(r.body, 'msg') or '')
    core.log(f'[短信登录] 核码 phone={p[:3]}**** code={rc} msg={msg}')
    if rc == 0:
        return True, ''
    return False, msg or f'验证码不正确（code={rc}）'


def captcha_html(callback_path: str = '/api/ykt/applogin/callback') -> str:
    """人机验证页。滑完把 ticket/randstr POST 回本地服务。

    TCaptcha 的回调里 r.ret === 0 表示验证通过，r.ticket / r.randstr 就是要的东西。
    这里不引任何框架，失败就把错误也回传，方便前端显示原因。

    除了回传 ticket，还会用 postMessage 告诉父窗口当前进行到哪一步
    （hfutone-ykt-captcha 的 phase）。父窗口据此立刻切回表单，不用等轮询——
    轮询只是兜底，单靠它做过一次事故：state 端点当时只注册在 POST 上，
    前端发的 GET 拿到 404，于是验证页做完什么都不会发生。
    """
    return f'''<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>雨课堂 · 人机验证</title>
<style>
  html,body {{ height:100%; margin:0; }}
  body {{
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    gap:16px; padding:24px; box-sizing:border-box;
    font:15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    background:#faf9f7; color:#1c1c1c;
  }}
  h1 {{ margin:0; font-size:17px; font-weight:600; }}
  p {{ margin:0; color:#6b6b6b; font-size:13px; text-align:center; max-width:34em; }}
  button {{
    appearance:none; font:inherit; font-size:14px; min-height:40px; padding:0 22px;
    border-radius:8px; border:1px solid #d8d5cf; background:#fff; color:#1c1c1c;
    cursor:pointer;
  }}
  button:hover {{ background:#f2f0ec; }}
  button:disabled {{ opacity:.5; cursor:default; }}
  .primary {{ background:#1c1c1c; color:#fff; border-color:#1c1c1c; }}
  .primary:hover {{ background:#000; }}
  .links {{ display:flex; gap:10px; }}
  .links button {{ min-height:34px; font-size:13px; padding:0 16px; }}
  #msg {{ min-height:20px; font-size:13px; }}
  .ok {{ color:#2f7d4f; }}
  .err {{ color:#c64545; }}
</style>
<script src="https://turing.captcha.qcloud.com/TCaptcha.js"></script>
</head>
<body>
  <h1>先过一下人机验证</h1>
  <p>雨课堂的 App 登录要求官方验证码，这一步没法跳过。验证通过后这里会自动继续。</p>
  <button id="go" class="primary" type="button">开始验证</button>
  <div class="links">
    <button id="back" type="button">回窗口</button>
  </div>
  <p id="msg"></p>

<script>
(function () {{
  var CALLBACK = {json.dumps(callback_path)};
  var APPID = {json.dumps(CAPTCHA_APPID)};
  var btn = document.getElementById('go');
  var back = document.getElementById('back');
  var msg = document.getElementById('msg');
  var busy = false;

  function show(text, cls) {{
    msg.textContent = text || '';
    msg.className = cls || '';
  }}

  /* 告诉父窗口进行到哪一步。父窗口只认 hfutone-ykt-captcha 这个 src。 */
  function notify(phase, extra) {{
    var payload = {{ src: 'hfutone-ykt-captcha', phase: phase }};
    if (extra) {{ for (var k in extra) {{ payload[k] = extra[k]; }} }}
    try {{ window.parent.postMessage(payload, '*'); }} catch (e) {{ /* 独立打开时没有父窗口 */ }}
  }}

  function post(payload) {{
    return fetch(CALLBACK, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload)
    }}).then(function (r) {{ return r.json().catch(function () {{ return {{}}; }}); }});
  }}

  function fail(text) {{
    show(text, 'err');
    notify('fail', {{ error: text }});
    btn.disabled = false;
    btn.textContent = '再试一次';
    busy = false;
  }}

  back.addEventListener('click', function () {{
    notify('closed');
  }});

  btn.addEventListener('click', function () {{
    if (busy) {{ return; }}
    if (typeof TencentCaptcha !== 'function') {{
      show('验证组件没加载出来（检查网络或代理），点「回窗口」先退出', 'err');
      notify('fail', {{ error: 'TCaptcha 脚本未加载' }});
      return;
    }}
    show('正在打开验证…');
    notify('opening');
    var cap = new TencentCaptcha(APPID, function (res) {{
      if (res && res.ret === 0 && res.ticket && res.randstr) {{
        busy = true;
        show('验证通过，正在提交…');
        notify('submitting');
        btn.disabled = true;
        btn.textContent = '请稍候…';
        post({{ ticket: res.ticket, rand: res.randstr }}).then(function (d) {{
          if (d && d.sent) {{
            // 短信登录第一步：码发出去了，这里就结束，回主窗口填验证码
            show('验证码已发送，回窗口填写即可', 'ok');
            notify('sent');
            btn.textContent = '已发送';
            busy = false;
            return;
          }}
          if (d && d.ok) {{
            show('登录成功：' + (d.name || '') + (d.school ? ' · ' + d.school : ''), 'ok');
            notify('ok', {{ name: d.name || '', school: d.school || '' }});
            btn.textContent = '已登录';
            busy = false;
            return;
          }}
          fail('登录失败：' + ((d && d.error) || '未知原因'));
        }}, function (e) {{
          fail('回传失败：' + e + '，点「回窗口」重试');
        }});
      }} else {{
        show(res && res.ret === 2 ? '你取消了验证' : '验证没通过，再试一次', 'err');
        /* 取消不算失败，但也要让父窗口知道人还卡在这一层 */
        notify('cancelled');
      }}
    }});
    cap.show();
  }});
}})();
</script>
</body></html>'''


def login(account: str, password: str, ticket: str, rand: str,
          kind: int = 1, store: core.Store | None = None) -> tuple[bool, str, dict]:
    """用账号密码 + 验证码凭据登录 App。

    kind: 1=账号密码  2=邮箱  3=短信验证码（此时 password 传验证码）
    返回 (成功?, 说明, 用户信息)。
    """
    acc = (account or '').strip()
    pwd = (password or '')
    if not acc or not pwd:
        return False, '账号和密码都要填', {}
    if not ticket or not rand:
        return False, '还没过人机验证', {}

    store = store or core.Store()
    is_email = '@' in acc

    # 短信登录：先核码，再拿 code 去登录。核不过就不必浪费一次登录请求
    # （服务端那边 ticket 也是一次性的，白跑一次就得重新过人机验证）。
    if kind == 3:
        ok_sms, why = verify_sms(acc, pwd)
        if not ok_sms:
            return False, why, {}

    body = {
        'type': kind if not is_email else 2,
        'phoneNumber': '' if is_email else acc,
        'email': acc if is_email else '',
        'password': '' if kind == 3 else pwd,
        'code': pwd if kind == 3 else '',
        # 官方 App 传的是微秒时间戳字符串，换设备就是换这个值
        'pushDeviceId': str(int(time.time() * 1_000_000)),
        'ticket': ticket,
        'rand': rand,
    }

    r = core.request('POST', LOGIN_URL, cookie=store.get_app_cookie(), referer='',
                     data=body, with_headers=True, timeout=30,
                     extra_headers=dict(core.APP_HEADERS), tag='ykt-applogin')
    code = core.jcode(r.body)
    msg = str(core.jstr(r.body, 'msg') or '')
    core.log(f'[App登录] code={code} msg={msg}')

    if code != 0:
        return False, msg or f'登录失败（code={code}）', {}

    # 关键：App 凭据存到 app_cookie，绝不碰 web 的 cookie。
    # 早先直接 store.save_cookie() 覆盖了 web 身份，而 App 体系的 sessionid
    # 对 browser 头不认，造成「App 登录成功 → 整个雨课堂 50000」。
    raw_sets = [ln for ln in (r.headers or '').replace('\r\n', '\n').split('\n')
                if ln.lower().startswith(('set-cookie:', 'set-auth:'))]
    for ln in raw_sets:
        core.log(f'[App登录] 响应头 {ln[:120]}')
    fresh = core.parse_set_cookies(r.headers, full=True)
    if fresh:
        store.save_app_cookie(fresh)
        core.log(f'[App登录] 已保存 App cookie（{len(fresh)} 字符）：{fresh[:24]}…')
    else:
        core.log('[App登录] 登录成功，但响应头里没有任何 set-cookie/set-auth')

    ok, prof = fetch_profile(store)
    if not ok:
        return True, '登录成功，但取用户信息失败（可能登录态没落盘）', {}
    return True, '', prof


def fetch_profile(store: core.Store | None = None) -> tuple[bool, dict]:
    """拿用户信息。走 App 身份，确认这套凭据在 App 头下是有效的。"""
    store = store or core.Store()
    r = core.request('GET', PROFILE_URL, cookie=store.get_app_cookie(), referer='',
                     timeout=20, extra_headers=dict(core.APP_HEADERS),
                     tag='ykt-appprofile')
    d = core.jdata(r.body) or {}
    prof = d.get('user_profile') if isinstance(d, dict) else None
    if not isinstance(prof, dict):
        return False, {}
    uid = str(prof.get('user_id') or prof.get('id') or '')
    user = {
        'userId': uid,
        'name': str(prof.get('name') or ''),
        'school': str(prof.get('school') or ''),
        'avatar': str(prof.get('avatar') or ''),
    }
    if uid:
        store.save_user(user)
        core.set_app_uid(uid)
    return bool(uid), user


def verify_identity(store: core.Store | None = None) -> dict:
    """登录后自检：这套 App 凭据在 App 身份下能不能拉课表。

    返回 {ok, code, msg, note}。这是判断「App 身份是否真的可用」的前置条件：
    如果 App 身份连 on-lesson 都过不了，那 50070 更没戏。
    """
    store = store or core.Store()
    ck = store.get_app_cookie()
    if not ck:
        return {'ok': False, 'code': 0, 'msg': 'empty',
                'note': '没有 App 凭据（登录响应里没有 set-cookie）'}
    url = core.SERVER + '/api/v3/classroom/on-lesson'
    r = core.request('GET', url, cookie=ck, referer='',
                     timeout=20, extra_headers=dict(core.APP_HEADERS),
                     tag='ykt-appverify')
    code = core.jcode(r.body)
    msg = str(core.jstr(r.body, 'msg') or '')
    if code == 0:
        return {'ok': True, 'code': 0, 'msg': 'OK',
                'note': 'App 身份可用，签到应该能免扫'}
    if code == 50000:
        return {'ok': False, 'code': code, 'msg': msg,
                'note': 'App 身份不被接受：这套凭据在 App 头下仍然 UNAUTHENTICATED'}
    return {'ok': False, 'code': code, 'msg': msg,
            'note': (msg or core.head(r.body, 100))}
