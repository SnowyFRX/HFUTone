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

unread_raw = cxim._pb_bytes(2, cxim._pb_bytes(1, cxim.encode_jid(name='112233', domain='conference.easemob.com')))
queues = cxim.decode_unread_dl(unread_raw)
check(len(queues) == 1 and queues[0]['name'] == '112233', 'CommUnreadDL 未读队列解码正确')

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

# 非签到消息（作业通知）必须过滤
hw_json = json.dumps({
    'att_chat_course': {
        'aid': 8888, 'atypeName': '作业', 'title': '第一次作业',
        'courseInfo': {'courseid': 1, 'classid': 2, 'coursename': '英语'},
    },
}, ensure_ascii=False)
kv_hw = cxim._pb_str(1, 'attachment') + cxim._pb_varint(2, 8) + cxim._pb_str(6, hw_json)
meta_hw = cxim._pb_varint(1, 111) + cxim._pb_varint(5, 1) + cxim._pb_bytes(6, cxim._pb_bytes(5, kv_hw))
check(cxim.decode_meta_to_activity(meta_hw) is None, '非签到活动（作业）被正确过滤')

# CommSyncDL 封包测试
sync_dl_raw = (
    cxim._pb_bytes(1, cxim._pb_varint(1, 0))
    + cxim._pb_bytes(4, meta_sign)
    + cxim._pb_varint(5, 555666)
    + cxim._pb_bytes(6, cxim.encode_jid(name='998877', domain='conference.easemob.com'))
    + cxim._pb_varint(7, 1)
)
sdl = cxim.decode_sync_dl(sync_dl_raw)
check(len(sdl['metas']) == 1 and sdl['nextKey'] == 555666 and sdl['isLast'] is True, 'CommSyncDL 解码正确')

print('--- 4. AutoSignEngine IM 离线自适应降级 ---')
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
