"""wsserve —— 够用就好的 WebSocket 服务端片段。

只做一件事：把 hfutplay 抽出来的 JPEG 帧推给本机页面。不实现扩展、分片、
压缩协商；客户端发来的帧只为识别 close。

为什么需要它：QtWebEngine(Chromium 83) 没有 H.264/AAC，学校的流播不了，
只能由 ffmpeg 解码后用 WS 送帧，页面 canvas 直接画。
"""

from __future__ import annotations

import base64
import hashlib
import select
import socket
import struct

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def accept_key(client_key: str) -> str:
    """RFC6455 的握手应答。"""
    digest = hashlib.sha1((client_key + GUID).encode('ascii')).digest()
    return base64.b64encode(digest).decode('ascii')


def frame(payload: bytes, opcode: int = 0x2, stamp: float | None = None) -> bytes:
    """服务器 → 客户端：不加掩码，长度按 7/16/64 位编码。

    给了 stamp 就在载荷前面塞 8 字节小端 double（秒）。页面据此算端到端延迟，
    免得靠猜——延迟要能被量出来，才谈得上优化。
    """
    if stamp:
        payload = struct.pack('<d', float(stamp)) + payload
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack('>H', n)
    else:
        head += bytes([127]) + struct.pack('>Q', n)
    return head + payload


def peek_close(sock: socket.socket) -> bool:
    """非阻塞地看一眼客户端有没有发 close。False = 该收摊了。

    这里必须用 select 探测再读：直接 recv 会按 socket 超时干等 0.4 秒，
    帧率立刻掉到 2fps（每帧都在等一个永远不会来的字节）。
    """
    try:
        ready, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    if not ready:
        return True
    try:
        data = sock.recv(2048)
    except OSError:
        return False
    if not data:
        return False
    # 客户端帧一定带掩码，第一字节低 4 位是 opcode；0x8 就是 close
    return (data[0] & 0x0F) != 0x8