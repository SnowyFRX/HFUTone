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


def version() -> str:
    """从 android/app/build.gradle 读 versionName，读不到就退回 dev。

    产物名跟着版本号走（HFUTone-1.4.4-win64.zip）。以前这里写死 1.1，
    发了三个版本之后，Release 里还挂着 HFUTone-1.1-* 的名字，和包里的
    versionCode 对不上，只能靠人记住这件事。
    """
    import re
    gradle = HERE / 'android' / 'app' / 'build.gradle'
    try:
        m = re.search(r"versionName\s+'([^']+)'", gradle.read_text('utf-8'))
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return 'dev'

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
# yktcore 每次启动会在配置目录里写这个标记（tools/release_check.py 认它）
SHIP_GUARD = '.do-not-ship'


def build(src: Path, out: Path, root_name: str | None = None) -> int:
    if not src.is_dir():
        print(f'[错误] 源目录不存在：{src}', file=sys.stderr)
        return 2
    # 传进来的就是配置目录本身（含 yktcore 落的 .do-not-ship）→ 直接拒绝，
    # 别打成"一个只有登录态的包"再靠后缀过滤去救
    if (src / SHIP_GUARD).exists():
        print(f'[错误] {src} 是运行期配置目录（含 {SHIP_GUARD}），不是发布目录。', file=sys.stderr)
        print('       发布目录应为 dist/HFUTone（exe 所在），不是它的 .config。', file=sys.stderr)
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

    # 终检交给 tools/release_check.py：它认的是"产物里有没有"，不看是谁打的包，
    # 所以这里不重复实现一套判据（两套很容易慢慢走岔）。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import release_check                                  # noqa: E402

    hits = release_check.check_zip(out, quiet=True)
    if hits:
        print(f'[失败] zip 里仍有 {len(hits)} 处不该发布的内容，已删除该包：', file=sys.stderr)
        for h in hits[:20]:
            print('   ', h, file=sys.stderr)
        out.unlink(missing_ok=True)
        return 1
    print('[自检] 无 .config / 日志 / 明文凭据')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=str(HERE / 'dist' / 'HFUTone'))
    ap.add_argument('--out', default=str(HERE / f'HFUTone-{version()}-win64.zip'))
    ap.add_argument('--root', default=None, help='zip 内的顶层目录名，默认取源目录名')
    args = ap.parse_args()
    return build(Path(args.src), Path(args.out), args.root)


if __name__ == '__main__':
    raise SystemExit(main())