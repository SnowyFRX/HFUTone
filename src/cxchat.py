"""cxchat.py —— 学习通群聊里的签到通知（环信 IM）。

为什么需要它：学习通签到通知除了走"课程中心 → 活动列表"，还会作为 **IM 消息**
发到课程班级群。课程中心不收录的班（教务后台直接排的、老师临时开的），
活动列表根本查不到，但群里照样会收到消息。course_helper 就是靠这条覆盖了
那部分盲区（lib/push/easemob.dart + lib/pages/push/groups.dart）。

照搬过来的关键事实（都来自该仓库）：
  · appKey：`cx-dev#cxstudy`（lib/push/easemob.dart:23）
  · 登录：账号密码来自学习通登录响应
      msg.accountInfo.imAccount.username
      msg.accountInfo.imAccount.password  ← DES-ECB 密文，key "SL2(M/eD"
      （lib/api/login.dart:122-127 + lib/utils/encrypt.dart:40）
  · 消息里的活动信息：attributes.attachment.att_chat_course
      { atype, atypeName, aid, title, subTitle, url,
        courseInfo: { courseid, classid, coursename } }
      （lib/push/easemob.dart:136-160）

实现方式：环信服务端有 REST 接口，客户端 SDK 走的也是同一套后端。
`/token` 用 grant_type=password 就能换到 access_token —— 不需要 client_id/secret
（实测不带凭据请求返回的是 "user not found"，不是 unauthorized）。
拿到 token 后按 REST 拉群列表、拉群消息即可，不必移植 SDK 的 WebSocket 协议。

边界：REST 只做"拉"，不做实时推送。所以这里是「刷新一次，看最近几条」，
不是后台常驻监听——那样得把长连接也搬过来，成本和收益不成比例。
"""
from __future__ import annotations

import json
import urllib.parse

import yktcore as core

APP_KEY = 'cx-dev#cxstudy'
ORG, APP = APP_KEY.split('#', 1)
BASE = f'https://a1.easemob.com/{ORG}/{APP}'
REQUEST_TIMEOUT = 20


def _json_or_text(raw: str):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _call(method: str, url: str, token: str = '', body: dict | None = None) -> dict | list | None:
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    kw = {'extra_headers': headers, 'with_headers': True, 'timeout': REQUEST_TIMEOUT,
          'tag': 'cxchat'}
    if body is not None:
        headers['Content-Type'] = 'application/json'
        kw['data'] = json.dumps(body, ensure_ascii=False)
        kw['extra_headers'] = headers
    r = core.request(method, url, cookie='', referer='', **kw)
    data = _json_or_text(r.body)
    if r.status != 200:
        msg = ''
        if isinstance(data, dict):
            msg = str(data.get('error_description') or data.get('error') or '')
        core.log(f'[群聊] HTTP {r.status} {method} {url[-40:]} {msg}')
        return {'__error__': msg or f'HTTP {r.status}'} if False else None
    return data


def login(username: str, password: str) -> tuple[str, str]:
    """换 access_token。返回 (token, 错误)。"""
    u = (username or '').strip()
    p = password or ''
    if not u or not p:
        return '', '没有 IM 账号（重新登录一次学习通即可拿到）'
    data = _call('POST', f'{BASE}/token',
                 body={'grant_type': 'password', 'username': u, 'password': p})
    if not isinstance(data, dict):
        return '', '环信登录失败（没有返回）'
    tok = str(data.get('access_token') or '')
    if not tok:
        return '', str(data.get('error_description') or data.get('error') or '环信登录失败')
    core.log(f'[群聊] 环信已登录 user={u[:3]}***')
    return tok, ''


def joined_groups(token: str, username: str) -> list[dict]:
    """我加入的群。"""
    url = f'{BASE}/users/{urllib.parse.quote(username)}/joined_chatgroups'
    data = _call('GET', url, token)
    if isinstance(data, dict):
        rows = data.get('data') or data.get('entities') or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    out = []
    for g in rows:
        if not isinstance(g, dict):
            continue
        gid = g.get('groupid') or g.get('id') or g.get('groupId')
        if not gid:
            continue
        out.append({'groupId': str(gid),
                    'name': str(g.get('name') or g.get('groupname') or ''),
                    'owner': str(g.get('owner') or '')})
    return out


def group_messages(token: str, group_id: str, limit: int = 20) -> list:
    """群最近消息（REST 按时间倒序返回）。"""
    url = f'{BASE}/chatgroups/{urllib.parse.quote(group_id)}/messages?limit={max(1, min(50, limit))}'
    data = _call('GET', url, token)
    if isinstance(data, dict):
        rows = data.get('entities') or data.get('data') or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return rows


def _walk_attachments(obj, out: list) -> None:
    """递归找 att_chat_course —— REST 与 SDK 的嵌套层数不一样，别写死路径。"""
    if isinstance(obj, dict):
        att = obj.get('attachment')
        if isinstance(att, dict) and isinstance(att.get('att_chat_course'), dict):
            out.append(att['att_chat_course'])
        for v in obj.values():
            _walk_attachments(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_attachments(v, out)
    elif isinstance(obj, str) and '{' in obj and 'att_chat_course' in obj:
        parsed = _json_or_text(obj)
        if parsed is not None:
            _walk_attachments(parsed, out)


def activities_from_messages(messages: list) -> list[dict]:
    """把消息里的签到活动摊平成列表（去重，保留最新的）。"""
    hits: list[dict] = []
    for m in messages or []:
        found: list[dict] = []
        _walk_attachments(m, found)
        for info in found:
            aid = str(info.get('aid') or '')
            if not aid:
                continue
            ci = info.get('courseInfo')
            ci = ci if isinstance(ci, dict) else {}
            hits.append({
                'activeId': aid,
                'courseId': str(ci.get('courseid') or ''),
                'classId': str(ci.get('classid') or ''),
                'courseName': str(ci.get('coursename') or ''),
                'title': str(info.get('title') or ''),
                'subTitle': str(info.get('subTitle') or ''),
                'atype': info.get('atype'),
                'atypeName': str(info.get('atypeName') or ''),
                'url': str(info.get('url') or ''),
            })
    # 同一个活动可能在多个群里出现
    seen: set[str] = set()
    out: list[dict] = []
    for h in hits:
        if h['activeId'] in seen:
            continue
        seen.add(h['activeId'])
        out.append(h)
    return out


def _selftest() -> None:
    msg = {
        'payload': {
            'attributes': {
                'attachment': {
                    'att_chat_course': {
                        'aid': 123456, 'atype': 2, 'atypeName': '签到',
                        'title': '签到', 'subTitle': '请签到',
                        'courseInfo': {'courseid': 1001, 'classid': 2002,
                                       'coursename': '高等数学'},
                    }
                }
            }
        }
    }
    got = activities_from_messages([msg])
    assert got and got[0]['activeId'] == '123456', got
    assert got[0]['courseId'] == '1001' and got[0]['classId'] == '2002'
    # 重复活动去重
    assert len(activities_from_messages([msg, msg])) == 1
    print('[OK] cxchat 自检通过（活动提取 + 去重）')


if __name__ == '__main__':
    _selftest()