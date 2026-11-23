"""yktws.py —— 最小 RFC6455 WebSocket 客户端（标准库实现）。

为什么不用第三方库：打包体积、以及不想给 PyInstaller 多带一个依赖。
雨课堂的随堂题没有 HTTP 查询接口，只在 wss://<host>/wsapp/ 的 hello 回包里
通过 timeline / unlockedproblem 下发，所以必须真连一次网关。

握手用 ssl 直连。注意：HTTP 接口那边的 TLS 指纹拦截是网关按 UA/JA3 打的
HTML 页，WS 握手走的是同一条 TLS 通道，但逆向出的原客户端（.NET
ClientWebSocket / Schannel）能正常握手，所以这里用标准库 ssl 也可以。
万一被抓，日志里会明确写"握手被拒 = HTTP 4xx"，不静默。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse


class WsError(RuntimeError):
    pass


class WS:
    """阻塞式 WebSocket。够用就好：文本帧、ping/pong、close、分片拼接。"""

    def __init__(self, url: str, headers: dict | None = None, timeout: float = 15.0):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ('ws', 'wss'):
            raise WsError(f'不支持的 scheme: {u.scheme}')
        self.secure = u.scheme == 'wss'
        self.host = u.hostname or ''
        self.port = u.port or (443 if self.secure else 80)
        self.path = (u.path or '/') + (('?' + u.query) if u.query else '')
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b''
        self._closed = False

    # -- 连接 -------------------------------------------------------------
    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            ctx = ssl.create_default_context()
            # 与浏览器一致的 SNI / ALPN，避免被网关当成非浏览器
            ctx.set_alpn_protocols(['http/1.1'])
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(self.timeout)
        self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f'GET {self.path} HTTP/1.1',
                 f'Host: {self.host}',
                 'Upgrade: websocket',
                 'Connection: Upgrade',
                 f'Sec-WebSocket-Key: {key}',
                 'Sec-WebSocket-Version: 13']
        for k, v in self.headers.items():
            if k.lower() in ('host', 'upgrade', 'connection',
                             'sec-websocket-key', 'sec-websocket-version'):
                continue
            lines.append(f'{k}: {v}')
        req = ('\r\n'.join(lines) + '\r\n\r\n').encode('utf-8')
        self.sock.sendall(req)

        head = b''
        while b'\r\n\r\n' not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsError('握手期间连接被关闭')
            head += chunk
            if len(head) > 65536:
                raise WsError('握手响应过大')
        header, _, rest = head.partition(b'\r\n\r\n')
        self._buf = rest
        text = header.decode('latin-1')
        status = text.split('\r\n', 1)[0]
        if ' 101 ' not in status:
            raise WsError(f'握手被拒：{status}')
        expect = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if expect.lower() not in text.lower():
            raise WsError('握手响应缺少 Sec-WebSocket-Accept')

    # -- 帧 ---------------------------------------------------------------
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.sock:
            raise WsError('还没连接')
        mask = os.urandom(4)
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack('>H', n)
        else:
            head.append(0x80 | 127)
            head += struct.pack('>Q', n)
        head += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(head) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode('utf-8'))

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsError('连接已关闭')
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self, timeout: float | None = None) -> tuple[int, bytes]:
        """读一个完整消息（分片自动拼接）。返回 (opcode, payload)。"""
        if timeout is not None and self.sock:
            self.sock.settimeout(timeout)
        op_first = None
        data = b''
        while True:
            b1, b2 = self._recv_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack('>H', self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack('>Q', self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b''
            payload = self._recv_exact(n) if n else b''
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x9:                    # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:                    # pong
                continue
            if opcode == 0x8:                    # close
                self._closed = True
                return 0x8, payload
            if opcode in (0x1, 0x2):
                op_first = opcode
                data = payload
            elif opcode == 0x0:
                data += payload
            if fin and op_first is not None:
                return op_first, data

    def recv_json(self, timeout: float | None = None) -> dict | None:
        try:
            op, payload = self.recv(timeout)
        except (socket.timeout, TimeoutError):
            return None
        if op == 0x8:
            self._closed = True
            return None
        try:
            obj = json.loads(payload.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    def close(self) -> None:
        if self.sock and not self._closed:
            try:
                self._send_frame(0x8, b'')
            except OSError:
                pass
        self._closed = True
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()


def now() -> float:
    return time.time()