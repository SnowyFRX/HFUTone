"""autosign.py —— 方案三后端自动签到引擎（IM 事件驱动秒签 + 后台低频轮询兜底）。

设计原则：
  1. 学习通与雨课堂统一由后端守护线程调度，Windows 桌面端与 Android
     （配合 KeepAliveService 前台服务）共用同一份逻辑，不依赖前端页面活跃。
  2. 学习通优先由环信 IM 推送触发秒级定向签到（on_im_events），低频全量扫描
     退居二线作为兜底（默认 180s + 随机抖动），兼顾响应速度与低频抗风控。
  3. 遇到需要人工交互的环节（验证码 showVCode/validate、手势、签到码、无画面二维码、
     未配置位置坐标）绝不硬试或绕过，只记录一次 need_action 并通过状态面板 /
     可选 Webhook 通知用户人工处理。
  4. 仅使用 Python 标准库，保证 Android Chaquopy 环境零额外依赖。
"""
from __future__ import annotations

import json
import queue
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from typing import Callable

import yktcore as core

# 失败重试上限与退避（秒）
MAX_ATTEMPTS = 3
RETRY_BACKOFF = (60.0, 180.0, 600.0)
# 雨课堂「已进班·待签到」时的复查间隔（秒）
YKT_JOINED_COOLDOWN = 45.0
# 雨课堂轮询基础间隔（秒）
YKT_POLL_BASE = 30.0
# 学习通低频兜底轮询默认间隔（秒）
DEFAULT_POLL_INTERVAL = 180
MIN_POLL_INTERVAL = 60
MAX_POLL_INTERVAL = 600


def _now_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime('%H:%M:%S')


def _build_webhook_payload(url: str, title: str, content: str,
                           level: str = 'info') -> tuple[str, dict]:
    """根据常见通知渠道 URL 形态构造对应的 JSON payload。"""
    low = url.lower()
    text = f'{title}\n{content}' if content else title

    if 'oapi.dingtalk.com/robot/send' in low:
        return url, {'msgtype': 'text', 'text': {'content': text}}
    if 'qyapi.weixin.qq.com/cgi-bin/webhook/send' in low:
        return url, {'msgtype': 'text', 'text': {'content': text}}
    if 'open.feishu.cn/open-apis/bot/v2/hook' in low or 'open.larksuite.com' in low:
        return url, {'msg_type': 'text', 'content': {'text': text}}
    if 'sctapi.ftqq.com' in low:
        return url, {'title': title, 'desp': content or title}
    if 'pushplus.plus' in low:
        parsed = urllib.parse.urlsplit(url)
        qs = urllib.parse.parse_qs(parsed.query)
        payload = {'title': title, 'content': content or title}
        tok = (qs.get('token') or [''])[0]
        if tok:
            payload['token'] = tok
        return url, payload
    if 'api.day.app' in low:
        return url, {'title': title, 'body': content or title}

    return url, {
        'title': title,
        'content': content,
        'level': level,
        'source': 'HFUTone',
        'timestamp': int(time.time()),
    }


def send_webhook(url: str, title: str, content: str,
                 level: str = 'info', timeout: float = 8.0) -> tuple[bool, str]:
    """向用户配置的 Webhook 发送文本通知。仅支持 http(s)，失败不抛异常。"""
    u = str(url or '').strip()
    if not u:
        return False, '未配置 Webhook 地址'
    parsed = urllib.parse.urlsplit(u)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return False, 'Webhook 地址必须以 http:// 或 https:// 开头'

    target, payload = _build_webhook_payload(u, title, content, level=level)
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        target,
        data=body,
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'HFUTone-AutoSign/1.0',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, 'status', 200)
            if 200 <= status < 300:
                return True, f'HTTP {status}'
            return False, f'HTTP {status}'
    except urllib.error.HTTPError as exc:
        return False, f'HTTP {exc.code}'
    except Exception as exc:                             # noqa: BLE001
        return False, str(exc)[:120]


class AutoSignEngine:
    """学习通（IM 秒签 + 低频轮询）与雨课堂（后台轮询）统一自动签到引擎。"""

    def __init__(
        self,
        get_settings: Callable[[], dict],
        get_cx: Callable[[], object],
        get_ykt: Callable[[], object],
        qr_grabber: Callable[..., dict] | None = None,
        webhook_sender: Callable[..., tuple[bool, str]] = send_webhook,
        clock: Callable[[], float] = time.time,
        rng: Callable[[float, float], float] = random.uniform,
    ):
        self._get_settings = get_settings
        self._get_cx = get_cx
        self._get_ykt = get_ykt
        self._qr_grabber = qr_grabber
        self._webhook_sender = webhook_sender
        self._clock = clock
        self._rng = rng

        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._im_queue: queue.Queue[tuple[dict, bool]] = queue.Queue(maxsize=256)
        self._started = False

        # 事件环形日志（最新在右，输出时倒序给前端）
        self._events: deque[dict] = deque(maxlen=60)
        self._event_seq = 0

        # 每个活动的去重、冷却与重试状态
        self._cx_track: dict[str, dict] = {}
        self._ykt_track: dict[str, dict] = {}

        # 运行统计与时间点
        self._last_cx_poll = 0.0
        self._last_ykt_poll = 0.0
        self._next_cx_poll = 0.0
        self._next_ykt_poll = 0.0
        self._stats = {
            'signed': 0,
            'needAction': 0,
            'failed': 0,
            'imTriggered': 0,
        }

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        with self.lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
        threading.Thread(target=self._im_loop, name='autosign-im', daemon=True).start()
        threading.Thread(target=self._poll_loop, name='autosign-poll', daemon=True).start()
        core.log('[自动签到] 后端引擎已启动（IM 事件秒签 + 低频轮询兜底）')

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        try:
            self._im_queue.put_nowait(({}, False))
        except queue.Full:
            pass

    def notify_config_changed(self) -> None:
        """设置变更时立刻唤醒轮询线程，让新开关/间隔即时生效。"""
        with self.lock:
            self._next_cx_poll = 0.0
            self._next_ykt_poll = 0.0
        self._wake.set()

    def trigger_now(self, platform: str = 'all') -> dict:
        """手动触发一次即时检查（异步或供 API 同步调用）。"""
        p = (platform or 'all').strip().lower()
        out = {'ok': True, 'cx': None, 'ykt': None}
        if p in ('all', 'cx'):
            out['cx'] = self.poll_cx_once(trigger='手动触发')
        if p in ('all', 'ykt'):
            out['ykt'] = self.poll_ykt_once(trigger='手动触发')
        return out

    def clear_events(self) -> None:
        with self.lock:
            self._events.clear()
            self._recount_need_action_locked()

    def _recount_need_action_locked(self) -> None:
        self._stats['needAction'] = sum(
            1 for ev in self._events if ev.get('status') == 'need_action'
        )

    def mark_resolved(self, platform: str, ref_id: str,
                      detail: str = '已完成签到', count_signed: bool = False) -> int:
        """手动签到成功或后续扫描确认已签后，同步将对应「需人工」记录翻为已签。"""
        ref = str(ref_id or '').strip()
        if not ref:
            return 0
        updated = 0
        now = self._clock()
        with self.lock:
            track = self._cx_track if platform == 'cx' else self._ykt_track
            st = track.setdefault(ref, {
                'done': False, 'attempts': 0, 'next_try': 0.0, 'notified_need': False,
            })
            was_done = bool(st.get('done'))
            st['done'] = True
            for ev in self._events:
                if ev.get('platform') == platform and ev.get('refId') == ref and ev.get('status') == 'need_action':
                    ev['status'] = 'signed'
                    ev['detail'] = detail
                    ev['time'] = _now_str(now)
                    ev.pop('item', None)
                    updated += 1
            if count_signed and not was_done:
                self._stats['signed'] += 1
            self._recount_need_action_locked()
        return updated

    def sync_cx_signs(self, signs: list[dict]) -> None:
        """扫描结果回来后，把已经签上或已从进行中列表消失的「需人工」记录同步消掉。"""
        live_unsigned: set[str] = set()
        for s in signs or []:
            aid = str(s.get('activeId') or '')
            if not aid:
                continue
            if s.get('signed') or self._cx_expired(s):
                self.mark_resolved('cx', aid, detail='已签到（状态已同步）', count_signed=False)
            else:
                live_unsigned.add(aid)

        with self.lock:
            pending_ids = [
                ev.get('refId') for ev in self._events
                if ev.get('platform') == 'cx'
                and ev.get('status') == 'need_action'
                and ev.get('refId')
                and ev.get('refId') not in live_unsigned
            ]
        if not pending_ids:
            return
        cx_state = self._get_cx()
        signer = getattr(cx_state, 'signer', None)
        api = getattr(signer, 'api', None)
        for aid in set(pending_ids):
            try:
                attend = api.attend_info(aid) if api and hasattr(api, 'attend_info') else {}
                st = int(attend.get('status') or 0)
            except Exception:                            # noqa: BLE001
                st = 0
            if st != 0:
                self.mark_resolved('cx', aid, detail='已完成签到（已同步）', count_signed=False)
            else:
                self.mark_resolved('cx', aid, detail='签到已结束', count_signed=False)

    # ------------------------------------------------------------ 配置与通知

    def _cfg(self) -> dict:
        try:
            return self._get_settings() or {}
        except Exception:                                # noqa: BLE001
            return {}

    def _cx_enabled(self, cfg: dict | None = None) -> bool:
        c = cfg if cfg is not None else self._cfg()
        return bool((c.get('cx') or {}).get('autoSign'))

    def _ykt_enabled(self, cfg: dict | None = None) -> bool:
        c = cfg if cfg is not None else self._cfg()
        return bool((c.get('ykt') or {}).get('autoSign'))

    def _poll_interval(self, cfg: dict | None = None) -> int:
        c = cfg if cfg is not None else self._cfg()
        raw = (c.get('autoSign') or {}).get('pollIntervalSec', DEFAULT_POLL_INTERVAL)
        try:
            val = int(raw)
        except (TypeError, ValueError):
            val = DEFAULT_POLL_INTERVAL
        return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, val))

    def _webhook_url(self, cfg: dict | None = None) -> str:
        c = cfg if cfg is not None else self._cfg()
        return str((c.get('autoSign') or {}).get('webhookUrl') or '').strip()

    def _record(self, platform: str, status: str, name: str, detail: str,
                trigger: str = '轮询', notify: bool = False,
                ref_id: str = '', item: dict | None = None) -> dict:
        now = self._clock()
        ref = str(ref_id or '')
        with self.lock:
            self._event_seq += 1
            ev = {
                'id': self._event_seq,
                'ts': int(now),
                'time': _now_str(now),
                'platform': platform,       # 'cx' | 'ykt' | 'system'
                'status': status,           # 'signed' | 'already' | 'need_action' | 'failed' | 'info'
                'name': name,
                'detail': detail,
                'trigger': trigger,
                'refId': ref,
            }
            if status == 'need_action' and isinstance(item, dict):
                ev['item'] = {
                    'activeId': str(item.get('activeId') or ref),
                    'courseId': str(item.get('courseId') or ''),
                    'classId': str(item.get('classId') or ''),
                    'courseName': str(item.get('courseName') or ''),
                    'name': str(item.get('name') or ''),
                    'url': str(item.get('url') or ''),
                    'signType': str(item.get('signType') or 'normal'),
                    'needCaptcha': bool(item.get('needCaptcha')),
                    'needFace': bool(item.get('needFace')),
                    'needPhoto': bool(item.get('needPhoto')),
                    'numberCount': int(item.get('numberCount') or 0),
                    'missing': list(item.get('missing') or []),
                }
            self._events.append(ev)
            if status == 'signed':
                self._stats['signed'] += 1
            elif status == 'failed':
                self._stats['failed'] += 1
            self._recount_need_action_locked()

        core.log(f'[自动签到][{platform}][{trigger}] {name}: {status} - {detail}')

        if notify:
            url = self._webhook_url()
            if url:
                plat_label = {'cx': '学习通', 'ykt': '雨课堂'}.get(platform, 'HFUTone')
                st_label = {
                    'signed': '签到成功',
                    'need_action': '需要人工介入',
                    'failed': '签到失败',
                }.get(status, status)
                title = f'[HFUTone] {plat_label}{st_label}：{name}'
                content = f'触发方式：{trigger}\n详情：{detail}\n时间：{_now_str(now)}'
                # 异步发送 Webhook，避免慢网阻塞签到线程
                threading.Thread(
                    target=self._send_webhook_safe,
                    args=(url, title, content, status),
                    daemon=True,
                ).start()
        return ev

    def _send_webhook_safe(self, url: str, title: str, content: str, level: str) -> None:
        ok, msg = self._webhook_sender(url, title, content, level)
        if not ok:
            core.log(f'[自动签到] Webhook 推送失败：{msg}')

    def test_webhook(self, url: str = '') -> tuple[bool, str]:
        target = str(url or '').strip() or self._webhook_url()
        return self._webhook_sender(
            target,
            '[HFUTone] 自动签到推送测试',
            f'如果你看到这条消息，说明 Webhook 推送已连通（{_now_str(self._clock())}）。',
            'info',
        )

    # ------------------------------------------------------------ IM 事件入口

    def on_im_events(self, items: list[dict], realtime: bool = True) -> int:
        """收到前端环信 Web SDK 上报的群聊签到活动后立刻入队，由秒签线程处理。"""
        if not items or not self._cx_enabled():
            return 0
        cx_state = self._get_cx()
        if not getattr(cx_state, 'logged_in', False):
            return 0

        queued = 0
        for raw in items:
            if not isinstance(raw, dict):
                continue
            aid = str(raw.get('activeId') or raw.get('aid') or '').strip()
            if not aid:
                continue
            with self.lock:
                st = self._cx_track.get(aid)
                if st and st.get('done'):
                    continue
            try:
                self._im_queue.put_nowait((dict(raw), bool(realtime)))
                queued += 1
            except queue.Full:
                core.log('[自动签到] IM 事件队列已满，丢弃冗余事件')
                break
        return queued

    def _im_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw, realtime = self._im_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if self._stop.is_set() or not raw:
                continue
            try:
                self.process_im_item(raw, realtime=realtime)
            except Exception as exc:                     # noqa: BLE001
                core.log(f'[自动签到] IM 秒签异常：{exc}')

    def process_im_item(self, raw: dict, realtime: bool = True) -> dict | None:
        """定向解析并签掉单条 IM 推送过来的学习通签到活动（无需全量扫 27 门课）。"""
        if not self._cx_enabled():
            return None
        cx_state = self._get_cx()
        if not getattr(cx_state, 'logged_in', False):
            return None

        aid = str(raw.get('activeId') or raw.get('aid') or '').strip()
        if not aid:
            return None

        signer = cx_state.signer
        cx_state.sync_location()

        course = {
            'courseId': str(raw.get('courseId') or ''),
            'classId': str(raw.get('classId') or ''),
            'cpi': '',
            'name': str(raw.get('courseName') or ''),
        }
        act = {
            'activeId': aid,
            'name': str(raw.get('title') or raw.get('atypeName') or '签到'),
            'desc': str(raw.get('atypeName') or ''),
            'url': str(raw.get('url') or ''),
            'startTime': 0,
            'endTime': 0,
            'signType': '',
            'status': 0,
        }

        try:
            item = signer._describe(course, act)
        except Exception as exc:                         # noqa: BLE001
            core.log(f'[自动签到] IM 活动 {aid} 预检失败：{exc}')
            return None

        # 补齐 courseId（有些群聊卡片只在 active_info 里给）
        if not item.get('courseId'):
            try:
                info = signer.api.active_info(aid)
                if info.get('courseId'):
                    item['courseId'] = str(info.get('courseId'))
                if not item.get('url') and info.get('url'):
                    item['url'] = str(info.get('url'))
            except Exception:                            # noqa: BLE001
                pass

        trigger = 'IM秒签' if realtime else 'IM漫游'
        if realtime:
            with self.lock:
                self._stats['imTriggered'] += 1
        return self._handle_cx_item(item, trigger=trigger, allow_notify_need=realtime)

    # ------------------------------------------------------------ 学习通单条处理

    def _cx_expired(self, item: dict) -> bool:
        end_ts = int(item.get('endTime') or 0)
        return bool(end_ts) and self._clock() * 1000 > end_ts

    def _handle_cx_item(self, item: dict, trigger: str,
                        allow_notify_need: bool = True) -> dict | None:
        aid = str(item.get('activeId') or '')
        if not aid:
            return None
        label = (f"{item.get('courseName') or '学习通'} · {item.get('name') or '签到'}").strip(' ·')

        if item.get('signed'):
            self.mark_resolved('cx', aid, detail='已签到（状态已同步）', count_signed=False)
            return {'status': 'already', 'activeId': aid}

        if self._cx_expired(item):
            self.mark_resolved('cx', aid, detail='签到已结束', count_signed=False)
            return {'status': 'expired', 'activeId': aid}

        now = self._clock()
        with self.lock:
            tr = self._cx_track.setdefault(aid, {
                'done': False, 'attempts': 0, 'next_try': 0.0, 'notified_need': False,
            })
            if tr['done']:
                return {'status': 'already', 'activeId': aid}
            if tr['next_try'] and now < tr['next_try']:
                return {'status': 'cooldown', 'activeId': aid}
            if tr['attempts'] >= MAX_ATTEMPTS:
                return {'status': 'exhausted', 'activeId': aid}

        # 需要验证码或手势/签到码/二维码/坐标：绝不绕过，通知用户人工在界面完成
        if not item.get('canAuto'):
            missing = list(item.get('missing') or [])
            if item.get('needCaptcha') and '验证码' not in missing:
                missing.append('验证码')
            why = '需人工完成：' + (' / '.join(missing) if missing else '不支持全自动')
            should_notify = False
            with self.lock:
                tr = self._cx_track[aid]
                if not tr.get('notified_need') and allow_notify_need:
                    tr['notified_need'] = True
                    should_notify = True
            if should_notify:
                self._record('cx', 'need_action', label, why, trigger=trigger,
                             notify=True, ref_id=aid, item=item)
            return {'status': 'need_action', 'activeId': aid, 'detail': why}

        # 全自动类型（普通 / 已配坐标的位置签到，且无验证码）
        cx_state = self._get_cx()
        cx_state.sync_location()
        try:
            res = cx_state.signer.sign(item)
        except Exception as exc:                         # noqa: BLE001
            res = {'ok': False, 'status': 'failed', 'detail': str(exc)[:120], 'needs': []}

        if res.get('ok'):
            self.mark_resolved('cx', aid, detail=res.get('detail') or '签到成功', count_signed=False)
            self._record('cx', 'signed', label, res.get('detail') or '签到成功',
                         trigger=trigger, notify=True, ref_id=aid)
            return {'status': 'signed', 'activeId': aid, 'detail': res.get('detail') or ''}

        # 服务端动态要求验证码或额外输入：立即停止重试，通知人工处理（不绕过）
        needs = list(res.get('needs') or [])
        if res.get('status') == 'need' or needs:
            why = '需人工完成：' + (' / '.join(needs) if needs else (res.get('detail') or '验证'))
            should_notify = False
            with self.lock:
                tr = self._cx_track[aid]
                tr['attempts'] = MAX_ATTEMPTS   # 不再自动重打
                if not tr.get('notified_need'):
                    tr['notified_need'] = True
                    should_notify = True
            if should_notify:
                item_snap = dict(item)
                if '验证码' in needs:
                    item_snap['needCaptcha'] = True
                self._record('cx', 'need_action', label, why, trigger=trigger,
                             notify=True, ref_id=aid, item=item_snap)
            return {'status': 'need_action', 'activeId': aid, 'detail': why}

        # 普通失败：退避重试
        with self.lock:
            tr = self._cx_track[aid]
            tr['attempts'] += 1
            idx = min(tr['attempts'] - 1, len(RETRY_BACKOFF) - 1)
            tr['next_try'] = now + RETRY_BACKOFF[idx]
            exhausted = tr['attempts'] >= MAX_ATTEMPTS
        detail = res.get('detail') or '签到失败'
        self._record('cx', 'failed', label, detail, trigger=trigger,
                     notify=exhausted, ref_id=aid)
        return {'status': 'failed', 'activeId': aid, 'detail': detail}

    # ------------------------------------------------------------ 轮询兜底

    def poll_cx_once(self, trigger: str = '低频轮询') -> dict:
        if not self._cx_enabled():
            return {'ok': True, 'skipped': 'disabled'}
        cx_state = self._get_cx()
        if not getattr(cx_state, 'logged_in', False):
            return {'ok': True, 'skipped': 'not_logged_in'}

        self._last_cx_poll = self._clock()
        data = cx_state.scan(force=True)
        if not data or not data.get('ok'):
            return {'ok': False, 'error': (data or {}).get('error') or '扫描失败'}

        signs_list = data.get('signs') or []
        self.sync_cx_signs(signs_list)
        signed = need = failed = 0
        for item in signs_list:
            r = self._handle_cx_item(item, trigger=trigger, allow_notify_need=True)
            if not r:
                continue
            if r['status'] == 'signed':
                signed += 1
            elif r['status'] == 'need_action':
                need += 1
            elif r['status'] == 'failed':
                failed += 1
        return {'ok': True, 'signed': signed, 'needAction': need, 'failed': failed}

    def poll_ykt_once(self, trigger: str = '雨课堂轮询') -> dict:
        cfg = self._cfg()
        if not self._ykt_enabled(cfg):
            return {'ok': True, 'skipped': 'disabled'}
        ykt_state = self._get_ykt()
        if not getattr(ykt_state, 'logged_in', False):
            return {'ok': True, 'skipped': 'not_logged_in'}
        if getattr(ykt_state, 'busy', False):
            return {'ok': True, 'skipped': 'busy'}

        self._last_ykt_poll = self._clock()
        try:
            ykt_state.refresh(force=True)
        except Exception as exc:                         # noqa: BLE001
            return {'ok': False, 'error': str(exc)[:120]}

        with ykt_state.lock:
            lessons = [dict(r) for r in (ykt_state.lessons or [])]

        sign_mode = ((cfg.get('ykt') or {}).get('signMode') or 'app')
        signed = need = failed = 0
        now = self._clock()

        for row in lessons:
            lid = str(row.get('lessonId') or '')
            if not lid:
                continue
            name = str(row.get('name') or row.get('courseName') or f'课堂 {lid}')
            if row.get('checkedAt') or row.get('signinStatus'):
                self.mark_resolved('ykt', lid, detail='已签到（状态已同步）', count_signed=False)
                continue

            with self.lock:
                tr = self._ykt_track.setdefault(lid, {
                    'done': False, 'attempts': 0, 'next_try': 0.0, 'notified_need': False,
                })
                if tr['done'] or (tr['next_try'] and now < tr['next_try']):
                    continue
                if tr['attempts'] >= MAX_ATTEMPTS:
                    continue

            try:
                res = ykt_state.ykt.signin_full(
                    lid,
                    str(row.get('classroomId') or ''),
                    name,
                    allow_wait=False,
                    signin_status=row.get('signinStatus') or 0,
                    sign_mode=sign_mode,
                )
            except Exception as exc:                     # noqa: BLE001
                res = {'ok': False, 'status': 'failed', 'detail': str(exc)[:120]}

            # 桌面端有直播画面时，动态码兜底截帧
            if res.get('status') == 'need_scan' and self._qr_grabber is not None:
                try:
                    grab = self._qr_grabber()
                    if grab and grab.get('ok') and grab.get('urls'):
                        res = ykt_state.ykt.signin_full(
                            lid,
                            str(row.get('classroomId') or ''),
                            name,
                            allow_wait=False,
                            signin_status=row.get('signinStatus') or 0,
                            sign_mode='scan',
                            qr_url=grab['urls'][0],
                        )
                except Exception:                        # noqa: BLE001
                    pass

            st = str(res.get('status') or 'failed')
            detail = str(res.get('detail') or '')

            if st in ('checked', 'already'):
                self.mark_resolved('ykt', lid, detail=detail or '签到成功', count_signed=False)
                # 同步回写 YKT.lessons，前端红点立即更新
                try:
                    checked_at = ykt_state.ykt.store.load_checked().get(lid, '')
                except Exception:                        # noqa: BLE001
                    checked_at = _now_str(now)
                with ykt_state.lock:
                    for item in ykt_state.lessons:
                        if str(item.get('lessonId') or '') == lid:
                            item['checkedAt'] = checked_at or _now_str(now)
                            item['signinStatus'] = 1
                self._record('ykt', 'signed', name, detail or '签到成功',
                             trigger=trigger, notify=True, ref_id=lid)
                signed += 1
            elif st == 'joined':
                # 老师尚未发起签到，不计失败，等待下次复查
                with self.lock:
                    self._ykt_track[lid]['next_try'] = now + YKT_JOINED_COOLDOWN
            elif st in ('need_scan', 'need_location'):
                should_notify = False
                with self.lock:
                    tr = self._ykt_track[lid]
                    tr['next_try'] = now + 120.0
                    if not tr.get('notified_need'):
                        tr['notified_need'] = True
                        should_notify = True
                if should_notify:
                    self._record('ykt', 'need_action', name,
                                 detail or '需要手动扫码或补充位置',
                                 trigger=trigger, notify=True, ref_id=lid)
                need += 1
            else:
                with self.lock:
                    tr = self._ykt_track[lid]
                    tr['attempts'] += 1
                    idx = min(tr['attempts'] - 1, len(RETRY_BACKOFF) - 1)
                    tr['next_try'] = now + RETRY_BACKOFF[idx]
                    exhausted = tr['attempts'] >= MAX_ATTEMPTS
                self._record('ykt', 'failed', name, detail or '签到失败',
                             trigger=trigger, notify=exhausted, ref_id=lid)
                failed += 1

        return {'ok': True, 'signed': signed, 'needAction': need, 'failed': failed}

    def _jitter(self, base: float) -> float:
        factor = self._rng(0.85, 1.15)
        return max(10.0, base * factor)

    def _poll_loop(self) -> None:
        # 启动后稍等 5 秒，等登录态/IM 连接预热
        self._wake.wait(timeout=5.0)
        while not self._stop.is_set():
            self._wake.clear()
            now = self._clock()
            cfg = self._cfg()
            cx_on = self._cx_enabled(cfg)
            ykt_on = self._ykt_enabled(cfg)
            cx_interval = float(self._poll_interval(cfg))

            if ykt_on and now >= self._next_ykt_poll:
                try:
                    self.poll_ykt_once()
                except Exception as exc:                 # noqa: BLE001
                    core.log(f'[自动签到] 雨课堂轮询异常：{exc}')
                self._next_ykt_poll = self._clock() + self._jitter(YKT_POLL_BASE)

            if cx_on and now >= self._next_cx_poll:
                try:
                    self.poll_cx_once()
                except Exception as exc:                 # noqa: BLE001
                    core.log(f'[自动签到] 学习通轮询异常：{exc}')
                self._next_cx_poll = self._clock() + self._jitter(cx_interval)

            # 计算下次唤醒等待时间
            now2 = self._clock()
            waits = [15.0]
            if ykt_on and self._next_ykt_poll > now2:
                waits.append(self._next_ykt_poll - now2)
            if cx_on and self._next_cx_poll > now2:
                waits.append(self._next_cx_poll - now2)
            wait_sec = max(1.0, min(waits))
            self._wake.wait(timeout=wait_sec)

    # ------------------------------------------------------------ 状态快照

    def status_payload(self) -> dict:
        cfg = self._cfg()
        with self.lock:
            events = list(reversed(self._events))
            stats = dict(self._stats)
            running = self._started and not self._stop.is_set()
        return {
            'ok': True,
            'running': running,
            'cxAutoSign': self._cx_enabled(cfg),
            'yktAutoSign': self._ykt_enabled(cfg),
            'pollIntervalSec': self._poll_interval(cfg),
            'webhookConfigured': bool(self._webhook_url(cfg)),
            'imQueued': self._im_queue.qsize(),
            'lastCxPoll': int(self._last_cx_poll),
            'lastYktPoll': int(self._last_ykt_poll),
            'stats': stats,
            'events': events[:30],
        }
