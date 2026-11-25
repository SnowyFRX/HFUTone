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


MPV_PKG = HERE / 'mpv' / 'mpv.exe.xz'
MPV_DIR = DIST / 'mpv'


def _unpack_mpv() -> None:
    """把 mpv/mpv.exe.xz 解到 dist/HFUTone/mpv/。

    仓库里存的是压缩包：mpv.exe 原文件 117 MB，超过 GitHub 单文件 100 MB 的
    限制。解压出来的播放器 hfutlive 会优先选中，用户不必自己装 mpv。
    """
    if not MPV_PKG.is_file():
        print('[mpv] 没有 mpv/mpv.exe.xz，跳过内置播放器')
        return
    import lzma
    import time
    MPV_DIR.mkdir(parents=True, exist_ok=True)
    dst = MPV_DIR / 'mpv.exe'
    started = time.time()
    dst.write_bytes(lzma.decompress(MPV_PKG.read_bytes()))
    print(f'[mpv] {dst.name} {dst.stat().st_size:,} B（{time.time() - started:.1f}s）')


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
    # 判"有实质内容"，不是判目录存在：.config 里只要没有登录态文件，
    # 拷过去只会把上一份好备份覆盖成空壳（2026-11-25 就这么丢过一次
    # 用户重登后的凭据）。
    MEANINGFUL = ('ykt.json', 'cx.json', 'settings.json', 'ykt-cookie.txt')

    def _has_state(d: Path) -> bool:
        return d.is_dir() and any((d / n).exists() for n in MEANINGFUL)

    if _has_state(CFG):
        if BACKUP.exists():
            shutil.rmtree(BACKUP, ignore_errors=True)
        _copy_tree(CFG, BACKUP)
        n_before = len(list(CFG.rglob('*')))
        n_after = len(list(BACKUP.rglob('*'))) if BACKUP.is_dir() else 0
        print(f'[备份] {CFG} -> {BACKUP}（{n_after}/{n_before} 项）')
        if not _has_state(BACKUP):
            print('[失败] 备份不含任何登录态文件，中止打包，免得把现存的也搞丢')
            return 3
    elif BACKUP.is_dir() and _has_state(BACKUP):
        print(f'[备份] dist 没有可用 .config，沿用上次备份 {BACKUP}')
    else:
        print('[备份] 没有可用的 .config（首次运行会重建），继续')

    # 2) 打包
    cmd = [sys.executable, '-m', 'PyInstaller', 'HFUTone.spec', '--noconfirm']
    print('[打包]', ' '.join(cmd))
    code = subprocess.call(cmd, cwd=str(HERE))
    if code != 0:
        # --noconfirm 在失败前往往已经删掉了 dist/HFUTone，此时不还原就等于
        # 把用户登录态一起清空。还原动作必须在这里也做一次。
        src = BACKUP if _has_state(BACKUP) else SRC_CFG
        if _has_state(src):
            _copy_tree(src, CFG)
            print(f'[还原] 打包失败，已把 {src} 恢复回 {CFG}')
        print(f'[失败] PyInstaller 退出码 {code}')
        return code

    # 3) 内置播放器：mpv/mpv.exe.xz -> dist/HFUTone/mpv/mpv.exe
    _unpack_mpv()

    # 4) 还原配置：优先备份，没有就退到源树的 .config
    source = BACKUP if BACKUP.is_dir() else SRC_CFG
    if source.is_dir():
        _copy_tree(source, CFG)
        print(f'[还原] {source} -> {CFG}')
    else:
        print('[还原] 没有可还原的 .config')

    # 5) 补一个"禁止发布"标记。它是 yktcore 运行时才写的，而 pyinstaller
    #    根本不运行 exe；还原又是从旧备份拷回来的（里面没有这个文件），
    #    所以这里得补。tools/release_check.py 认它。
    guard = CFG / '.do-not-ship'
    try:
        CFG.mkdir(parents=True, exist_ok=True)
        guard.write_text(
            '运行期配置目录，含登录态与个人信息，禁止打包发布。\n'
            '打发布包请用 tools/make_release_zip.py（它会排除本目录并自检）。\n',
            'utf-8')
        print(f'[标记] {guard}')
    except OSError as exc:
        print(f'[警告] 写标记失败（不影响运行）：{exc}')

    print('[完成] 打包结束，配置已保住')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())