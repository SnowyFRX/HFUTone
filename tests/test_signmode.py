import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import yktapi
import yktcore


def resp(code, msg='', lesson_token=''):
    body = '{"code":%d,"msg":"%s","data":{"lessonToken":"%s"}}' % (code, msg, lesson_token)
    return yktcore.Resp(200, body, '')


class FakeStore:
    """cookie 走 property，必须从 store 给。"""

    def get_cookie(self):
        return 'k=v'

    def save_cookie(self, c):
        return True

    def get_user(self):
        return {}

    def mark_checked(self, *a):
        return True

    def load_checked(self):
        return {}


class FakeYkt(yktapi.Ykt):
    """只测分支选择，网络全部打桩。"""

    def __init__(self):
        self.store = FakeStore()
        self._creds = {}
        self.calls = []

    def app_checkin(self, lesson_id, user_id=None):
        self.calls.append('app')
        return resp(0, '签到成功'), yktapi.Cred(lesson_id, 'bearer', 'tok')

    def scan(self, qr_url):
        self.calls.append('scan:' + qr_url)
        if 'bad' in qr_url:
            return resp(40001, '二维码无效'), yktapi.Cred()
        return resp(0, 'ok', 'tok'), yktapi.Cred('L1', 'bearer', 'tok')

    def checkin(self, lesson_id, source=21, extra_headers=None):
        self.calls.append('plain')
        return resp(0, '签到成功'), yktapi.Cred(lesson_id, 'b', 't')

    def rollcall(self, lesson_id, classroom_id):
        self.calls.append('rollcall')
        return 1, 'x', False

    def _signed_on_server(self, lesson_id):
        return False


fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print('  [FAIL]', msg)


def run(mode, qr=''):
    y = FakeYkt()
    r = y.signin_full('L1', 'C1', '测试课', sign_mode=mode, qr_url=qr)
    return r, y.calls


print('--- mode=app（不给码）---')
r, calls = run('app')
print('  channel=%s calls=%s status=%s' % (r['channel'], calls, r['status']))
check(calls[0] == 'app', 'app 模式应先走 app')
check(r['channel'] == 'app', 'channel 应为 app')

print('--- mode=scan（给码）---')
r, calls = run('scan', 'https://x/y?enc=1')
print('  channel=%s calls=%s status=%s' % (r['channel'], calls, r['status']))
check(calls[0].startswith('scan:'), 'scan 模式应先走 scan')
check('app' not in calls, 'scan 模式成功后不该再走 app')
check(r['channel'] == 'scan', 'channel 应为 scan')

print('--- mode=scan（不给码）应直接要码 ---')
r, calls = run('scan', '')
print('  channel=%s calls=%s status=%s detail=%s' % (
    r['channel'], calls, r['status'], r['detail']))
check(r['status'] == 'need_scan', f'应报 need_scan，实际 {r["status"]}')
check(not calls, f'不该发任何请求，实际 {calls}')

print('--- mode=both（给好码）先 scan 成功就不走 app ---')
r, calls = run('both', 'https://x/y?enc=1')
print('  channel=%s calls=%s' % (r['channel'], calls))
check(calls[0].startswith('scan:'), 'both 应先 scan')
check('app' not in calls, f'scan 成功不该再走 app，实际 {calls}')

print('--- mode=both（坏码）回退 app ---')
r, calls = run('both', 'https://x/bad')
print('  channel=%s calls=%s status=%s' % (r['channel'], calls, r['status']))
check(calls[0].startswith('scan:'), 'both 应先 scan')
check('app' in calls, f'scan 失败应回退 app，实际 {calls}')
check(r['channel'] == 'app', '回退后 channel 应为 app')

print('--- mode=both（不给码）直接 app ---')
r, calls = run('both', '')
print('  channel=%s calls=%s' % (r['channel'], calls))
check(calls == ['app', 'rollcall'], f'没码时只走 app，实际 {calls}')

print('--- 非法 mode 当 both 处理 ---')
r, calls = run('nonsense', '')
check(calls[0] == 'app', f'非法 mode 应退化为 both→app，实际 {calls}')

if fails:
    print(f'\n共 {len(fails)} 项失败')
    raise SystemExit(1)
print('\n三种签到通道全部通过')
