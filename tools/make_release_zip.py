"""打发布用的 Windows zip，并且**绝不带出登录态**。

踩过的坑（2026-11-24）：手工把 dist/HFUTone 整个压进 zip 挂到 Release，
里面连着 .config/ 一起上传了 —— 那目录里是用户自己的东西：

    .config/ykt.json         雨课堂 App cookie（app_cookie + userId）
    .config/ykt-cookie.txt   网页 sessionid
    .config/session.json     雨课堂会话
    .config/cx.json          学习通凭据（DPAPI 加密，但仍属本人）
    .config/courses.json     他的课程表
    .config/logs/*.log       四天的接口日志

公开仓库的附件是全世界都能下的，这等于把本人的登录态一起发出去。
所以打包必须白名单式地排除，而不是"记得别放进去"。

用法：
    python tools/make_release_zip.py                 # 默认 dist/HFUTone -> HFUTone-<ver>-win64.zip
    python tools/make_release_zip.py --src dist/HFUTone --out out.zip
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

# 目录名前缀命中即整棵跳过
SKIP_DIRS = {
    '.config',
    '__pycache__',
    '.qtcache',
    '.git',
    'logs',
}
# 单文件按后缀兜底（有些日志会落到别处）
SKIP_SUFFIX = {'.log', '.pyc', '.pyo', '.bak'}
# 命中这些名字说明凭据混进来了，直接中止打包，别赌
SECRET_NAMES = {
    'ykt.json', 'cx.json', 'session.json', 'settings.json',
    'ykt-cookie.txt', 'courses.json', 'client-errors.log',
}


def build(src: Path, out: Path, root_name: str | None = None) -> int:
    if not src.is_dir():
        print(f'[错误] 源目录不存在：{src}', file=sys.stderr)
        return 2
    root = root_name or src.name
    total = 0
    written = 0
    flagged: list[str] = []

    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                total += 1
                path = Path(dirpath) / name
                if path.suffix.lower() in SKIP_SUFFIX:
                    continue
                if name in SECRET_NAMES:
                    flagged.append(str(path.relative_to(src)))
                    continue
                arc = Path(root) / path.relative_to(src)
                z.write(path, arc.as_posix())
                written += 1

    size = out.stat().st_size
    print(f'[完成] {out}  {size / 1048576:.1f} MB  文件 {written}/{total}（已跳过配置与日志）')
    if flagged:
        print('[警告] 下面的文件名看着像凭据，已被排除：')
        for f in flagged:
            print('   ', f)

    # 出厂自检：包里不能再出现任何 .config 条目
    with zipfile.ZipFile(out) as z:
        leak = [n for n in z.namelist()
                if '/.config/' in n or n.startswith('.config/')
                or any(part in SKIP_DIRS for part in n.split('/'))
                or n.endswith('.log')]
    if leak:
        print(f'[失败] zip 里仍有 {len(leak)} 条不该发布的路径：', file=sys.stderr)
        for n in leak[:10]:
            print('   ', n, file=sys.stderr)
        out.unlink(missing_ok=True)
        return 1
    print('[自检] 无 .config / 日志条目')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=str(HERE / 'dist' / 'HFUTone'))
    ap.add_argument('--out', default=str(HERE / 'HFUTone-1.1-win64.zip'))
    ap.add_argument('--root', default=None, help='zip 内的顶层目录名，默认取源目录名')
    args = ap.parse_args()
    return build(Path(args.src), Path(args.out), args.root)


if __name__ == '__main__':
    raise SystemExit(main())