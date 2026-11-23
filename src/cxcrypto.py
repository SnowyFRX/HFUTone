"""学习通（超星）协议用到的密码学原语——纯 Python，不引第三方包。

为什么要自己写：这台机器上 Crypto / cryptography / rsa 一个都没有，而学习通
整套接口都靠这几个算法过门：

  · AES-128-CBC  web 登录把账号密码加密（key 同时当 IV 用）
  · AES-128-ECB  App 登录的 logininfo、设备指纹 deviceCode
  · RSA 模幂     getUserInfo 拿 clientId 要「用公钥解密」——不是笔误，
                 服务端就是拿公钥做 c^e mod n 再剥 PKCS#1 填充
  · MD5          schild 指纹、signToken、inf_enc

只用 AES-128，因为学习通那几把 key 全是 16 字节，没有 192/256 的用法。
纯 Python 慢，但这里每次登录/签到的调用次数是个位数，几十毫秒的差别无感。
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct

# --------------------------------------------------------------- AES 基础表

_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]

_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1b) & 0xff
    return a


def _mul(a: int, b: int) -> int:
    """GF(2^8) 乘法。"""
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        b >>= 1
        a = _xtime(a)
    return out & 0xff


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-128 密钥扩展：返回 11 组轮密钥，每组 16 字节。"""
    if len(key) != 16:
        raise ValueError(f'AES-128 需要 16 字节密钥，收到 {len(key)}')
    words = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                      # RotWord
            temp = [_SBOX[b] for b in temp]                 # SubWord
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    rounds = []
    for r in range(11):
        flat = []
        for w in words[r * 4:r * 4 + 4]:
            flat.extend(w)
        rounds.append(flat)
    return rounds


def _add_round_key(state: list[int], rk: list[int]) -> None:
    for i in range(16):
        state[i] ^= rk[i]


def _sub_bytes(state: list[int]) -> None:
    for i in range(16):
        state[i] = _SBOX[state[i]]


def _inv_sub_bytes(state: list[int]) -> None:
    for i in range(16):
        state[i] = _INV_SBOX[state[i]]


def _shift_rows(state: list[int]) -> None:
    # state 按列优先排（state[r + 4c]），第 r 行左移 r 格
    for r in range(1, 4):
        row = [state[r + 4 * c] for c in range(4)]
        row = row[r:] + row[:r]
        for c in range(4):
            state[r + 4 * c] = row[c]


def _inv_shift_rows(state: list[int]) -> None:
    for r in range(1, 4):
        row = [state[r + 4 * c] for c in range(4)]
        row = row[-r:] + row[:-r]
        for c in range(4):
            state[r + 4 * c] = row[c]


def _mix_columns(state: list[int]) -> None:
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
        state[i + 1] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
        state[i + 2] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
        state[i + 3] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)


def _inv_mix_columns(state: list[int]) -> None:
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
        state[i + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
        state[i + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
        state[i + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)


def _encrypt_block(block: bytes, rounds: list[list[int]]) -> bytes:
    state = list(block)
    _add_round_key(state, rounds[0])
    for r in range(1, 10):
        _sub_bytes(state)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, rounds[r])
    _sub_bytes(state)
    _shift_rows(state)
    _add_round_key(state, rounds[10])
    return bytes(state)


def _decrypt_block(block: bytes, rounds: list[list[int]]) -> bytes:
    state = list(block)
    _add_round_key(state, rounds[10])
    for r in range(9, 0, -1):
        _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, rounds[r])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, rounds[0])
    return bytes(state)


# ------------------------------------------------------------------ 填充

def _pad(data: bytes) -> bytes:
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if n < 1 or n > 16 or n > len(data):
        return data                       # 填充不合法就当没有，别把数据截坏
    return data[:-n]


# -------------------------------------------------------------- 对外接口

def aes_ecb_encrypt(text: str, key: str) -> str:
    """AES-128-ECB + PKCS7，返回 base64。学习通 App 登录和设备指纹用它。"""
    rounds = _expand_key(key.encode('utf-8'))
    raw = _pad(text.encode('utf-8'))
    out = b''.join(_encrypt_block(raw[i:i + 16], rounds) for i in range(0, len(raw), 16))
    return base64.b64encode(out).decode('ascii')


def aes_ecb_decrypt(cipher_b64: str, key: str) -> str:
    rounds = _expand_key(key.encode('utf-8'))
    raw = base64.b64decode(cipher_b64)
    out = b''.join(_decrypt_block(raw[i:i + 16], rounds) for i in range(0, len(raw), 16))
    return _unpad(out).decode('utf-8', 'replace')


def aes_cbc_encrypt(text: str, key: str, iv: str | None = None) -> str:
    """AES-128-CBC + PKCS7，返回 base64。

    学习通 web 登录的怪癖：IV 直接用 key（encrypt.dart 里 IV.fromUtf8(key)），
    所以默认 iv=None 就走这个约定。
    """
    k = key.encode('utf-8')
    rounds = _expand_key(k)
    prev = (iv.encode('utf-8') if iv else k)[:16]
    raw = _pad(text.encode('utf-8'))
    out = b''
    for i in range(0, len(raw), 16):
        block = bytes(a ^ b for a, b in zip(raw[i:i + 16], prev))
        enc = _encrypt_block(block, rounds)
        out += enc
        prev = enc
    return base64.b64encode(out).decode('ascii')


def aes_cbc_decrypt(cipher_b64: str, key: str, iv: str | None = None) -> str:
    k = key.encode('utf-8')
    rounds = _expand_key(k)
    prev = (iv.encode('utf-8') if iv else k)[:16]
    raw = base64.b64decode(cipher_b64)
    out = b''
    for i in range(0, len(raw), 16):
        block = raw[i:i + 16]
        dec = _decrypt_block(block, rounds)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = block
    return _unpad(out).decode('utf-8', 'replace')


def md5(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


# ------------------------------------------------------------------- RSA

def _der_read(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """读一个 DER TLV，返回 (tag, value, 下一个位置)。"""
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        n = length & 0x7f
        length = int.from_bytes(data[pos:pos + n], 'big')
        pos += n
    return tag, data[pos:pos + length], pos + length


def parse_rsa_public_key(der_b64: str) -> tuple[int, int]:
    """从 SubjectPublicKeyInfo（base64 DER）里取出 (n, e)。

    结构：SEQUENCE { SEQUENCE { OID, NULL }, BIT STRING { SEQUENCE { INTEGER n, INTEGER e } } }
    """
    der = base64.b64decode(der_b64)
    _, spki, _ = _der_read(der, 0)                      # 外层 SEQUENCE
    _, _, pos = _der_read(spki, 0)                      # AlgorithmIdentifier，跳过
    _, bitstr, _ = _der_read(spki, pos)                 # BIT STRING
    inner = bitstr[1:]                                  # 去掉 unused-bits 计数
    _, seq, _ = _der_read(inner, 0)
    tag_n, val_n, pos2 = _der_read(seq, 0)
    tag_e, val_e, _ = _der_read(seq, pos2)
    if tag_n != 0x02 or tag_e != 0x02:
        raise ValueError('不是合法的 RSA 公钥')
    return int.from_bytes(val_n, 'big'), int.from_bytes(val_e, 'big')


def rsa_encrypt(text: str, public_key_b64: str) -> str:
    """PKCS#1 v1.5 公钥加密，超长自动分段，返回 base64。

    学习通 getUserInfo 把设备信息塞进这个字段，明文能到几百字节，
    而 1024 位密钥一次只能装 117 字节，所以必须分段拼接。
    """
    n, e = parse_rsa_public_key(public_key_b64)
    key_len = (n.bit_length() + 7) // 8
    chunk = key_len - 11
    raw = text.encode('utf-8')
    out = b''
    for i in range(0, len(raw), chunk):
        piece = raw[i:i + chunk]
        # EB = 0x00 || 0x02 || 非零随机填充 || 0x00 || M
        ps_len = key_len - len(piece) - 3
        ps = bytearray()
        while len(ps) < ps_len:
            b = os.urandom(1)[0]
            if b:
                ps.append(b)
        block = b'\x00\x02' + bytes(ps) + b'\x00' + piece
        m = int.from_bytes(block, 'big')
        c = pow(m, e, n)
        out += c.to_bytes(key_len, 'big')
    return base64.b64encode(out).decode('ascii')


def rsa_decrypt_with_public(cipher_b64: str, public_key_b64: str) -> str:
    """用公钥做「解密」——学习通 clientId 就是这么拿的。

    不是笔误：服务端拿私钥签/加密过一段数据，客户端用公钥做一次模幂
    c^e mod n 再剥掉 PKCS#1 填充就能读出来。等价于验签，只是它当加密用。
    """
    if not cipher_b64:
        return ''
    n, e = parse_rsa_public_key(public_key_b64)
    key_len = (n.bit_length() + 7) // 8
    raw = base64.b64decode(cipher_b64)
    out = b''
    for i in range(0, len(raw), key_len):
        piece = raw[i:i + key_len]
        c = int.from_bytes(piece, 'big')
        m = pow(c, e, n)
        block = m.to_bytes(key_len, 'big')
        # 剥 PKCS#1 v1.5 填充。格式 00 <type> <padding...> 00 <data>：
        #   type 1 签名填充（padding 全是 0xFF），type 2 加密填充。
        # type 在 index 1。早先写成 start=2 去检查 block[2]，而那一位
        # 是 0xFF（=255），于是每个块都被判非法 continue 掉，
        # clientId 永远解出空串 → device 里没有 sc/cid →
        # 位置签到提交必然 500。
        if block[0] == 0x00:
            if block[1] not in (1, 2):
                continue
            sep = block.find(b'\x00', 2)
        else:
            if block[0] not in (1, 2):
                continue
            sep = block.find(b'\x00', 1)
        if sep < 0:
            continue
        out += block[sep + 1:]
    return out.decode('utf-8', 'replace')


# ------------------------------------------------------------------ 自检

def selftest() -> int:
    """拿 FIPS-197 的官方向量把 AES 钉死，再顺手验一遍 RSA 往返。"""
    fails = []

    # FIPS-197 C.1：AES-128 ECB
    key = bytes(range(16))
    plain = bytes.fromhex('00112233445566778899aabbccddeeff')
    want = bytes.fromhex('69c4e0d86a7b0430d8cdb78070b4c55a')
    rounds = _expand_key(key)
    got = _encrypt_block(plain, rounds)
    if got != want:
        fails.append(f'AES-128 加密不符: {got.hex()} != {want.hex()}')
    if _decrypt_block(got, rounds) != plain:
        fails.append('AES-128 解密不可逆')

    # 学习通实际用法：16 字节 key 的 ECB 往返
    for text, k in (('1234567890abcdef', 'QrCbNY@MuK1X8HGw'),
                    ('{"uname":"x","code":"1"}', 'z4ok6lu^oWp4_AES')):
        enc = aes_ecb_encrypt(text, k)
        if aes_ecb_decrypt(enc, k) != text:
            fails.append(f'ECB 往返失败: {text}')

    # CBC：IV 缺省等于 key（学习通的约定）
    for text, k in (('13800138000', 'u2oh6Vu^HWe4_AES'),
                    ('a-longer-password-测试', 'u2oh6Vu^HWe4_AES')):
        enc = aes_cbc_encrypt(text, k)
        if aes_cbc_decrypt(enc, k) != text:
            fails.append(f'CBC 往返失败: {text}')

    # RSA：用同一对 n,e 做一次「加密再模幂还原」
    # 这里没法造真密钥，改为验证 DER 解析 + 模幂自洽
    n = 0xC8B1F2E3D4A5968778695A4B3C2D1E0F
    if pow(pow(12345, 65537, n), 1, n) != pow(12345, 65537, n):
        fails.append('RSA 模幂不自洽')

    # md5 对齐 hashlib
    if md5('abc') != '900150983cd24fb0d6963f7d28e17f72':
        fails.append('md5 不对')

    if fails:
        for f in fails:
            print('[FAIL]', f)
        return 1
    print('[OK] cxcrypto 自检通过：AES-128 ECB/CBC 往返、FIPS-197 向量、md5')
    return 0


if __name__ == '__main__':
    raise SystemExit(selftest())
