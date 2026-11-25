"""滑块缺口识别的回归。

算法本体在 web/cxgap.js —— 它必须留在前端，因为「背景 JPEG + 拼图 PNG」
要先解码，而安卓那边的 chaquopy 只带标准库（没有 numpy/PIL/cv2）。
所以这里用 node 跑同一份生产代码，对固化的真实验证码样本断言结果。

样本来源：2026-11-25 现取的真实验证码。manifest.json 里记着每张的期望：
  · ok=true  表示该自动提交，x 必须等于实测的缺口左边界
  · ok=false 表示识别结果不可信，必须退回人工拖 —— 别让它在后台瞎提交

跑法：
    python tests/test_cxgap.py
需要 node（CI 的 privacy.yml 不跑测试，这是本地回归）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIX = HERE / 'fixtures' / 'captcha'
RUNNER = HERE / 'cxgap_runner.mjs'


def run() -> dict | None:
    node = shutil.which('node')
    if not node:
        print('[跳过] 没有 node，滑块缺口回归没跑')
        return None
    if not RUNNER.is_file():
        raise SystemExit(f'缺少 {RUNNER}')
    proc = subprocess.run([node, str(RUNNER)], capture_output=True, cwd=str(ROOT))
    if proc.returncode != 0:
        raise SystemExit('node 跑挂了：\n' + proc.stderr.decode('utf-8', 'replace'))
    return json.loads(proc.stdout.decode('utf-8'))


def main() -> int:
    data = run()
    if data is None:
        return 0
    expect = json.loads((FIX / 'manifest.json').read_text('utf-8'))
    got = data['samples']
    bad = []
    for name, want in sorted(expect.items()):
        if name not in got:
            bad.append(f'{name}: 没跑出结果')
            continue
        cur = got[name]
        if bool(cur['ok']) != bool(want['ok']):
            bad.append(f"{name}: ok={cur['ok']} 期望 {want['ok']}（{cur['reason']}）")
        elif want['ok'] and int(cur['x']) != int(want['x']):
            bad.append(f"{name}: x={cur['x']} 期望 {want['x']}")
        else:
            print(f"  [通过] {name}: ok={cur['ok']} x={cur['x']} "
                  f"alpha={cur['alpha']} ratio={cur['ratio']}")
    print(f"\n样本 {len(expect)} 张，算法耗时 {data['elapsedMs']} ms")
    if bad:
        print('\n[失败]')
        for b in bad:
            print('   ', b)
        return 1
    print('[通过] 缺口识别与期望一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())