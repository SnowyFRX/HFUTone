"""updater.py —— 检查有没有新版本。

三道公开源，全是公共设施，不依赖任何自建服务：

  jsdelivr  仓库里的 web/version.json，公共 CDN，国内可达、没有限额
  raw       GitHub raw，同一份文件的备用入口
  api       releases/latest，结构化最准，但匿名只有 60 次/小时/IP，
            校园网 NAT 后面几十个人共用出口，几小时就能打光——所以它只做兜底

结果缓存 6 小时，写 .config/update.json。检查失败一律静默返回，
不抛异常、不阻断任何功能。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import yktcore as core

SOURCES = (
    ('jsdelivr', 'https://cdn.jsdelivr.net/gh/SnowyFRX/HFUTone@main/web/version.json'),
    ('raw', 'https://raw.githubusercontent.com/SnowyFRX/HFUTone/main/web/version.json'),
    ('api', 'https://api.github.com/repos/SnowyFRX/HFUTone/releases/latest'),
)
TIMEOUT = 8
TTL = 6 * 3600
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
    r = core.request('GET', url, timeout=TIMEOUT, tag='update',
                     extra_headers={'Accept': 'application/json'})
    if r.status != 200 or not r.body:
        return None
    try:
        d = json.loads(r.body)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None

    if name == 'api':
        tag = str(d.get('tag_name') or '')
        if not tag:
            return None
        return {'version': tag.lstrip('vV'),
                'release': f'{REPO}/releases/tag/{tag}',
                'notes': str(d.get('body') or '')[:300]}
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
            if now - float(hit.get('ts') or 0) < TTL and hit.get('result'):
                out = dict(hit['result'])
                out['cached'] = True
                return out
        except (OSError, ValueError):
            pass

    errors = []
    for name, url in SOURCES:
        try:
            got = _fetch(name, url)
        except Exception as exc:                       # noqa: BLE001
            errors.append(f'{name}:{str(exc)[:40]}')
            continue
        if not got:
            errors.append(f'{name}:无数据')
            continue

        out = {
            'ok': True,
            'current': cur['version'],
            'latest': got['version'],
            'hasNew': ver_tuple(got['version']) > ver_tuple(cur['version']),
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

    return {'ok': False, 'current': cur['version'], 'error': '检查失败',
            'detail': '; '.join(errors)[:200]}


def reset_cache() -> None:
    try:
        (core.CONFIG_DIR / 'update.json').unlink()
    except OSError:
        pass