import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'web'))
import server

tmp = Path(tempfile.mkdtemp())
server.CRED_DIR = tmp
server.SETTINGS_FILE = tmp / 'settings.json'

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print('  [FAIL]', msg)


print('--- 默认值 ---')
cfg = server.read_settings()
print('ykt.signMode   :', cfg['ykt']['signMode'])
print('cx.location    :', cfg['cx']['location'])
check(cfg['ykt']['signMode'] == 'app', '默认应为 app（App 身份是唯一签到通道）')
check(cfg['cx']['location'] == {'address': '', 'lat': 0.0, 'lng': 0.0},
      '坐标默认应为空')

print('--- 改签到通道 ---')
for m in ('scan', 'app', 'both'):
    cfg = server.write_settings({'ykt': {'signMode': m}})
    check(cfg['ykt']['signMode'] == m, f'{m} 写入失败')
    check(server.read_settings()['ykt']['signMode'] == m, f'{m} 读回失败')
print('三种模式都通')

print('--- 非法模式应被忽略 ---')
cfg = server.write_settings({'ykt': {'signMode': 'nonsense'}})
check(cfg['ykt']['signMode'] == 'both', f'非法模式不该生效（应保持上一次的 both），实际 {cfg["ykt"]["signMode"]}')

print('--- 位置坐标 ---')
cfg = server.write_settings({'cx': {'location': {
    'address': '翡翠湖校区 · 西二302', 'lat': 31.860123, 'lng': 117.280456}}})
print('写后:', cfg['cx']['location'])
check(cfg['cx']['location']['address'] == '翡翠湖校区 · 西二302', '地址没存上')
check(abs(cfg['cx']['location']['lat'] - 31.860123) < 1e-6, '纬度没存上')

print('--- 越界应被钳制 ---')
cfg = server.write_settings({'cx': {'location': {'lat': 999, 'lng': -999}}})
print('钳制后:', cfg['cx']['location'])
check(cfg['cx']['location']['lat'] == 90, f'纬度应钳到 90，实际 {cfg["cx"]["location"]["lat"]}')
check(cfg['cx']['location']['lng'] == -180, f'经度应钳到 -180，实际 {cfg["cx"]["location"]["lng"]}')

print('--- 只改一个字段不该抹掉其他 ---')
server.write_settings({'cx': {'location': {'address': 'A', 'lat': 1.5, 'lng': 2.5}}})
cfg = server.write_settings({'cx': {'location': {'address': 'B'}}})
print('结果:', cfg['cx']['location'])
check(cfg['cx']['location']['address'] == 'B', '地址没更新')
check(abs(cfg['cx']['location']['lat'] - 1.5) < 1e-6, '纬度被抹掉了')
check(abs(cfg['cx']['location']['lng'] - 2.5) < 1e-6, '经度被抹掉了')

print('--- 磁盘内容 ---')
print(json.dumps(json.loads(server.SETTINGS_FILE.read_text('utf-8')),
                 ensure_ascii=False, indent=1))

if fails:
    print(f'\n共 {len(fails)} 项失败')
    raise SystemExit(1)
print('\n设置读写全部通过')
