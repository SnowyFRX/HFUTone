"""tests/test_cxim.py —— 纯 Python 环信 MSYNC Protobuf 编解码与自适应降级自检。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

import autosign
import cxim

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print('  [FAIL]', msg)
    else:
        print('  [PASS]', msg)


print('--- 1. JID / Provision / MSync 编解码 ---')
jid_raw = cxim.encode_jid(app_key='cx-dev#cxstudy', name='10086', domain='easemob.com', client_resource='webim_1')
jid_dec = cxim.decode_jid(jid_raw)
check(jid_dec == {'appKey': 'cx-dev#cxstudy', 'name': '10086', 'domain': 'easemob.com', 'clientResource': 'webim_1'},
      'JID 编解码一致')

prov_msync, res = cxim.encode_provision('10086', 'replay_token_val')
ms = cxim.decode_msync(prov_msync)
check(ms['command'] == 3 and len(ms['payload']) > 10, 'Provision MSync command=3 且含 payload')
check(res.startswith('webim_'), 'Provision resource 格式正确')

# 构造服务端返回的 Provision 回执（status.errorCode=0, resource=webim_99）
prov_dl_raw = cxim._pb_bytes(8, cxim._pb_varint(1, 0)) + cxim._pb_str(13, 'webim_99')
prov_dl = cxim.decode_provision_dl(prov_dl_raw)
check(prov_dl['status']['errorCode'] == 0 and prov_dl['resource'] == 'webim_99', 'Provision 回执解码正确')

print('--- 2. CommNotice / CommUnreadDL / CommSyncDL 解码 ---')
notice_raw = cxim._pb_bytes(1, cxim.encode_jid(name='998877', domain='conference.easemob.com'))
q_notice = cxim.decode_notice(notice_raw)
check(q_notice and q_notice['name'] == '998877' and 'conference' in q_notice['domain'], 'CommNotice 队列解码正确')

unread_raw = cxim._pb_bytes(
    2,
    cxim._pb_bytes(1, cxim.encode_jid(name='112233', domain='conference.easemob.com'))
    + cxim._pb_varint(2, 3),
)
queues = cxim.decode_unread_dl(unread_raw)
check(len(queues) == 1 and queues[0]['name'] == '112233' and queues[0]['n'] == 3,
      'CommUnreadDL 未读队列与未读数 n 解码正确')

print('--- 3. Meta -> MessageBody -> 签到活动提取 ---')
att_json = json.dumps({
    'attachmentType': 15,
    'att_chat_course': {
        'aid': 7000182999999,
        'atype': 2,
        'atypeName': '位置签到',
        'title': '签到',
        'url': 'https://mobilelearn.chaoxing.com/newsign/preSign?activePrimaryId=7000182999999',
        'courseInfo': {'courseid': 269364950, 'classid': 160114240, 'coursename': '高等数学'},
    },
}, ensure_ascii=False)
kv_att = cxim._pb_str(1, 'attachment') + cxim._pb_varint(2, 8) + cxim._pb_str(6, att_json)
msg_body = cxim._pb_varint(1, 2) + cxim._pb_bytes(5, kv_att)
meta_sign = cxim._pb_varint(1, 987654321) + cxim._pb_varint(5, 1) + cxim._pb_bytes(6, msg_body)
act = cxim.decode_meta_to_activity(meta_sign)
check(act is not None and act['activeId'] == '7000182999999', '签到 Meta 解码出 activeId')
check(act['courseId'] == '269364950' and act['courseName'] == '高等数学', '课程信息解码完整')
check(act['msgId'] == '987654321', '消息 ID 提取正确')

# 自定义标题签到（标题不含“签到”字样但 atype=2/74/54）必须保留
custom_json = json.dumps({
    'att_chat_course': {
        'aid': 7000199999999, 'atype': 2, 'atypeName': '', 'title': '第三周课堂考勤',
        'courseInfo': {'courseid': 10, 'classid': 20, 'coursename': '大学物理'},
    },
}, ensure_ascii=False)
kv_custom = cxim._pb_str(1, 'attachment') + cxim._pb_varint(2, 8) + cxim._pb_str(6, custom_json)
meta_custom = cxim._pb_varint(1, 222) + cxim._pb_varint(5, 1) + cxim._pb_bytes(6, cxim._pb_bytes(5, kv_custom))
act_custom = cxim.decode_meta_to_activity(meta_custom)
check(act_custom is not None and act_custom['activeId'] == '7000199999999',
      '自定义标题但 atype=2 的签到活动被正确识别')

# 非签到消息（作业通知）必须过滤
hw_json = json.dumps({
    'att_chat_course': {
        'aid': 8888, 'atype': 42, 'atypeName': '作业', 'title': '第一次作业',
        'courseInfo': {'courseid': 1, 'classid': 2, 'coursename': '英语'},
    },
}, ensure_ascii=False)
kv_hw = cxim._pb_str(1, 'attachment') + cxim._pb_varint(2, 8) + cxim._pb_str(6, hw_json)
meta_hw = cxim._pb_varint(1, 111) + cxim._pb_varint(5, 1) + cxim._pb_bytes(6, cxim._pb_bytes(5, kv_hw))
check(cxim.decode_meta_to_activity(meta_hw) is None, '非签到活动（作业）被正确过滤')

# CommSyncDL 封包测试：省略 isLast(field 7) 时必须默认 False，避免漏掉下一页和未读游标 ACK
sync_dl_not_last_raw = (
    cxim._pb_bytes(1, cxim._pb_varint(1, 0))
    + cxim._pb_bytes(4, meta_sign)
    + cxim._pb_varint(5, 555666)
    + cxim._pb_bytes(6, cxim.encode_jid(name='998877', domain='conference.easemob.com'))
)
sdl_not_last = cxim.decode_sync_dl(sync_dl_not_last_raw)
check(sdl_not_last['isLast'] is False and sdl_not_last['nextKey'] == 555666,
      'CommSyncDL 省略 isLast 时默认 False（符合 Protobuf 语义）')

sync_dl_raw = sync_dl_not_last_raw + cxim._pb_varint(7, 1)
sdl = cxim.decode_sync_dl(sync_dl_raw)
check(len(sdl['metas']) == 1 and sdl['nextKey'] == 555666 and sdl['isLast'] is True, 'CommSyncDL 显式 isLast=True 解码正确')

print('--- 4. 实时 CommNotice 非阻塞与 nextKey ACK 回归测试 ---')
import base64


def _sockjs_a(pb_bytes: bytes) -> str:
    return 'a' + json.dumps([base64.b64encode(pb_bytes).decode('ascii')])


class _FakeWS:
    """模拟环信服务端：先插一条无 queue 的 rebuild 回执，再推实时 CommNotice，再验 nextKey ACK。"""

    def __init__(self, host, port, path, timeout=10.0):
        self.sent: list[dict] = []
        self.inbox: list[str] = ['o']
        self._closed = False

    def connect(self):
        pass

    def send_text(self, text: str):
        arr = json.loads(text)
        raw = base64.b64decode(arr[0])
        ms = cxim.decode_msync(raw)
        cmd, pl = ms['command'], ms['payload']
        self.sent.append({'cmd': cmd, 'payload': pl})
        if cmd == 3:
            prov_dl = cxim._pb_bytes(8, cxim._pb_varint(1, 0)) + cxim._pb_str(13, 'webim_fake')
            self.inbox.append(_sockjs_a(cxim.encode_msync(3, prov_dl)))
        elif cmd == 1:
            # 首次 UNREAD 返回一个旧队列
            u_pl = cxim._pb_bytes(
                2,
                cxim._pb_bytes(1, cxim.encode_jid(name='old_q', domain=''))
                + cxim._pb_varint(2, 1),
            )
            self.inbox.append(_sockjs_a(cxim.encode_msync(1, u_pl)))
        elif cmd == 0:
            # 区分 rebuild（field 1=Meta）与 queue sync（field 3=JID, field 2=key）
            fields = cxim._pb_parse(pl)
            q_bytes = next((v for f, wt, v in fields if f == 3 and wt == 2), b'')
            key_val = next((int(v) for f, wt, v in fields if f == 2 and wt == 0), 0)
            if not q_bytes:
                # rebuild 回执：queue=None, isLast 省略（旧代码在此会干扰队列并导致死锁）
                self.inbox.append(_sockjs_a(cxim.encode_msync(0, cxim._pb_bytes(1, cxim._pb_varint(1, 0)))))
                # 紧接着服务端推一条实时 CommNotice（群聊有新签到）
                self.inbox.append(_sockjs_a(cxim.encode_msync(2, notice_raw)))
            else:
                q_dec = cxim.decode_jid(q_bytes)
                if q_dec['name'] == '998877':
                    if key_val == 0:
                        # 第一页返回签到 Meta，省略 isLast（False），带 nextKey=555666
                        self.inbox.append(_sockjs_a(cxim.encode_msync(0, sync_dl_not_last_raw)))
                    else:
                        # 收到客户端带 key=555666 的 ACK 后返回 isLast=True
                        ack_dl = (
                            cxim._pb_bytes(1, cxim._pb_varint(1, 0))
                            + cxim._pb_bytes(6, q_bytes)
                            + cxim._pb_varint(7, 1)
                        )
                        self.inbox.append(_sockjs_a(cxim.encode_msync(0, ack_dl)))
                        self._closed = True
                else:
                    # old_q 故意不回包（模拟最坏丢包/未匹配场景），验证不能阻塞 998877 的实时推送
                    pass

    def recv_text(self):
        if self.inbox:
            return self.inbox.pop(0)
        if self._closed:
            raise ConnectionError('done')
        return None

    def close(self):
        self._closed = True


received_realtime: list[tuple[list[dict], bool]] = []
fake_ws_holder: list[_FakeWS] = []
orig_ws = cxim.SimpleWebSocket


def _ws_factory(*args, **kwargs):
    inst = _FakeWS(*args, **kwargs)
    fake_ws_holder.append(inst)
    return inst


cxim.SimpleWebSocket = _ws_factory
try:
    client = cxim.BackendIMClient(
        get_cx_state=lambda: type('_Cx', (), {'logged_in': True})(),
        on_activities=lambda acts, rt: received_realtime.append((acts, rt)),
    )
    try:
        client._session_once('10086', 'replay_tok', [])
    except ConnectionError:
        pass
finally:
    cxim.SimpleWebSocket = orig_ws

check(
    len(received_realtime) == 1
    and received_realtime[0][1] is True
    and received_realtime[0][0][0]['activeId'] == '7000182999999',
    '旧队列未回包 + rebuild 回执交错时，实时 CommNotice 仍能秒级拉取并分发签到事件',
)
sent_ack_keys = []
if fake_ws_holder:
    for pkt in fake_ws_holder[0].sent:
        if pkt['cmd'] == 0:
            fs = cxim._pb_parse(pkt['payload'])
            kv = next((int(v) for f, wt, v in fs if f == 2 and wt == 0), 0)
            if kv:
                sent_ack_keys.append(kv)
check(555666 in sent_ack_keys, 'isLast=False 时自动回发 CommSyncUL(key=nextKey) 推进服务端游标')

print('--- 5. AutoSignEngine IM 离线自适应降级 ---')
im_online = [False]
eng = autosign.AutoSignEngine(
    get_settings=lambda: {'cx': {'autoSign': True}, 'ykt': {'autoSign': False}, 'autoSign': {'pollIntervalSec': 180}},
    get_cx=lambda: None,
    get_ykt=lambda: None,
    im_status_fn=lambda: {'connected': im_online[0]},
)
check(eng.status_payload()['effectiveCxPollSec'] == 45, 'IM 离线时兜底间隔自动收紧为 45s')
im_online[0] = True
check(eng.status_payload()['effectiveCxPollSec'] == 180, 'IM 在线时兜底间隔恢复为配置的 180s')

if fails:
    print(f'\n共 {len(fails)} 项失败')
    raise SystemExit(1)
print('\n后端环信 IM 客户端测试全部通过')
