"""二维码解析：过期码只标记、不拦截。

学习通签到接口只认 enc，c（signCode）只是老师那边的展示码。所以展示码
过期不影响提交——早先这里 c 对不上就返回错误，把本来能签的路堵死了。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import cxsign

fails = []


def check(cond, msg):
    print(('  [通过] ' if cond else '  [FAIL] ') + msg)
    if not cond:
        fails.append(msg)


class FakeApi:
    def __init__(self, server_code='NEW'):
        self.server_code = server_code

    def sign_detail(self, aid, code):
        if self.server_code is None:
            raise OSError('网络挂了')
        return {'signCode': self.server_code}


def parse(url, server_code='NEW'):
    s = cxsign.CxSigner.__new__(cxsign.CxSigner)
    s.api = FakeApi(server_code)
    return s.parse_qr(url)


BASE = 'https://mobilelearn.chaoxing.com/widget/sign/e?id=1&enc=ENC'

print('--- 1. 展示码过期：放行并标记 ---')
d, e = parse(BASE + '&c=OLD', 'NEW')
check(e == '', '不返回错误')
check(d.get('expired') is True, '标记 expired')
check(d.get('enc') == 'ENC', 'enc 正常带出')

print('--- 2. 展示码当前：不标记 ---')
d, e = parse(BASE + '&c=NEW', 'NEW')
check(e == '' and d.get('expired') is False, '不标记过期')

print('--- 3. 服务端没给 signCode（非动态码）：放行 ---')
d, e = parse(BASE + '&c=X', '')
check(e == '' and d.get('expired') is False, '按未过期处理')

print('--- 4. 复核请求失败：仍然放行 ---')
d, e = parse(BASE + '&c=OLD', None)
check(e == '', '复核失败不阻断提交')

print('--- 5. 非学习通链接：拒绝 ---')
d, e = parse('https://example.com/x')
check(e != '', '返回错误')

print()
if fails:
    print(len(fails), '条不通过')
    sys.exit(1)
print('二维码解析自检全部通过')
