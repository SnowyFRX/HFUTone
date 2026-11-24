"""发布物自检：任何 zip / 目录在发出去之前，先证明它不含登录态。

为什么单独做一个脚本：`make_release_zip.py` 只能保证"这个脚本生成的包是干净的"。
但要发的东西未必出自它 —— dist/HFUTone/.config 被复制成 backup/、被人 zip 到
桌面再拖进 Release，都能绕过打包时的那道排除。所以自检必须能独立存在于产物上。

判据分三层，任一命中即判非法（退出码 1）：
  1. 路径层：出现 .config/ 目录、logs/ 目录、*.log 文件
  2. 标记层：出现 .config/.do-not-ship（yktcore 每次启动会写，跟着目录走，
     所以目录被改名复制也拦得住）
  3. 内容层：小体积文本文件里出现 sessionid= / app_cookie / "token": 等明文特征

用法：
    python tools/release_check.py --zip HFUTone-1.1-win64.zip
    python tools/release_check.py --dir dist/HFUTone      # 打包前先看源目录
    python tools/release_check.py --zip x.zip --quiet     # 只靠退出码
退出码：0 干净，1 有命中，2 用法/IO 错误。
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

# 目录名（任意层级）命中即整棵算违规
BAD_DIRS = {'.config', 'logs', '.qtcache', '__pycache__'}
# 文件名命中即违规：这些是凭据/个人数据
BAD_NAMES = {
    'ykt.json', 'cx.json', 'session.json', 'settings.json',
    'ykt-cookie.txt', 'courses.json', 'client-errors.log',
    '.do-not-ship',
}
BAD_SUFFIXES = {'.log', '.pyc', '.pyo'}

# 文本里出现这些形态，说明凭据被内联进了某个文件（比如日志或打包残留）
BAD_PATTERNS = [
    re.compile(r'sessionid\s*[=:]\s*[\w.\-]{16,}', re.I),
    re.compile(r'"?app_cookie"?\s*[=:]', re.I),
    re.compile(r'"(token|uniqueId|device_id)"\s*:\s*"[^"]{8,}"'),
]
# 占位符不算泄露：源码与夹具里本来就有 s sessionid={token}、replay、<32HEX>、
# 我造的那条全 0 JWT 之类的假值。语气上宁可漏报也不要天天误报 —— 一旦误报，
# pre-commit 会被 --no-verify 绕过去，那这道闸就白装了。
PLACEHOLDER_HINTS = (
    'replay', '{', '}', '<', '>', 'xxx', 'example', 'placeholder',
    '0000000', 'deadbeef', 'redacted', 'dump', 'fake', 'dummy', 'sample',
)
# 只扫这些后缀的内容，二进制跳过（避免误报与性能损失）
SCAN_SUFFIXES = {'.json', '.txt', '.ini', '.cfg', '.py', '.js', '.html',
                 '.css', '.md', '.bat', '.ps1', '.yml', '.yaml'}
SCAN_MAX_BYTES = 2 * 1024 * 1024


def _check_name(parts: list[str], name: str) -> str | None:
    for p in parts:
        if p in BAD_DIRS:
            return f'目录 {p}/'
    if name in BAD_NAMES:
        return f'凭据文件 {name}'
    if Path(name).suffix.lower() in BAD_SUFFIXES:
        return f'后缀 {Path(name).suffix}'
    return None


def _check_text(data: bytes) -> str | None:
    try:
        text = data.decode('utf-8', 'replace')
    except Exception:                                    # noqa: BLE001
        return None
    for line in text.splitlines():
        low = line.lower()
        if any(h in low for h in PLACEHOLDER_HINTS):
            continue
        for pat in BAD_PATTERNS:
            m = pat.search(line)
            if m:
                return f'明文特征 {m.group(0)[:40]!r}'
    return None


def check_zip(path: Path, quiet: bool) -> list[str]:
    hits: list[str] = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = info.filename.replace('\\', '/').split('/')[:-1]
            name = info.filename.rsplit('/', 1)[-1]
            why = _check_name(parts, name)
            if why:
                hits.append(f'{info.filename}  <- {why}')
                continue
            if Path(name).suffix.lower() in SCAN_SUFFIXES and info.file_size <= SCAN_MAX_BYTES:
                with z.open(info) as fh:
                    why = _check_text(fh.read())
                if why:
                    hits.append(f'{info.filename}  <- {why}')
    return hits


def check_dir(root: Path, quiet: bool) -> list[str]:
    hits: list[str] = []
    for path in root.rglob('*'):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        parts = list(rel.parts[:-1])
        why = _check_name(parts, rel.name)
        if why:
            hits.append(f'{rel.as_posix()}  <- {why}')
            continue
        if rel.suffix.lower() in SCAN_SUFFIXES and path.stat().st_size <= SCAN_MAX_BYTES:
            why = _check_text(path.read_bytes())
            if why:
                hits.append(f'{rel.as_posix()}  <- {why}')
    return hits


def check_git(quiet: bool) -> list[str]:
    """仓库模式：只看 git 跟踪的文件（工作树内容）。

    和 --dir 的区别是它不扫 dist/、.config/ 这些被忽略的东西 —— 那些本来就
    进不了提交。它要抓的是"有人把凭据 git add 进来了"：
      git add -f .config/ykt.json
      git add *.log
      把 token 抄进某个源码/夹具文件
    历史里的旧东西不在这里管（改历史代价太大），这条只管以后。
    """
    import subprocess

    try:
        # git 输出的是 UTF-8（仓库里有中文文件名，比如 docs/直播协议逆向报告.md），
        # 这里不能按系统 locale（GBK）解，否则直接 UnicodeDecodeError
        out = subprocess.check_output(['git', 'ls-files', '-z'],
                                      encoding='utf-8', errors='surrogateescape')
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f'[错误] 读不到 git 文件列表：{exc}', file=sys.stderr)
        return ['<git ls-files 失败>']
    hits: list[str] = []
    for rel in [p for p in out.split('\0') if p]:
        path = Path(rel)
        if not path.is_file():
            continue
        parts = list(path.parts[:-1])
        why = _check_name(parts, path.name)
        if why:
            hits.append(f'{rel}  <- {why}')
            continue
        if path.suffix.lower() in SCAN_SUFFIXES and path.stat().st_size <= SCAN_MAX_BYTES:
            why = _check_text(path.read_bytes())
            if why:
                hits.append(f'{rel}  <- {why}')
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--zip', help='要检查的 zip')
    g.add_argument('--dir', help='要检查的目录（打包前的源目录）')
    g.add_argument('--git', action='store_true',
                   help='检查 git 跟踪的文件（提交前 / CI 用）')
    ap.add_argument('--quiet', action='store_true', help='只输出结论')
    args = ap.parse_args()

    if args.git:
        hits = check_git(args.quiet)
        label = 'git 跟踪文件'
    else:
        target = Path(args.zip or args.dir)
        if args.zip:
            if not target.is_file():
                print(f'[错误] 不是文件：{target}', file=sys.stderr)
                return 2
            hits = check_zip(target, args.quiet)
            label = f'zip {target.name}'
        else:
            if not target.is_dir():
                print(f'[错误] 不是目录：{target}', file=sys.stderr)
                return 2
            hits = check_dir(target, args.quiet)
            label = f'目录 {target}'

    if not hits:
        if not args.quiet:
            print(f'[通过] {label}：未发现配置目录、凭据文件或明文凭据')
        return 0

    print(f'[失败] {label}：{len(hits)} 处不该发布的内容', file=sys.stderr)
    for h in hits[:40]:
        print('   ', h, file=sys.stderr)
    if len(hits) > 40:
        print(f'    …另有 {len(hits) - 40} 处', file=sys.stderr)
    print('提示：打发布包请用 tools/make_release_zip.py（会排除 .config 并复检）',
          file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())