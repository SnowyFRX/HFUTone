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


def _decode(img) -> list[str]:
    import cv2
    try:
        det = cv2.QRCodeDetector()
        ok, texts, _pts, _ = det.detectAndDecodeMulti(img)
        if ok:
            return [t for t in texts if t]
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
    found = _decode(img)
    if found:
        return found
    # 再来一轮：灰度 + 自适应阈值，屏幕反光/低对比度时明显更容易出码
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for block in (31, 51):
            th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, block, 7)
            found = _decode(th)
            if found:
                return found
    except Exception:                      # noqa: BLE001
        pass
    return []


def pick_ykt(codes: Iterable[str]) -> list[str]:
    """筛出雨课堂域名的码，并去重保序。"""
    out: list[str] = []
    for c in codes or ():
        c = str(c or '').strip()
        if not c:
            continue
        low = c.lower()
        if any(h in low for h in YKT_HOSTS) and c not in out:
            out.append(c)
    return out


def decode_frame(buf: bytes) -> dict:
    """给界面/服务端用的统一入口：返回 {ok, urls, all, note}。"""
    raw = decode_bytes(buf)
    urls = pick_ykt(raw)
    if urls:
        return {'ok': True, 'urls': urls, 'all': raw, 'note': ''}
    if raw:
        return {'ok': False, 'urls': [], 'all': raw,
                'note': '画面里有二维码，但不是雨课堂的码'}
    return {'ok': False, 'urls': [], 'all': [],
            'note': '这一帧里没识别到二维码'}