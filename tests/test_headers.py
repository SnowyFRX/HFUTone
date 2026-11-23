"""请求头组装的自检。

核心结论（2026-11-23 实测长江雨课堂）：
  同一个 sessionid，三种身份拉 /api/v3/classroom/on-lesson
    App 头    -> {"code":50000,"msg":"UNAUTHENTICATED"}
    浏览器头   -> {"code":0,"msg":"OK"}
    裸 curl   -> {"code":50000,"msg":"UNAUTHENTICATED"}
  所以默认必须是浏览器身份（APP_IDENTITY=False），App 头只在 app_checkin
  那一次显式借用。这个测试守住三件事：
    1. 默认请求是浏览器身份，不能自己变成 App
    2. 学习通请求绝不能被雨课堂的头污染
    3. 显式传 App 头时（app_checkin）确实带上了，且没有 Referer
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import yktcore

captured = []
_orig_run = yktcore.subprocess.run


class FakeProc:
    returncode = 0
    stdout = '{"code":0}\n---YKTSTATUS---200'
    stderr = ''


def fake_run(args, **kw):
    captured.append(args)
    return FakeProc()


yktcore.subprocess.run = fake_run
yktcore.curl_path = lambda force=False: 'curl'
yktcore.ca_cert_arg = lambda: []

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print('  [FAIL]', msg)


def headers_of(args):
    ua, h = '', {}
    for i, a in enumerate(args):
        if a == '-H' and i + 1 < len(args):
            kv = args[i + 1]
            if ':' in kv:
                k, v = kv.split(':', 1)
                k, v = k.strip().lower(), v.strip()
                if k == 'user-agent':
                    ua = v
                h[k] = v
    return ua, h


YURL = 'https://changjiang.yuketang.cn/api/v3/classroom/on-lesson'

print('--- 1. 默认必须是浏览器身份 ---')
check(yktcore.APP_IDENTITY is False, 'APP_IDENTITY 默认必须是 False')
captured.clear()
yktcore.request('GET', YURL, cookie='sessionid=x', tag='t')
ua, h = headers_of(captured[-1])
print('  UA:', ua[:60])
check('Mozilla' in ua, f'默认 UA 应是浏览器，实际 {ua!r}')
check('x-client' not in h, '默认不该带 x-client（会把会话弄成 UNAUTHENTICATED）')
check('xtua' not in h, '默认不该带 xtua')

print('--- 2. 显式传 App 头时确实生效（app_checkin 走这条）---')
captured.clear()
yktcore.request('POST', 'https://changjiang.yuketang.cn/api/v3/lesson/checkin',
                cookie='sessionid=x', referer='', data={'source': 21},
                extra_headers=dict(yktcore.APP_HEADERS), tag='t')
ua, h = headers_of(captured[-1])
print('  UA:', ua)
check(ua == 'Android', f'显式 App UA 应为 Android，实际 {ua!r}')
check(h.get('x-client') == 'app', 'x-client 应为 app')
check(h.get('xtua', '').startswith('client=app'), 'xtua 不对')
check(h.get('buildnumber') == '1610', 'buildnumber 不对')
check('referer' not in h, 'App 身份不该带 Referer')

print('--- 3. 学习通请求不能被污染 ---')
captured.clear()
cx_headers = {'User-Agent': 'Dalvik/2.1.0 (Linux; U; Android 16) (schild:abc)',
              'content-type': 'application/x-www-form-urlencoded'}
yktcore.request('POST', 'https://mobilelearn.chaoxing.com/pptSign/stuSignajax',
                form={'a': '1'}, extra_headers=cx_headers, tag='t')
ua, h = headers_of(captured[-1])
print('  UA:', ua[:60])
check('schild' in ua, '学习通 UA 被覆盖了')
check('x-client' not in h, '学习通不该带 x-client')
check('xtua' not in h, '学习通不该带 xtua')
check('x-uid' not in h, '学习通不该带 x-uid')

print('--- 4. 打开 APP_IDENTITY 后雨课堂才自动带 App 头 ---')
yktcore.APP_IDENTITY = True
yktcore.set_app_uid('12345')
captured.clear()
yktcore.request('GET', YURL, cookie='sessionid=x', tag='t')
ua, h = headers_of(captured[-1])
check(ua == 'Android', f'开关打开后 UA 应为 Android，实际 {ua!r}')
check(h.get('x-uid') == '12345', f'x-uid 应为 12345，实际 {h.get("x-uid")}')

print('--- 5. 开关打开也不该污染学习通 ---')
captured.clear()
yktcore.request('GET', 'https://mobilelearn.chaoxing.com/x', extra_headers=cx_headers, tag='t')
ua, h = headers_of(captured[-1])
check('x-client' not in h, '开关打开后学习通仍不该带 x-client')

yktcore.APP_IDENTITY = False
yktcore.set_app_uid('')
yktcore.subprocess.run = _orig_run

if fails:
    print(f'\n共 {len(fails)} 项失败')
    raise SystemExit(1)
print('\n请求头全部符合预期')
