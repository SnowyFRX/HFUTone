"""updater 的自检：版本比较、三路降级、缓存命中。

不联网：所有请求都替换成假响应。真实可达性靠 build 后的手动检查，
这里只保证逻辑正确。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import updater

fails = []


def check(cond, msg):
    if cond:
        print('  [通过]', msg)
    else:
        fails.append(msg)
        print('  [FAIL]', msg)


class Resp:
    def __init__(self, status=200, body=''):
        self.status = status
        self.body = body


print('--- 1. 版本比较按数字段，不按字符串 ---')
check(updater.ver_tuple('v1.4.10') > updater.ver_tuple('1.4.4'), '1.4.10 > 1.4.4')
check(updater.ver_tuple('1.4.4') == updater.ver_tuple('v1.4.4'), 'v 前缀只剥一次')
check(updater.ver_tuple('1.4') < updater.ver_tuple('1.4.1'), '缺位补 0')
check(updater.ver_tuple('') == (0,), '空串不炸')

print('--- 2. 降级：jsdelivr 挂了走后面几路 ---')
tmp = Path(tempfile.mkdtemp())
updater.core.CONFIG_DIR = tmp
(Path('web') / 'version.json').exists() or print('  [警告] 缺 web/version.json')

calls = []


def fake(method, url, **kw):
    calls.append(url)
    if 'jsdelivr' in url:
        return Resp(0, '')                      # 主路挂掉
    if 'releases.atom' in url:                  # 备用路：atom feed
        return Resp(200, '<feed><entry><link href="https://github.com/SnowyFRX/'
                         'HFUTone/releases/tag/v1.5.0"/></entry></feed>')
    return Resp(0, '')


updater.core.request = fake
out = updater.check(Path('web'), force=True)
check(out.get('ok') is True, '整体 ok')
check(out.get('source') == 'atom', '降级到 atom（实际 %s）' % out.get('source'))
check(out.get('hasNew') is True, '1.5.0 比本地新')
check('jsdelivr' in calls[0] and any('atom' in c for c in calls), '按顺序尝试')

print('--- 3. 缓存：6 小时内不再请求 ---')
calls.clear()
again = updater.check(Path('web'), force=False)
check(again.get('cached') is True, '第二次命中缓存')
check(not calls, '缓存命中时没有发请求')
forced = updater.check(Path('web'), force=True)
check(forced.get('cached') is False, 'force=True 绕过缓存')

print('--- 3b. 装上新版后，旧缓存必须失效 ---')
import tempfile as _tf
fake = Path(_tf.mkdtemp())
(fake / 'version.json').write_text(json.dumps({'version': '1.4.4', 'code': 0}), 'utf-8')


def _src(*a, **k):
    return Resp(200, json.dumps({'version': '1.4.5', 'release': 'https://x'}))


updater.core.request = _src
old = updater.check(fake, force=False)
check(old.get('hasNew') is True, '本地 1.4.4 时判定有新版本')
(fake / 'version.json').write_text(json.dumps({'version': '1.4.6', 'code': 0}), 'utf-8')
newer = updater.check(fake, force=False)
check(newer.get('cached') is False, '当前版本变了，缓存不命中')
check(newer.get('hasNew') is False, '本地 1.4.6 不再提示 1.4.5')

print('--- 4. 三路全挂：返回 ok=False 而不是抛异常 ---')
updater.core.request = lambda *a, **k: Resp(0, '')


def boom(*a, **k):
    raise OSError('no network')


updater.core.request = boom
bad = updater.check(Path('web'), force=True)
check(bad.get('ok') is False, '全挂时 ok=False')
check(bad.get('error') == '检查失败', '给出可展示的错误文案')

print()
if fails:
    print(f'{len(fails)} 条不通过')
    sys.exit(1)
print('updater 自检全部通过')