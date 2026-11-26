"""updater.py —— 检查有没有新版本。

每个用户在自己的设备上直接查，不经任何中转；也**不碰 api.github.com**
（匿名 60 次/小时/IP，校园网 NAT 后面几十个人共用出口，撑不住）。

两路都是「读公开文件」，不限流：

  jsdelivr  仓库里的 web/version.json，公共 CDN，国内可达
  atom      GitHub Releases 的 atom feed，走 github.com 的 CDN，拿 tag

结果缓存 6 小时，写 .config/update.json。检查失败一律静默返回，
不抛异常、不阻断任何功能。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import yktcore as core

SOURCES = (
    ('jsdelivr', 'https://cdn.jsdelivr.net/gh/SnowyFRX/HFUTone@main/web/version.json'),
    ('atom', 'https://github.com/SnowyFRX/HFUTone/releases.atom'),
)
TIMEOUT = 8
# 30 分钟：jsDelivr 不限额，请求又是用户自己的设备发的，没必要压到 6 小时
TTL = 30 * 60
REPO = 'https://github.com/SnowyFRX/HFUTone'


def ver_tuple(text: str) -> tuple:
    """'v1.4.10' -> (1, 4, 10)。

    按数字段比，不能用字符串：'1.4.10' < '1.4.4' 是错的。
    """
    s = str(text or '').strip().lstrip('vV')
    parts = []
    for seg in s.split('.'):
        num = ''
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def local_version(web_dir: Path) -> dict:
    """读随包发布的 version.json（构建时就在 web/ 里）。"""
    try:
        d = json.loads((Path(web_dir) / 'version.json').read_text('utf-8'))
        return {'version': str(d.get('version') or ''),
                'code': int(d.get('code') or 0)}
    except (OSError, ValueError):
        return {'version': '', 'code': 0}


def _fetch(name: str, url: str) -> dict | None:
    accept = 'application/atom+xml' if name == 'atom' else 'application/json'
    r = core.request('GET', url, timeout=TIMEOUT, tag='update',
                     extra_headers={'Accept': accept})
    if r.status != 200 or not r.body:
        return None

    if name == 'atom':
        # atom 里第一个 entry 就是最新 release，直接抠 tag，不解析 XML
        m = re.search(r'/releases/tag/(v?\d+(?:\.\d+)*)', r.body)
        if not m:
            return None
        tag = m.group(1)
        return {'version': tag.lstrip('vV'),
                'release': f'{REPO}/releases/tag/{tag}',
                'notes': ''}

    try:
        d = json.loads(r.body)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None

    ver = str(d.get('version') or '')
    if not ver:
        return None
    return {'version': ver.lstrip('vV'),
            'release': str(d.get('release') or ''),
            'notes': str(d.get('notes') or '')[:300]}


def check(web_dir: Path, force: bool = False) -> dict:
    """查一次版本。force=True 忽略缓存（用户手动点的那一下）。"""
    cur = local_version(web_dir)
    cache = core.CONFIG_DIR / 'update.json'
    now = time.time()

    if not force:
        try:
            hit = json.loads(cache.read_text('utf-8'))
            cached = hit.get('result') or {}
            # 缓存必须和「当前安装的版本」对得上才用：装上新版之后，旧缓存里的
            # current 还是老版本，继续用它就会提示一个早就不存在的"新版本"。
            if (now - float(hit.get('ts') or 0) < TTL
                    and cached
                    and str(cached.get('current') or '') == str(cur['version'] or '')):
                out = dict(cached)
                out['cached'] = True
                return out
        except (OSError, ValueError):
            pass

    errors = []
    seen = []
    cur_t = ver_tuple(cur['version'])
    for name, url in SOURCES:
        try:
            got = _fetch(name, url)
        except Exception as exc:                       # noqa: BLE001
            errors.append(f'{name}:{str(exc)[:40]}')
            continue
        if not got:
            errors.append(f'{name}:无数据')
            continue
        seen.append((name, got))
        # 已经拿到比本地新的版本，后面几路不用再问（省 API 那 60 次额度）
        if ver_tuple(got['version']) > cur_t:
            break

    if not seen:
        return {'ok': False, 'current': cur['version'], 'error': '检查失败',
                'detail': '; '.join(errors)[:200]}

    # 取最高版本而不是「第一路成功的」：jsDelivr 的 @main 有 12 小时缓存，
    # 发版后一段时间它还返回旧版本，直接信它就会误报「已是最新」。
    name, got = max(seen, key=lambda x: ver_tuple(x[1]['version']))

    out = {
        'ok': True,
        'current': cur['version'],
        'latest': got['version'],
        'hasNew': ver_tuple(got['version']) > cur_t,
        'release': got.get('release') or f'{REPO}/releases',
        'notes': got.get('notes') or '',
        'source': name,
        'cached': False,
        'checkedAt': int(now),
    }
    try:
        cache.write_text(json.dumps({'ts': now, 'result': out},
                                    ensure_ascii=False), 'utf-8')
    except OSError:
        pass
    return out


def reset_cache() -> None:
    try:
        (core.CONFIG_DIR / 'update.json').unlink()
    except OSError:
        pass