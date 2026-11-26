"""tests/test_autosign.py —— 方案三后端自动签到引擎与配置自检。"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'web'))

import autosign
import server

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print('  [FAIL]', msg)
    else:
        print('  [PASS]', msg)


# ---- 1. 设置读写与边界校验 ------------------------------------------------
print('--- 1. 自动签到设置读写与校验 ---')
tmp = Path(tempfile.mkdtemp())
server.CRED_DIR = tmp
server.SETTINGS_FILE = tmp / 'settings.json'

cfg = server.read_settings()
check(cfg['ykt']['autoSign'] is False, 'ykt.autoSign 默认应为 False')
check(cfg['autoSign']['pollIntervalSec'] == 180, 'pollIntervalSec 默认应为 180')
check(cfg['autoSign']['webhookUrl'] == '', 'webhookUrl 默认应为空')

cfg = server.write_settings({
    'ykt': {'autoSign': True},
    'cx': {'autoSign': True},
    'autoSign': {'pollIntervalSec': 10, 'webhookUrl': 'https://example.com/hook'},
})
check(cfg['ykt']['autoSign'] is True, 'ykt.autoSign 写入成功')
check(cfg['autoSign']['pollIntervalSec'] == 60, 'pollIntervalSec 下限钳制到 60')
check(cfg['autoSign']['webhookUrl'] == 'https://example.com/hook', 'webhookUrl 写入成功')

cfg = server.write_settings({'autoSign': {'pollIntervalSec': 9999, 'webhookUrl': 'file:///etc/passwd'}})
check(cfg['autoSign']['pollIntervalSec'] == 600, 'pollIntervalSec 上限钳制到 600')
check(cfg['autoSign']['webhookUrl'] == 'https://example.com/hook', '非法 scheme 不得覆盖原 webhookUrl')


# ---- 2. Webhook payload 构造 ---------------------------------------------
print('--- 2. Webhook 多渠道 payload 适配 ---')
_, p_ding = autosign._build_webhook_payload('https://oapi.dingtalk.com/robot/send?access_token=replay', '标题', '正文')
check(p_ding.get('msgtype') == 'text' and '标题' in p_ding['text']['content'], '钉钉格式正确')

_, p_wx = autosign._build_webhook_payload('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=replay', '标题', '正文')
check(p_wx.get('msgtype') == 'text', '企业微信格式正确')

_, p_fs = autosign._build_webhook_payload('https://open.feishu.cn/open-apis/bot/v2/hook/replay', '标题', '正文')
check(p_fs.get('msg_type') == 'text', '飞书格式正确')

_, p_sct = autosign._build_webhook_payload('https://sctapi.ftqq.com/replay.send', '标题', '正文')
check(p_sct.get('title') == '标题' and p_sct.get('desp') == '正文', 'Server酱格式正确')

_, p_bark = autosign._build_webhook_payload('https://api.day.app/replay', '标题', '正文')
check(p_bark.get('title') == '标题' and p_bark.get('body') == '正文', 'Bark 格式正确')


# ---- 3. IM 秒签 + 人工验证拦截 + 去重 ------------------------------------
print('--- 3. 学习通 IM 秒签与人工介入通知 ---')


class FakeCxSigner:
    def __init__(self):
        self.sign_calls = []
        self.described = {}
        self.sign_results = {}
        self.scan_items = []
        self.api = type('Api', (), {'active_info': lambda _self, aid: {}})()

    def _describe(self, course, act):
        aid = act['activeId']
        return dict(self.described[aid])

    def sign(self, item):
        aid = item['activeId']
        self.sign_calls.append(aid)
        return dict(self.sign_results.get(aid, {'ok': True, 'status': 'signed', 'detail': '签到成功', 'needs': []}))


class FakeCxState:
    def __init__(self, signer):
        self.logged_in = True
        self.signer = signer

    def sync_location(self):
        pass

    def scan(self, force=True):
        return {'ok': True, 'signs': list(self.signer.scan_items)}


class FakeYktStore:
    def load_checked(self):
        return {'L1': '21:55:00'}


class FakeYktApi:
    def __init__(self):
        self.store = FakeYktStore()
        self.calls = []
        self.responses = {}

    def signin_full(self, lid, cid, name, **kw):
        self.calls.append((lid, kw.get('sign_mode')))
        return dict(self.responses[lid])


class FakeYktState:
    def __init__(self, api):
        self.logged_in = True
        self.busy = False
        self.lock = threading.RLock()
        self.ykt = api
        self.lessons = []

    def refresh(self, force=False):
        pass


now_box = [10000.0]
webhooks = []
webhooks_done = threading.Event()


def fake_webhook(url, title, content, level='info'):
    webhooks.append((url, title, content, level))
    webhooks_done.set()
    return True, 'HTTP 200'


settings_state = {
    'cx': {'autoSign': True},
    'ykt': {'signMode': 'app', 'autoSign': True},
    'autoSign': {'pollIntervalSec': 180, 'webhookUrl': 'https://example.com/hook'},
}

cx_signer = FakeCxSigner()
cx_state = FakeCxState(cx_signer)
ykt_api = FakeYktApi()
ykt_state = FakeYktState(ykt_api)

engine = autosign.AutoSignEngine(
    get_settings=lambda: settings_state,
    get_cx=lambda: cx_state,
    get_ykt=lambda: ykt_state,
    webhook_sender=fake_webhook,
    clock=lambda: now_box[0],
    rng=lambda a, b: 1.0,
)

# 3a. 普通签到：IM 秒签直接成功
cx_signer.described['A1'] = {
    'activeId': 'A1', 'courseId': 'C1', 'courseName': '高等数学', 'name': '普通签到',
    'signType': 'normal', 'signed': False, 'canAuto': True, 'missing': [],
    'needCaptcha': False, 'endTime': 0,
}
r1 = engine.process_im_item({'activeId': 'A1', 'courseId': 'C1', 'courseName': '高等数学'}, realtime=True)
check(r1 and r1['status'] == 'signed', 'IM 秒签普通签到成功')
check(cx_signer.sign_calls == ['A1'], '仅调用了一次 sign')

# 重复 IM 推送不重复签
r1_dup = engine.process_im_item({'activeId': 'A1', 'courseId': 'C1', 'courseName': '高等数学'}, realtime=True)
check(r1_dup and r1_dup['status'] == 'already', '重复 IM 活动自动去重')
check(cx_signer.sign_calls == ['A1'], '去重后未再次提交')

# 3b. 带验证码（showVCode=1）活动：绝不绕过，直接报 need_action 通知人工
cx_signer.described['A_CAP'] = {
    'activeId': 'A_CAP', 'courseId': 'C2', 'courseName': '大学物理', 'name': '位置签到',
    'signType': 'location', 'signed': False, 'canAuto': False, 'missing': ['验证码'],
    'needCaptcha': True, 'endTime': 0,
}
r_cap = engine.process_im_item({'activeId': 'A_CAP', 'courseId': 'C2', 'courseName': '大学物理'}, realtime=True)
check(r_cap and r_cap['status'] == 'need_action', '验证码活动正确拦截为 need_action')
check('A_CAP' not in cx_signer.sign_calls, '验证码活动未尝试绕过提交')

# 3c. 服务端动态返回 validate_ 验证码要求：立即停止自动重试
cx_signer.described['A_DYN'] = {
    'activeId': 'A_DYN', 'courseId': 'C3', 'courseName': '线性代数', 'name': '普通签到',
    'signType': 'normal', 'signed': False, 'canAuto': True, 'missing': [],
    'needCaptcha': False, 'endTime': 0,
}
cx_signer.sign_results['A_DYN'] = {
    'ok': False, 'status': 'need', 'detail': '要验证码', 'needs': ['验证码'],
}
r_dyn1 = engine.process_im_item({'activeId': 'A_DYN', 'courseId': 'C3'}, realtime=True)
r_dyn2 = engine.process_im_item({'activeId': 'A_DYN', 'courseId': 'C3'}, realtime=True)
check(r_dyn1 and r_dyn1['status'] == 'need_action', '动态验证码回执转为 need_action')
check(r_dyn2 and r_dyn2['status'] == 'exhausted', '动态验证码后不再自动重试')
check(cx_signer.sign_calls.count('A_DYN') == 1, '动态验证码活动只尝试一次即交由人工')

snap_mid = engine.status_payload()
check(snap_mid['stats']['needAction'] == 2, f'此时待人工应为 2，实际 {snap_mid["stats"]["needAction"]}')
cap_ev = next((e for e in snap_mid['events'] if e.get('refId') == 'A_CAP'), None)
check(bool(cap_ev and cap_ev.get('item') and cap_ev['item'].get('needCaptcha')),
      'need_action 事件应附带 item 供前端直接弹出人工验证')

# 手动完成 A_CAP 签到后，对应 need_action 应立即翻为 signed 并减少待人工计数
resolved_n = engine.mark_resolved('cx', 'A_CAP', detail='手动签到成功')
check(resolved_n == 1, 'mark_resolved 成功同步翻正 A_CAP 记录')
check(engine.status_payload()['stats']['needAction'] == 1, '手动签完 A_CAP 后待人工降为 1')


# ---- 4. 低频兜底轮询（学习通 + 雨课堂） ----------------------------------
print('--- 4. 学习通与雨课堂低频轮询兜底 ---')
cx_signer.scan_items = [
    {
        'activeId': 'A_POLL', 'courseId': 'C4', 'courseName': '数据结构', 'name': '位置签到',
        'signType': 'location', 'signed': False, 'canAuto': True, 'missing': [],
        'needCaptcha': False, 'endTime': 0,
    },
]
pcx = engine.poll_cx_once()
check(pcx.get('ok') and pcx.get('signed') == 1, '学习通兜底轮询成功补签 1 条')

ykt_state.lessons = [
    {'lessonId': 'L1', 'classroomId': 'R1', 'name': '操作系统', 'checkedAt': '', 'signinStatus': 0},
    {'lessonId': 'L2', 'classroomId': 'R2', 'name': '计算机网络', 'checkedAt': '', 'signinStatus': 0},
    {'lessonId': 'L3', 'classroomId': 'R3', 'name': '离散数学', 'checkedAt': '', 'signinStatus': 0},
]
ykt_api.responses = {
    'L1': {'ok': True, 'status': 'checked', 'detail': '已进班且老师已发起签到'},
    'L2': {'ok': True, 'status': 'joined', 'detail': '进班成功，老师还没发起签到'},
    'L3': {'ok': False, 'status': 'need_scan', 'detail': '动态二维码签到：需手机扫码'},
}
pykt = engine.poll_ykt_once()
check(pykt.get('ok') and pykt.get('signed') == 1 and pykt.get('needAction') == 1,
      f'雨课堂轮询判定正确：{pykt}')
check(ykt_state.lessons[0]['signinStatus'] == 1, '雨课堂签到成功后同步回写 lesson 状态')

# 冷却期内再次轮询不重复打 L1/L2/L3
calls_before = len(ykt_api.calls)
engine.poll_ykt_once()
check(len(ykt_api.calls) == calls_before, '冷却期内不重复提交雨课堂签到')

# 推进时间超过 joined 冷却后，L2 会复查（老师此时发起了签到）
now_box[0] += autosign.YKT_JOINED_COOLDOWN + 5
ykt_api.responses['L2'] = {'ok': True, 'status': 'checked', 'detail': '签到成功'}
pykt2 = engine.poll_ykt_once()
check(pykt2.get('signed') == 1, 'joined 冷却到期后成功补签 L2')

# 等待异步 Webhook 线程落盘
time.sleep(0.15)
snap = engine.status_payload()
check(snap['stats']['signed'] == 4, f'统计已签应为 4，实际 {snap["stats"]["signed"]}')
# A_CAP 已手动消掉，A_DYN 在轮询同步中已消掉，仅剩雨课堂 L3 仍为待人工
check(snap['stats']['needAction'] == 1, f'统计当前待人工应为 1，实际 {snap["stats"]["needAction"]}')
check(len(webhooks) >= 4, f'Webhook 异步推送触发次数正常（{len(webhooks)} 次）')

if fails:
    print(f'\n共 {len(fails)} 项失败')
    raise SystemExit(1)
print('\n自动签到引擎全部测试通过')
