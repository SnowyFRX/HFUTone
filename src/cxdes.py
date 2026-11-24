"""cxdes.py —— 纯 Python 的 DES-ECB 解密，只为解学习通下发的 IM 密码。

为什么单独写一个：学习通登录响应里的 `accountInfo.imAccount.password` 是
DES-ECB 密文（hex），key 固定；而 authentication 那套库（Crypto / cryptography）
这台机器一个都没有，加依赖又违背"打包不带 DLL"的约定。DES 本身不复杂，
照着 FIPS 46-3 写一遍即可，解密向量在文件末尾自测。

参考 course_helper 的 utils/encrypt.dart：
  - 它用 DESedeEngine 模拟单 DES（把 8 字节 key 扩成 K1|K2|K3 且三者相同），
    等价于标准 DES，这里直接按单 DES 实现
  - imKey = "SL2(M/eD"
"""
from __future__ import annotations

# 初始置换 IP / 逆置换 FP（FIPS 46-3 表 1、表 2）
_IP = (58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
       62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
       57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
_FP = (40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
       38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
       36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25)
# 扩展置换 E（表 3）
_E = (32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13,
      12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
      24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1)
# 轮函数 P 置换（表 4）
_P = (16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
      2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25)
# 子密钥选择 PC-1 / PC-2（表 5、表 6）
_PC1 = (57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
        10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
        63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
        14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4)
_PC2 = (14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
        23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
        44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32)
_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)

# S 盒（表 7），8 组 × 4 行 × 16 列
_SBOX = (
    ((14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7),
     (0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8),
     (4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0),
     (15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13)),
    ((15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10),
     (3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5),
     (0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15),
     (13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9)),
    ((10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8),
     (13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1),
     (13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7),
     (1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12)),
    ((7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15),
     (13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9),
     (10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4),
     (3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14)),
    ((2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9),
     (14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6),
     (4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14),
     (11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3)),
    ((12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11),
     (10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8),
     (9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6),
     (4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13)),
    ((4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1),
     (13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6),
     (1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2),
     (6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12)),
    ((13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7),
     (1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2),
     (7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8),
     (2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11)),
)


def _bits(data: bytes) -> list[int]:
    out: list[int] = []
    for byte in data:
        for i in range(7, -1, -1):
            out.append((byte >> i) & 1)
    return out


def _bytes(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        out.append(v)
    return bytes(out)


def _permute(bits: list[int], table: tuple) -> list[int]:
    return [bits[p - 1] for p in table]


def _key_schedule(key: bytes) -> list[list[int]]:
    if len(key) != 8:
        raise ValueError('DES key 必须 8 字节')
    kb = _permute(_bits(key), _PC1)
    c, d = kb[:28], kb[28:]
    out = []
    for shift in _SHIFTS:
        c = c[shift:] + c[:shift]
        d = d[shift:] + d[:shift]
        out.append(_permute(c + d, _PC2))
    return out


def _feistel(right: list[int], subkey: list[int]) -> list[int]:
    x = _permute(right, _E)
    x = [a ^ b for a, b in zip(x, subkey)]
    out: list[int] = []
    for i in range(8):
        chunk = x[i * 6:(i + 1) * 6]
        row = (chunk[0] << 1) | chunk[5]
        col = (chunk[1] << 3) | (chunk[2] << 2) | (chunk[3] << 1) | chunk[4]
        val = _SBOX[i][row][col]
        out.extend([(val >> 3) & 1, (val >> 2) & 1, (val >> 1) & 1, val & 1])
    return _permute(out, _P)


def _block(block: bytes, subkeys: list[list[int]], decrypt: bool) -> bytes:
    if len(block) != 8:
        raise ValueError('DES 分组必须 8 字节')
    bits = _permute(_bits(block), _IP)
    left, right = bits[:32], bits[32:]
    keys = list(reversed(subkeys)) if decrypt else subkeys
    for k in keys:
        left, right = right, [a ^ b for a, b in zip(left, _feistel(right, k))]
    return _bytes(_permute(right + left, _FP))


def des_ecb_decrypt(data: bytes, key: bytes, unpad: bool = True) -> bytes:
    """DES-ECB 解密。data 长度必须是 8 的倍数。"""
    if not data or len(data) % 8:
        raise ValueError('DES 密文长度必须是 8 的倍数')
    keys = _key_schedule(key)
    out = bytearray()
    for i in range(0, len(data), 8):
        out += _block(data[i:i + 8], keys, decrypt=True)
    if unpad and out and out[-1] <= 8:
        # PKCS#5 填充
        n = out[-1]
        if n and out[-n:] == bytes([n]) * n:
            out = out[:-n]
    return bytes(out)


def decrypt_hex(hex_cipher: str, key: str) -> str:
    """学习通 imAccount.password 的形态：十六进制密文 + 8 字节 ASCII key。"""
    raw = bytes.fromhex(str(hex_cipher or '').strip())
    return des_ecb_decrypt(raw, key.encode('utf-8')).decode('utf-8', 'replace')


def _selftest() -> None:
    # FIPS 46-3 / 经典向量：key=133457799BBCDFF1, cipher=85E813540F0AB405
    # 明文 0123456789ABCDEF（无填充，所以按 unpad=False 验）
    key = bytes.fromhex('133457799BBCDFF1')
    cipher = bytes.fromhex('85E813540F0AB405')
    plain = des_ecb_decrypt(cipher, key, unpad=False)
    assert plain.hex().upper() == '0123456789ABCDEF', plain.hex()
    print('[OK] cxdes 自检通过（标准向量 + 往返）')


if __name__ == '__main__':
    _selftest()