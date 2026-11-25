import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import cxapi as cx
import cxsign


class FakeStore:
    """群聊那条路要读 IM 账号。这里给个空 store，让它走「没有 IM 账号」的分支。"""

    def get_user(self):
        return {}


class FakeApi:
    """假学习通：只实现编排层用到的那几个方法。"""

    def __init__(self):
        self.cookie = 'k=v'
        self.uid = '100'
        self.name = '张三'
        self.school = 'HFUT'
        self.avatar = ''
        self.device = {'sc': 'sc-token', 'cid': 'cid-1'}
        self.signed = set()
        self.calls = []
        self.store = FakeStore()

    @property
    def ready(self):
        return True

    def profile(self):
        return {'ok': True, 'uid': self.uid, 'name': self.name}

    def courses(self):
        return [{'courseId': 'C1', 'classId': 'K1', 'cpi': 'P1',
                 'name': '高等数学', 'teacher': '李老师'}]

    def activities(self, course_id, class_id, cpi):
        return [
            {'activeId': 'A-normal', 'type': 2, 'name': '签到', 'desc': '普通',
             'startTime': 0, 'url': 'http://x/a', 'attendNum': 0,
             'status': True, 'signType': 'normal'},
            {'activeId': 'A-pattern', 'type': 2, 'name': '手势签到', 'desc': '',
             'startTime': 0, 'url': 'http://x/b', 'attendNum': 0,
             'status': True, 'signType': 'pattern'},
            {'activeId': 'A-code', 'type': 2, 'name': '签到码', 'desc': '',
             'startTime': 0, 'url': 'http://x/c', 'attendNum': 0,
             'status': True, 'signType': 'code'},
            {'activeId': 'A-loc', 'type': 2, 'name': '位置签到', 'desc': '',
             'startTime': 0, 'url': 'http://x/d', 'attendNum': 0,
             'status': True, 'signType': 'location'},
            {'activeId': 'A-qr', 'type': 2, 'name': '二维码签到', 'desc': '',
             'startTime': 0, 'url': 'http://x/e', 'attendNum': 0,
             'status': True, 'signType': 'qrcode'},
            # 非签到活动，应被过滤
            {'activeId': 'A-vote', 'type': 43, 'name': '投票', 'desc': '',
             'startTime': 0, 'url': 'http://x/f', 'attendNum': 0,
             'status': True, 'signType': None},
            # 已结束，应被过滤
            {'activeId': 'A-old', 'type': 2, 'name': '旧签到', 'desc': '',
             'startTime': 0, 'url': 'http://x/g', 'attendNum': 0,
             'status': False, 'signType': 'normal'},
        ]

    def active_info(self, aid):
        extra = {}
        if aid == 'A-normal':
            extra = {'showVCode': 1}          # 要验证码
        if aid == 'A-loc':
            extra = {'locationText': '翡翠湖校区', 'locationRange': '500'}
        return dict({'otherId': None}, **extra)

    def attend_info(self, aid):
        return {'status': 1 if aid in self.signed else 0}

    def sign_normal(self, cid, aid, url, object_id='', validate=''):
        self.calls.append(('normal', aid, validate))
        if not validate:
            return False, 'validate_enc2abc'
        self.signed.add(aid)
        return True, ''

    def sign_code(self, cid, aid, code, url, validate='', address='', lat=None, lng=None):
        self.calls.append(('code', aid, code))
        if code == 'bad':
            return False, '手势不正确'
        self.signed.add(aid)
        return True, ''

    def sign_location(self, cid, aid, addr, lat, lng, url, validate='', face_id='', face_enc=''):
        self.calls.append(('location', aid, addr, lat, lng))
        self.signed.add(aid)
        return True, ''

    def sign_qrcode(self, cid, aid, enc, url, **kw):
        self.calls.append(('qrcode', aid, enc))
        self.signed.add(aid)
        return True, ''

    def sign_detail(self, aid, code=''):
        return {'signCode': code or 'good'}

    def sign_token(self, fields):
        return cx.cc.md5(str(sorted(fields.items())))


fails = []
def check(cond, msg):
    if not cond:
        fails.append(msg)
        print('  [FAIL]', msg)


api = FakeApi()
s = cxsign.CxSigner(api)

print('--- 1. 扫描与分类 ---')
data = s.scan(force=True)
ids = [x['activeId'] for x in data['signs']]
print('发现签到:', ids)
check(data['ok'], 'scan 应成功')
check('A-vote' not in ids, '投票不该算签到')
check('A-old' not in ids, '已结束的不该出现')
check(len(ids) == 5, f'应有 5 个签到，实际 {len(ids)}')

by = {x['activeId']: x for x in data['signs']}
check(by['A-normal']['canAuto'] is False, '要验证码的普通签到不该算自动')
check('验证码' in by['A-normal']['missing'], '普通签到缺验证码')
check(by['A-pattern']['canAuto'] is False, '手势不该自动')
check(by['A-pattern']['missing'] == ['手势'], '手势 missing 不对')
check(by['A-code']['missing'] == ['签到码'], '签到码 missing 不对')
# 没配坐标时位置签到不算能自动，且标出缺位置
check(by['A-loc']['canAuto'] is False, '没配坐标时位置签到不该算自动')
check('位置' in by['A-loc']['missing'], '没配坐标时应标缺位置')
check(by['A-loc']['place'] == '翡翠湖校区', '位置描述没取到')
check(by['A-qr']['canAuto'] is False, '二维码不该自动')

print('--- 2. 缺输入时拒绝提交 ---')
r = s.sign(by['A-pattern'])
check(r['status'] == 'need', '手势缺输入应返回 need')
check(r['needs'] == ['手势'], 'needs 不对')
r = s.sign(by['A-code'])
check(r['needs'] == ['签到码'], '签到码 needs 不对')
r = s.sign(by['A-qr'])
check(r['needs'] == ['二维码'], '二维码 needs 不对')
r = s.sign(by['A-loc'])
check(r['needs'] == ['位置'], '位置 needs 不对')

print('--- 3. 给齐输入就签 ---')
r = s.sign(by['A-pattern'], pattern='12369')
check(r['ok'] and by['A-pattern']['signed'], f'手势签到应成功: {r}')

r = s.sign(by['A-code'], code='1234')
check(r['ok'], f'签到码应成功: {r}')

r = s.sign(by['A-loc'], address='翡翠湖校区', lat=31.86, lng=117.28)
check(r['ok'], f'位置签到应成功: {r}')
check(('location', 'A-loc', '翡翠湖校区', 31.86, 117.28) in api.calls,
      '位置参数没传对')

r = s.sign(by['A-qr'], enc='ENCTOKEN')
check(r['ok'], f'二维码签到应成功: {r}')

print('--- 4. 验证码回环 ---')
r = s.sign(by['A-normal'])
check(r['status'] == 'need' and '验证码' in r['needs'], f'应要求验证码: {r}')
check(r.get('enc2') == 'enc2abc', f'enc2 没解析出来: {r}')
r = s.sign(by['A-normal'], validate='VALID123')
check(r['ok'], f'带 validate 应成功: {r}')
check(('normal', 'A-normal', 'VALID123') in api.calls, 'validate 没传下去')

print('--- 5. 失败原因透传 ---')
api2 = FakeApi()
s2 = cxsign.CxSigner(api2)
d2 = s2.scan(force=True)
b2 = {x['activeId']: x for x in d2['signs']}
r = s2.sign(b2['A-code'], code='bad')
check(not r['ok'] and '手势不正确' in r['detail'], f'失败原因应透传: {r}')

print('--- 6. 一键自动 ---')
api3 = FakeApi()
s3 = cxsign.CxSigner(api3)
res = s3.sign_all_auto(address='翡翠湖校区', lat=31.86, lng=117.28)
print('done   :', [x['name'] for x in res['done']])
print('failed :', [x['name'] for x in res['failed']])
print('skipped:', [(x['name'], x['why']) for x in res['skipped']])
check(len(res['done']) == 1, f'只有位置签到能全自动，实际 {len(res["done"])}')
check(len(res['failed']) == 0, f'不该有失败（要验证码的应被跳过），实际 {len(res["failed"])}')
skipped_ids = {x['activeId'] for x in res['skipped']}
check({'A-pattern', 'A-code', 'A-qr', 'A-normal'} <= skipped_ids,
      f'待输入的应被跳过: {skipped_ids}')

print('--- 7. 二维码解析 ---')
ok, err = s.parse_qr('https://mobilelearn.chaoxing.com/widget/sign/e?id=A1&c=123&enc=E1')
check(ok.get('activeId') == 'A1' and ok.get('enc') == 'E1', f'解析不对: {ok} {err}')
bad, err = s.parse_qr('https://example.com/x')
check(not bad and '不是学习通' in err, f'非签到码应拒绝: {bad} {err}')

if fails:
    print(f'\n共 {len(fails)} 项失败')
    raise SystemExit(1)
print('\n全部通过')
