"""cxim.py —— 纯 Python 标准库实现的环信 IM 长连接客户端（MSYNC over SockJS/WebSocket）。

为什么要把环信长连接从前端 WebView（web/im.js）下沉到 Python 后端：
  - Android 切后台或锁屏后，WebView 的 JS 定时器与回调会被系统节流；
  - 当 Activity 被销毁或 KeepAliveService 经 START_STICKY 后台重建时，
    进程里根本没有 WebView，前端 im.js 完全不存在，IM 秒签 100% 失效。
  - 下沉到 Python 守护线程后，桌面端与 Android（KeepAliveService 前台服务）
    均由后端直接维护 WebSocket 长连接、心跳、断线重连与漫游补录，真正实现
    不依赖界面的全天候秒签。

协议依据（直接取自仓库内 web/im/Easemob.js）：
  1. 传输层：SockJS Raw WebSocket
     wss://im-api-vip6-v2.easecdn.com/ws/<3位数字>/<8位随机>/websocket
     下行：o（打开）、h（心跳）、a["<base64>",...]（消息数组）、c[code,"reason"]（关闭）
     上行：["<base64>"]
  2. 帧内载荷：Base64 编码的 easemob.pb.MSync Protobuf
     - command=3 PROVISION：建连鉴权（auth="$t$"+access_token）
     - command=1 UNREAD：心跳与未读队列拉取（下行 CommUnreadDL）
     - command=2 NOTICE：服务端实时推送有新消息的队列（下行 CommNotice）
     - command=0 SYNC：上行 CommSyncUL 拉队列，下行 CommSyncDL 返回 Meta 列表
  3. 群聊签到消息位于 Meta(ns=1 CHAT).payload -> MessageBody.ext(key="attachment")
     的 JSON 字段 att_chat_course 中。
  4. 历史漫游消息：POST /users/{uid}/messageroaming，返回每条 base64 Meta。

零第三方依赖（仅用标准库 socket/ssl/struct/base64/json/threading），
兼容 Windows 桌面版与 Android Chaquopy 环境。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import ssl
import string
import struct
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import cxchat
import yktcore as core

WS_HOST = 'im-api-vip6-v2.easecdn.com'
WS_PORT = 443
WS_BASE_PATH = '/ws'
DOMAIN = 'easemob.com'
APP_KEY = cxchat.APP_KEY          # 'cx-dev#cxstudy'
SDK_VERSION = '3.6.3'
OS_TYPE_OTHER = 16
HEARTBEAT_INTERVAL = 25.0
WS_MAGIC_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


# ============================================================================
# 1. 极简 Protobuf 编解码（纯标准库，仅覆盖 easemob.pb 所需字段）
# ============================================================================

def _enc_varint(v: int) -> bytes:
    if v < 0:
        v &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_varint(field: int, val: int) -> bytes:
    return _enc_varint((field << 3) | 0) + _enc_varint(val)


def _pb_bytes(field: int, data: bytes) -> bytes:
    return _enc_varint((field << 3) | 2) + _enc_varint(len(data)) + data


def _pb_str(field: int, text: str) -> bytes:
    return _pb_bytes(field, text.encode('utf-8'))


def _dec_varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = 0
    val = 0
    n = len(buf)
    while pos < n:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift > 70:
            raise ValueError('varint overflow')
    raise ValueError('truncated varint')


def _pb_parse(buf: bytes) -> list[tuple[int, int, Any]]:
    """把 protobuf 二进制解析为 [(field_number, wire_type, value), ...]。"""
    out: list[tuple[int, int, Any]] = []
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _dec_varint(buf, pos)
        field = tag >> 3
        wtype = tag & 0x07
        if wtype == 0:
            v, pos = _dec_varint(buf, pos)
            out.append((field, 0, v))
        elif wtype == 1:
            if pos + 8 > n:
                break
            out.append((field, 1, buf[pos:pos + 8]))
            pos += 8
        elif wtype == 2:
            length, pos = _dec_varint(buf, pos)
            if pos + length > n:
                break
            out.append((field, 2, buf[pos:pos + length]))
            pos += length
        elif wtype == 5:
            if pos + 4 > n:
                break
            out.append((field, 5, buf[pos:pos + 4]))
            pos += 4
        else:
            break
    return out


# ---- easemob.pb 消息构造 ---------------------------------------------------

def encode_jid(app_key: str = '', name: str = '', domain: str = '',
               client_resource: str = '') -> bytes:
    parts = []
    if app_key:
        parts.append(_pb_str(1, app_key))
    if name:
        parts.append(_pb_str(2, name))
    if domain:
        parts.append(_pb_str(3, domain))
    if client_resource:
        parts.append(_pb_str(4, client_resource))
    return b''.join(parts)


def decode_jid(buf: bytes) -> dict[str, str]:
    d = {'appKey': '', 'name': '', 'domain': '', 'clientResource': ''}
    for f, wt, v in _pb_parse(buf):
        if wt != 2:
            continue
        s = v.decode('utf-8', errors='replace')
        if f == 1:
            d['appKey'] = s
        elif f == 2:
            d['name'] = s
        elif f == 3:
            d['domain'] = s
        elif f == 4:
            d['clientResource'] = s
    return d


def encode_msync(command: int, payload: bytes = b'', guid: bytes = b'',
                 auth: str = '') -> bytes:
    parts = [_pb_varint(1, 0)]             # version = MSYNC_V1 (0)
    if guid:
        parts.append(_pb_bytes(2, guid))
    if auth:
        parts.append(_pb_str(3, auth))
    parts.append(_pb_varint(8, command))
    if payload:
        parts.append(_pb_bytes(9, payload))
    parts.append(_pb_varint(11, 0))        # encryptType = [ENCRYPT_NONE]
    return b''.join(parts)


def encode_provision(uid: str, token: str, app_key: str = APP_KEY,
                     domain: str = DOMAIN) -> tuple[bytes, str]:
    ts = str(int(time.time() * 1000))
    resource = f'webim_{ts}'
    auth = f'$t${token}'
    prov = b''.join([
        _pb_varint(1, OS_TYPE_OTHER),      # osType = OS_OTHER (16)
        _pb_str(2, SDK_VERSION),           # version = "3.6.3"
        _pb_varint(5, 0),                  # compressType = [COMPRESS_NONE]
        _pb_varint(6, 0),                  # encryptType = [ENCRYPT_NONE]
        _pb_str(9, ts),                    # deviceUuid
        _pb_str(12, 'webim'),              # deviceName
        _pb_str(13, resource),             # resource
        _pb_str(14, auth),                 # auth
    ])
    guid = encode_jid(app_key=app_key, name=uid, domain=domain,
                      client_resource=resource)
    return encode_msync(command=3, payload=prov, guid=guid, auth=auth), resource


def encode_unread() -> bytes:
    return encode_msync(command=1)


def encode_rebuild() -> bytes:
    stat = _pb_varint(1, 0)                # StatisticsBody.operation = INFORMATION
    meta = b''.join([
        _pb_varint(1, int(time.time() * 1000)),
        _pb_varint(5, 0),                  # NameSpace.STATISTIC
        _pb_bytes(6, stat),
    ])
    sync_ul = _pb_bytes(1, meta)
    return encode_msync(command=0, payload=sync_ul)


def encode_sync_queue(queue_jid: dict[str, str], key: int = 0) -> bytes:
    q = encode_jid(
        app_key=queue_jid.get('appKey', ''),
        name=queue_jid.get('name', ''),
        domain=queue_jid.get('domain', ''),
        client_resource=queue_jid.get('clientResource', ''),
    )
    parts = []
    if key:
        parts.append(_pb_varint(2, key))
    parts.append(_pb_bytes(3, q))
    return encode_msync(command=0, payload=b''.join(parts))


# ---- easemob.pb 下行解析 ---------------------------------------------------

def decode_status(buf: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {'errorCode': 0, 'reason': ''}
    for f, wt, v in _pb_parse(buf):
        if f == 1 and wt == 0:
            out['errorCode'] = int(v)
        elif f == 2 and wt == 2:
            out['reason'] = v.decode('utf-8', errors='replace')
    return out


def decode_msync(buf: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {'command': -1, 'payload': b''}
    for f, wt, v in _pb_parse(buf):
        if f == 8 and wt == 0:
            out['command'] = int(v)
        elif f == 9 and wt == 2:
            out['payload'] = v
    return out


def decode_provision_dl(buf: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {'status': {'errorCode': 0, 'reason': ''}, 'resource': ''}
    for f, wt, v in _pb_parse(buf):
        if f == 8 and wt == 2:
            out['status'] = decode_status(v)
        elif f == 13 and wt == 2:
            out['resource'] = v.decode('utf-8', errors='replace')
    return out


def decode_unread_dl(buf: bytes) -> list[dict[str, str]]:
    queues: list[dict[str, str]] = []
    for f, wt, v in _pb_parse(buf):
        if f == 2 and wt == 2:             # MetaQueue
            for mf, mwt, mv in _pb_parse(v):
                if mf == 1 and mwt == 2:
                    queues.append(decode_jid(mv))
    return queues


def decode_notice(buf: bytes) -> dict[str, str] | None:
    for f, wt, v in _pb_parse(buf):
        if f == 1 and wt == 2:
            return decode_jid(v)
    return None


def decode_sync_dl(buf: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {
        'status': {'errorCode': 0, 'reason': ''},
        'metas': [],
        'nextKey': 0,
        'queue': None,
        'isLast': True,
    }
    for f, wt, v in _pb_parse(buf):
        if f == 1 and wt == 2:
            out['status'] = decode_status(v)
        elif f == 4 and wt == 2:
            out['metas'].append(v)
        elif f == 5 and wt == 0:
            out['nextKey'] = int(v)
        elif f == 6 and wt == 2:
            out['queue'] = decode_jid(v)
        elif f == 7 and wt == 0:
            out['isLast'] = bool(v)
    return out


def decode_meta_to_activity(meta_bytes: bytes) -> dict[str, Any] | None:
    """把一条 easemob.pb.Meta（ns=1 CHAT）解码为学习通签到活动，非签到返回 None。"""
    msg_id = 0
    ns = -1
    payload = b''
    for f, wt, v in _pb_parse(meta_bytes):
        if f == 1 and wt == 0:
            msg_id = int(v)
        elif f == 5 and wt == 0:
            ns = int(v)
        elif f == 6 and wt == 2:
            payload = v
    if ns != 1 or not payload:
        return None

    # MessageBody: field 5 = repeated KeyValue ext
    ext: dict[str, Any] = {}
    for f, wt, v in _pb_parse(payload):
        if f != 5 or wt != 2:
            continue
        k = ''
        ktype = 0
        sval = ''
        ival = None
        for kf, kwt, kv in _pb_parse(v):
            if kf == 1 and kwt == 2:
                k = kv.decode('utf-8', errors='replace')
            elif kf == 2 and kwt == 0:
                ktype = int(kv)
            elif kf == 3 and kwt == 0:
                ival = int(kv)
            elif kf == 6 and kwt == 2:
                sval = kv.decode('utf-8', errors='replace')
        if not k:
            continue
        if ktype == 8 and sval:            # JSON_STRING
            try:
                ext[k] = json.loads(sval)
            except ValueError:
                ext[k] = sval
        elif sval:
            ext[k] = sval
        elif ival is not None:
            ext[k] = ival

    att = ext.get('attachment')
    if isinstance(att, str):
        try:
            att = json.loads(att)
        except ValueError:
            att = None
    if not isinstance(att, dict):
        return None
    course_att = att.get('att_chat_course')
    if not isinstance(course_att, dict):
        return None
    aid = str(course_att.get('aid') or '').strip()
    if not aid:
        return None

    atype_name = str(course_att.get('atypeName') or '')
    title = str(course_att.get('title') or atype_name or '签到')
    label = atype_name + title
    if '签到' not in label:
        return None

    ci = course_att.get('courseInfo')
    ci = ci if isinstance(ci, dict) else {}
    return {
        'activeId': aid,
        'courseId': str(ci.get('courseid') or ''),
        'classId': str(ci.get('classid') or ''),
        'courseName': str(ci.get('coursename') or ''),
        'title': title,
        'atypeName': atype_name,
        'url': str(course_att.get('url') or ''),
        'msgId': str(msg_id) if msg_id else '',
    }


# ============================================================================
# 2. 极简 RFC 6455 TLS WebSocket 客户端（纯标准库）
# ============================================================================

class SimpleWebSocket:
    """仅实现客户端所需的 RFC 6455 TLS WebSocket 握手与文本/控制帧收发。"""

    def __init__(self, host: str, port: int, path: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.path = path
        self.timeout = timeout
        self._sock: ssl.SSLSocket | None = None
        self._buf = bytearray()
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        ctx = ssl.create_default_context()
        ssock = ctx.wrap_socket(raw, server_hostname=self.host)
        ssock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode('ascii')
        req = (
            f'GET {self.path} HTTP/1.1\r\n'
            f'Host: {self.host}\r\n'
            f'Upgrade: websocket\r\n'
            f'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            f'Sec-WebSocket-Version: 13\r\n'
            f'Origin: https://{self.host}\r\n'
            f'User-Agent: Mozilla/5.0\r\n'
            f'\r\n'
        ).encode('ascii')
        ssock.sendall(req)

        # 读取 HTTP 101 响应头
        resp = bytearray()
        while b'\r\n\r\n' not in resp:
            chunk = ssock.recv(4096)
            if not chunk:
                ssock.close()
                raise ConnectionError('WebSocket 握手时连接关闭')
            resp.extend(chunk)
            if len(resp) > 16384:
                ssock.close()
                raise ConnectionError('WebSocket 握手响应头异常')

        sep = resp.index(b'\r\n\r\n') + 4
        head = bytes(resp[:sep]).decode('latin-1', errors='replace')
        self._buf.extend(resp[sep:])
        first_line = head.split('\r\n', 1)[0]
        if '101' not in first_line:
            ssock.close()
            raise ConnectionError(f'WebSocket 握手被拒: {first_line}')

        expected = base64.b64encode(
            hashlib.sha1((key + WS_MAGIC_GUID).encode('ascii')).digest()
        ).decode('ascii')
        if expected.lower() not in head.lower():
            ssock.close()
            raise ConnectionError('WebSocket Sec-WebSocket-Accept 校验失败')

        ssock.settimeout(5.0)
        self._sock = ssock

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise ConnectionError('WebSocket 对端关闭')
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send_frame(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self._sock is None:
                raise ConnectionError('WebSocket 未连接')
            length = len(payload)
            header = bytearray([0x80 | (opcode & 0x0F)])
            if length < 126:
                header.append(0x80 | length)
            elif length < 65536:
                header.append(0x80 | 126)
                header.extend(struct.pack('!H', length))
            else:
                header.append(0x80 | 127)
                header.extend(struct.pack('!Q', length))
            mask = os.urandom(4)
            header.extend(mask)
            masked = bytes(payload[i] ^ mask[i & 3] for i in range(length))
            self._sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode('utf-8'))

    def recv_text(self) -> str | None:
        """读取一条完整文本消息。读超时返回 None，对端关闭抛 ConnectionError。"""
        fragments: list[bytes] = []
        while True:
            try:
                b1, b2 = self._recv_exact(2)
            except socket.timeout:
                if not fragments:
                    return None
                continue
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack('!H', self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b''
            data = self._recv_exact(length) if length else b''
            if masked:
                data = bytes(data[i] ^ mask[i & 3] for i in range(len(data)))

            if opcode == 0x8:              # Close
                raise ConnectionError('收到 WebSocket Close 帧')
            if opcode == 0x9:              # Ping -> Pong
                self.send_frame(0xA, data)
                continue
            if opcode == 0xA:              # Pong
                continue
            if opcode in (0x0, 0x1, 0x2):
                fragments.append(data)
                if fin:
                    return b''.join(fragments).decode('utf-8', errors='replace')

    def close(self) -> None:
        s = self._sock
        self._sock = None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


# ============================================================================
# 3. 漫游历史消息拉取（纯 Python，不依赖 WebView）
# ============================================================================

def fetch_roaming_activities(token: str, uid: str, groups: list[dict],
                             max_groups: int = 20) -> list[dict]:
    """用环信 messageroaming 接口拉取各班级群最近历史消息并解出签到活动。"""
    if not token or not uid or not groups:
        return []

    url = f'{cxchat.BASE}/users/{urllib.parse.quote(uid)}/messageroaming'
    headers = {
        'Accept': 'application/json',
        'Authorization': 'Bearer ' + token,
    }

    def _one_group(g: dict) -> list[dict]:
        gid = str(g.get('groupId') or '').strip()
        if not gid:
            return []
        body = {'queue': f'{gid}@conference.easemob.com', 'start': -1, 'end': -1}
        try:
            r = core.request('POST', url, cookie='', referer='',
                             data=body, extra_headers=headers,
                             with_headers=True, timeout=15, tag='cxim-roam')
            if r.status != 200:
                return []
            d = json.loads(r.body)
        except Exception:                                # noqa: BLE001
            return []
        msgs = ((d.get('data') or {}).get('msgs') or []) if isinstance(d, dict) else []
        found: list[dict] = []
        for entry in msgs:
            raw_b64 = entry.get('msg') if isinstance(entry, dict) else ''
            if not raw_b64:
                continue
            try:
                act = decode_meta_to_activity(base64.b64decode(raw_b64))
            except Exception:                            # noqa: BLE001
                act = None
            if act:
                found.append(act)
        return found

    out: list[dict] = []
    seen: set[str] = set()
    target = list(groups)[:max_groups]
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            batches = list(pool.map(_one_group, target))
    except Exception:                                    # noqa: BLE001
        batches = [_one_group(g) for g in target]

    for batch in batches:
        for act in batch:
            aid = act.get('activeId') or ''
            if aid and aid not in seen:
                seen.add(aid)
                out.append(act)
    return out


# ============================================================================
# 4. 后端常驻环信 IM 守护客户端
# ============================================================================

def _sockjs_path() -> str:
    srv_id = f'{random.randint(0, 999):03d}'
    sess_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f'{WS_BASE_PATH}/{srv_id}/{sess_id}/websocket'


class BackendIMClient:
    """常驻后端线程维护环信 WebSocket 长连接，彻底摆脱 WebView 节流与生命周期限制。"""

    def __init__(
        self,
        get_cx_state: Callable[[], Any],
        on_activities: Callable[[list[dict], bool], None],
    ):
        self._get_cx = get_cx_state
        self._on_activities = on_activities

        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started = False
        self._ws: SimpleWebSocket | None = None

        self._connected = False
        self._state = 'idle'       # idle | connecting | connected | backoff | disabled
        self._detail = '未连接'
        self._error = ''
        self._groups_count = 0
        self._received = 0
        self._last_heartbeat = 0.0
        self._last_message = 0.0
        self._seen_msg_ids: set[str] = set()
        self._roaming_done_uid = ''

    @property
    def connected(self) -> bool:
        with self.lock:
            return self._connected

    def start(self) -> None:
        with self.lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
        threading.Thread(target=self._run_loop, name='cx-im-ws', daemon=True).start()
        core.log('[后端IM] 环信长连接守护线程已启动')

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        ws = self._ws
        if ws is not None:
            ws.close()

    def notify_login_changed(self) -> None:
        """学习通登录或退出时唤醒守护线程，立刻建连或断开。"""
        with self.lock:
            self._roaming_done_uid = ''
        ws = self._ws
        if ws is not None:
            ws.close()
        self._wake.set()

    def _set_state(self, connected: bool, state: str, detail: str, error: str = '') -> None:
        with self.lock:
            self._connected = connected
            self._state = state
            self._detail = detail
            self._error = error

    def _credentials(self) -> tuple[str, str, str]:
        """从学习通登录态取出 (im_uid, im_pwd, err)。缺 imAccount 时现场补拉一次。"""
        cx_state = self._get_cx()
        if not getattr(cx_state, 'logged_in', False):
            return '', '', '学习通未登录'
        store = cx_state.signer.api.store
        user_obj = store.get_user() or {}
        im = user_obj.get('imAccount') or {}
        u = str(im.get('userName') or '').strip()
        p = str(im.get('password') or '')
        if not u or not p:
            try:
                cx_state.signer.api.refresh_user()
            except Exception as exc:                     # noqa: BLE001
                core.log(f'[后端IM] 现场补拉 IM 账号失败：{exc}')
            user_obj = store.get_user() or {}
            im = user_obj.get('imAccount') or {}
            u = str(im.get('userName') or '').strip()
            p = str(im.get('password') or '')
        if not u or not p:
            return '', '', '未获取到 IM 账号'
        return u, p, ''

    def _send_pb(self, ws: SimpleWebSocket, raw: bytes) -> None:
        b64 = base64.b64encode(raw).decode('ascii')
        ws.send_text(json.dumps([b64]))

    def _run_loop(self) -> None:
        backoff = 3.0
        # 启动后稍等 2 秒让状态对象初始化完成
        self._wake.wait(timeout=2.0)

        while not self._stop.is_set():
            self._wake.clear()
            uid, pwd, err = self._credentials()
            if err:
                self._set_state(False, 'idle', err)
                self._wake.wait(timeout=15.0)
                continue

            self._set_state(False, 'connecting', '正在登录环信…')
            try:
                token, lerr = cxchat.login(uid, pwd)
            except Exception as exc:                     # noqa: BLE001
                token, lerr = '', str(exc)[:120]

            if not token:
                self._set_state(False, 'backoff', f'环信登录失败，{int(backoff)}s 后重试', lerr)
                core.log(f'[后端IM] 环信登录失败：{lerr}')
                self._wake.wait(timeout=backoff)
                backoff = min(60.0, backoff * 2)
                continue

            # 顺便拿一下群列表用于漫游补录
            groups: list[dict] = []
            try:
                groups = cxchat.joined_groups(token, uid)
                with self.lock:
                    self._groups_count = len(groups)
            except Exception:                            # noqa: BLE001
                pass

            try:
                ok_session = self._session_once(uid, token, groups)
                if ok_session:
                    backoff = 3.0
            except Exception as exc:                     # noqa: BLE001
                msg = str(exc)[:120]
                core.log(f'[后端IM] 连接断开：{msg}')
                self._set_state(False, 'backoff', f'连接断开，{int(backoff)}s 后重连', msg)
            finally:
                ws = self._ws
                self._ws = None
                if ws is not None:
                    ws.close()
                with self.lock:
                    self._connected = False

            if self._stop.is_set():
                break
            self._wake.wait(timeout=backoff)
            backoff = min(60.0, backoff * 2)

    def _session_once(self, uid: str, token: str, groups: list[dict]) -> bool:
        """执行一次完整的 SockJS-WebSocket 建连 -> Provision -> 实时监听循环。"""
        ws = SimpleWebSocket(WS_HOST, WS_PORT, _sockjs_path(), timeout=15.0)
        self._ws = ws
        ws.connect()

        # 等待 SockJS 'o' 帧
        opened = False
        deadline = time.time() + 10.0
        while not self._stop.is_set() and time.time() < deadline:
            frame = ws.recv_text()
            if frame is None:
                continue
            if frame == 'o':
                opened = True
                break
            if frame.startswith('c'):
                raise ConnectionError(f'SockJS 拒绝连接: {frame}')
        if not opened:
            raise ConnectionError('等待 SockJS open 帧超时')

        # 发送 MSYNC Provision 鉴权
        prov_bytes, _res = encode_provision(uid, token)
        self._send_pb(ws, prov_bytes)

        provisioned = False
        last_hb = time.time()
        last_rx = time.time()
        pending_queues: list[dict[str, str]] = []

        def _enqueue_queue(q: dict[str, str] | None) -> None:
            if not q or not q.get('name'):
                return
            key = (q.get('name'), q.get('domain'))
            if any((x.get('name'), x.get('domain')) == key for x in pending_queues):
                return
            pending_queues.append(q)
            if len(pending_queues) == 1:
                self._send_pb(ws, encode_sync_queue(q))

        while not self._stop.is_set():
            # 检查登录态是否已退出
            if not getattr(self._get_cx(), 'logged_in', False):
                self._set_state(False, 'idle', '学习通已退出')
                return True

            frame = ws.recv_text()
            now = time.time()
            if frame is None:
                if provisioned and now - last_hb >= HEARTBEAT_INTERVAL:
                    self._send_pb(ws, encode_unread())
                    last_hb = now
                    with self.lock:
                        self._last_heartbeat = now
                # 超过 90 秒没有任何下行帧（含 SockJS 'h' 心跳）则主动重连
                if now - last_rx > 90.0:
                    raise ConnectionError('心跳超时（90s 无下行）')
                continue

            last_rx = now
            if frame == 'h':
                with self.lock:
                    self._last_heartbeat = now
                continue
            if frame.startswith('c'):
                raise ConnectionError(f'服务端关闭连接: {frame}')
            if not frame.startswith('a['):
                continue

            try:
                items = json.loads(frame[1:])
            except ValueError:
                continue

            for b64_msg in items:
                if not isinstance(b64_msg, str):
                    continue
                try:
                    raw = base64.b64decode(b64_msg)
                    ms = decode_msync(raw)
                except Exception:                        # noqa: BLE001
                    continue

                cmd = ms['command']
                pl = ms['payload']

                if cmd == 3:                             # PROVISION 回执
                    pdl = decode_provision_dl(pl)
                    ec = pdl['status']['errorCode']
                    if ec != 0:
                        raise ConnectionError(
                            f"Provision 失败 code={ec} {pdl['status']['reason']}"
                        )
                    provisioned = True
                    last_hb = now
                    with self.lock:
                        self._last_heartbeat = now
                    self._set_state(
                        True, 'connected',
                        f'后端实时在线（{self._groups_count} 个群 · 后台秒签）',
                    )
                    core.log(f'[后端IM] 环信长连接已就绪（{self._groups_count} 个群）')
                    # 鉴权成功后立刻拉未读 + 发统计重建 + 后台补一次漫游历史
                    self._send_pb(ws, encode_unread())
                    self._send_pb(ws, encode_rebuild())
                    if groups and self._roaming_done_uid != uid:
                        self._roaming_done_uid = uid
                        threading.Thread(
                            target=self._pull_roaming_bg,
                            args=(token, uid, groups),
                            name='cx-im-roam',
                            daemon=True,
                        ).start()

                elif cmd == 1:                           # CommUnreadDL
                    queues = decode_unread_dl(pl)
                    for q in queues:
                        _enqueue_queue(q)
                    if queues:
                        self._send_pb(ws, encode_rebuild())

                elif cmd == 2:                           # CommNotice（实时推送！）
                    q = decode_notice(pl)
                    _enqueue_queue(q)

                elif cmd == 0:                           # CommSyncDL（消息体！）
                    sdl = decode_sync_dl(pl)
                    acts: list[dict] = []
                    for mbytes in sdl['metas']:
                        act = decode_meta_to_activity(mbytes)
                        if not act:
                            continue
                        mid = act.get('msgId') or ''
                        with self.lock:
                            if mid and mid in self._seen_msg_ids:
                                continue
                            if mid:
                                self._seen_msg_ids.add(mid)
                                if len(self._seen_msg_ids) > 500:
                                    self._seen_msg_ids.clear()
                        acts.append(act)

                    if acts:
                        with self.lock:
                            self._received += len(acts)
                            self._last_message = now
                        core.log(f'[后端IM] 实时收到 {len(acts)} 条群聊签到通知')
                        try:
                            self._on_activities(acts, True)
                        except Exception as exc:         # noqa: BLE001
                            core.log(f'[后端IM] 分发签到事件异常：{exc}')

                    q_done = sdl.get('queue')
                    if sdl.get('isLast'):
                        if q_done:
                            key = (q_done.get('name'), q_done.get('domain'))
                            pending_queues[:] = [
                                x for x in pending_queues
                                if (x.get('name'), x.get('domain')) != key
                            ]
                        if pending_queues:
                            self._send_pb(ws, encode_sync_queue(pending_queues[0]))
                    elif q_done and sdl.get('nextKey'):
                        self._send_pb(ws, encode_sync_queue(q_done, key=sdl['nextKey']))

            if provisioned and now - last_hb >= HEARTBEAT_INTERVAL:
                self._send_pb(ws, encode_unread())
                last_hb = now
                with self.lock:
                    self._last_heartbeat = now

        return provisioned

    def _pull_roaming_bg(self, token: str, uid: str, groups: list[dict]) -> None:
        try:
            acts = fetch_roaming_activities(token, uid, groups)
            if acts:
                with self.lock:
                    self._received += len(acts)
                core.log(f'[后端IM] 漫游补录 {len(acts)} 条群聊签到历史')
                self._on_activities(acts, False)
        except Exception as exc:                         # noqa: BLE001
            core.log(f'[后端IM] 漫游补录异常：{exc}')

    def status_payload(self) -> dict[str, Any]:
        with self.lock:
            return {
                'ok': True,
                'backend': True,
                'connected': self._connected,
                'state': self._state,
                'detail': self._detail,
                'error': self._error,
                'groups': self._groups_count,
                'received': self._received,
                'lastHeartbeat': int(self._last_heartbeat),
                'lastMessage': int(self._last_message),
            }
