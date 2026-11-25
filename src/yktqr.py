"""yktqr.py —— 从画面里读雨课堂动态码。

为什么需要它：动态二维码考勤（50070）要的是"把码交给服务器"，不是"装成
App 客户端"。工小湖能签，是因为它用相机扫了码；而我们在电脑上本来就拉着
老师的课件/PGM 画面 —— 老师把码打在大屏上，画面里就有。截一帧、解出来、
交给 /api/v3/app/scan，效果和手机相机完全等价。

实测要点（2026-11-23）：
  * 50070 与 UA / x-client 无关：只有 x-client: app 会触发另一套鉴权，而那套
    鉴权过不了码校验。所以别再往"伪装 App 头"上使力。
  * 二维码里就是一个 URL，服务器（/api/v3/app/scan）负责解析出 lessonId。

解码只用 OpenCV（cv2 >= 4.5 自带 QRCodeDetector），不引 pyzbar/zbar 原生库，
打包时少一个 dll 依赖。小码/低对比度用放大 + 灰度 + 自适应阈值再试一轮。
"""
from __future__ import annotations

from typing import Iterable

# 只认雨课堂自己的域，避免把画面里无关的二维码（公众号、支付码）当成动态码
YKT_HOSTS = ('yuketang.cn', 'yuketang.com', 'changjiang.yuketang')

# 学习通的签到码也长在老师屏幕/课件里，形态是
#   https://mobilelearn.chaoxing.com/widget/sign/e?id=<activeId>&c=<code>&enc=<enc>
# 解码器（OpenCV）本来就能读任何二维码，之前只是被上面那张雨课堂白名单挡在外面。
CX_HOSTS = ('mobilelearn.chaoxing.com',)


def decoder_available() -> bool:
    """本机是否具备二维码图片解码能力（OpenCV + NumPy）。"""
    try:
        import cv2                             # noqa: F401
        import numpy                           # noqa: F401
        return True
    except ImportError:
        return False


def _decode(img) -> list[str]:
    import cv2
    try:
        det = cv2.QRCodeDetector()
        ok, texts, _pts, _ = det.detectAndDecodeMulti(img)
        if ok:
            res = [t for t in texts if t]
            if res:
                return res
    except Exception:                      # noqa: BLE001  OpenCV 版本差异
        pass
    try:
        det = cv2.QRCodeDetector()
        t, _pts, _ = det.detectAndDecode(img)
        return [t] if t else []
    except Exception:                      # noqa: BLE001
        return []


def decode_bytes(buf: bytes, scale: float = 1.0) -> list[str]:
    """从一张 JPEG/PNG 字节流里解出所有二维码文本。

    OpenCV 不是硬依赖：打包时没有 cv2 就返回空，由上层给出"没有解码器"的
    提示，绝不能让取码这一步把签到流程整个打断。
    """
    if not buf:
        return []
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []
    arr = np.frombuffer(buf, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return []
    if scale and scale != 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC)

    # 截图经常紧贴二维码边缘裁剪，缺少白色静区（quiet zone）会导致 OpenCV 解不出；
    # 先补一圈白边，再按原图/多尺度/灰度二值化逐层尝试。
    h, w = img.shape[:2]
    pad = max(16, min(48, min(h, w) // 10))
    padded = cv2.copyMakeBorder(img, pad, pad, pad, pad,
                                cv2.BORDER_CONSTANT, value=(255, 255, 255))

    candidates = [padded, img]
    max_dim = max(h, w)
    if max_dim > 1200:
        f = 900.0 / max_dim
        candidates.append(cv2.resize(padded, None, fx=f, fy=f,
                                     interpolation=cv2.INTER_AREA))
    elif max_dim < 500:
        candidates.append(cv2.resize(padded, None, fx=1.8, fy=1.8,
                                     interpolation=cv2.INTER_CUBIC))

    for cand in candidates:
        found = _decode(cand)
        if found:
            return found

    # 再来一轮：灰度 + Otsu / 自适应阈值，屏幕反光/低对比度/暗色主题截图更容易出码
    try:
        gray = cv2.cvtColor(padded, cv2.COLOR_BGR2GRAY)
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        found = _decode(otsu)
        if found:
            return found
        for block in (31, 51):
            th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, block, 7)
            found = _decode(th)
            if found:
                return found
    except Exception:                      # noqa: BLE001
        pass
    return []


def _pick(codes: Iterable[str], hosts: tuple) -> list[str]:
    """按域名白名单筛码，去重保序。"""
    out: list[str] = []
    for c in codes or ():
        c = str(c or '').strip()
        if not c:
            continue
        low = c.lower()
        if any(h in low for h in hosts) and c not in out:
            out.append(c)
    return out


def pick_ykt(codes: Iterable[str]) -> list[str]:
    """筛出雨课堂域名的码，并去重保序。"""
    return _pick(codes, YKT_HOSTS)


def pick_cx(codes: Iterable[str]) -> list[str]:
    """筛出学习通签到码，并去重保序。"""
    return _pick(codes, CX_HOSTS)


def decode_frame(buf: bytes, hosts: tuple = YKT_HOSTS,
                 label: str = '雨课堂') -> dict:
    """给界面/服务端用的统一入口：返回 {ok, urls, all, note}。

    hosts 决定这次认谁的码：默认只认雨课堂（动态考勤），传 CX_HOSTS 就认
    学习通签到码。画面里同一帧可能同时有别的二维码，白名单是防误判的。
    """
    raw = decode_bytes(buf)
    urls = _pick(raw, hosts)
    if urls:
        return {'ok': True, 'urls': urls, 'all': raw, 'note': ''}
    if raw:
        return {'ok': False, 'urls': [], 'all': raw,
                'note': f'画面里有二维码，但不是{label}的码'}
    return {'ok': False, 'urls': [], 'all': [],
            'note': '这一帧里没识别到二维码'}