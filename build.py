"""build.py —— 打包 HFUTone，并保住 dist/HFUTone/.config 里的登录态与设置。

为什么要这个脚本：PyInstaller --noconfirm 会整个删掉 dist/HFUTone，
而 .config（学习通 cookie、雨课堂 sessionid、坐标设置、手动加课）
就放在 exe 同级，于是每次打包都把登录态清空，用户得重新扫码。

流程：备份 .config -> 跑 PyInstaller -> 还原 .config（不存在才用源树的）。
用法：python build.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / 'dist' / 'HFUTone'
CFG = DIST / '.config'
BACKUP = HERE / '.config-backup'
SRC_CFG = HERE / '.config'


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        try:
            if item.is_dir():
                _copy_tree(item, target)
            else:
                shutil.copy2(item, target)
        except OSError as exc:
            print(f'  [跳过] {item.name}: {exc}')


def main() -> int:
    # 1) 备份现有 dist 配置（这是真身，含登录态）
    #    顺序很重要：先看源目录在不在，再动旧备份。反过来写会造成
    #    "先删了备份、源目录又恰好不在了"——备份就此消失（2026-11-24
    #    实际踩过：连续打包两次，第二次把唯二份登录态一起清掉）。
    if CFG.is_dir():
        if BACKUP.exists():
            shutil.rmtree(BACKUP, ignore_errors=True)
        _copy_tree(CFG, BACKUP)
        print(f'[备份] {CFG} -> {BACKUP}')
    elif BACKUP.is_dir():
        print(f'[备份] dist 没有 .config，沿用上次备份 {BACKUP}')
    else:
        print('[备份] dist 没有 .config，也没有旧备份')

    # 2) 打包
    cmd = [sys.executable, '-m', 'PyInstaller', 'HFUTone.spec', '--noconfirm']
    print('[打包]', ' '.join(cmd))
    code = subprocess.call(cmd, cwd=str(HERE))
    if code != 0:
        print(f'[失败] PyInstaller 退出码 {code}，.config 仍在 {BACKUP}')
        return code

    # 3) 还原配置：优先备份，没有就退到源树的 .config
    source = BACKUP if BACKUP.is_dir() else SRC_CFG
    if source.is_dir():
        _copy_tree(source, CFG)
        print(f'[还原] {source} -> {CFG}')
    else:
        print('[还原] 没有可还原的 .config')

    print('[完成] 打包结束，配置已保住')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())