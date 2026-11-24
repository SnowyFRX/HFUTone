"""找「调用了没定义的名字」这种运行时才炸的低级错。

为什么需要：2026-11-25 学习通登录直接报 `name 'log' is not defined` ——
cxapi.py 里写成了 `log(...)`，而那个模块只 import 了 `import yktcore as core`，
没把 log 拉进命名空间。py_compile 只查语法，查不出这种；replay 夹具也走不到
登录那条分支。用户点一下登录才看见。

规则（故意保守，只报"名字在整个文件里根本不存在"）：
  · 收集：import 绑定的名字、模块级 def/class、任何层级的 def/class/参数名/赋值目标
  · 检查：Call(func=Name) 里的名字，若不在上面任一集合、也不是内建 → 报
名字在别处定义（比如局部变量）就不报，所以误报率低；代价是漏报率高。
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

BUILTINS = set(dir(builtins))


def collect(tree: ast.AST) -> set[str]:
    known: set[str] = set()

    def add_target(node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            known.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for e in node.elts:
                add_target(e)
        elif isinstance(node, ast.Starred):
            add_target(node.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                known.add((a.asname or a.name).split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                known.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            known.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for a in list(args.args) + list(args.kwonlyargs) + \
                        list(getattr(args, 'posonlyargs', [])):
                    known.add(a.arg)
                if args.vararg:
                    known.add(args.vararg.arg)
                if args.kwarg:
                    known.add(args.kwarg.arg)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                add_target(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            add_target(node.target)
        elif isinstance(node, ast.For):
            add_target(node.target)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                add_target(node.optional_vars)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                known.add(node.name)
        elif isinstance(node, ast.Lambda):
            for a in list(node.args.args) + list(node.args.kwonlyargs):
                known.add(a.arg)
    return known


def check(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except SyntaxError as exc:
        return [f'{path}: 语法错误 {exc}']
    known = collect(tree) | BUILTINS
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id not in known:
            problems.append(f'{path}:{node.lineno} 调用了未定义的名字 {fn.id}()')
    return problems


def main() -> int:
    roots = [Path('src'), Path('web'), Path('tools'), Path('tests')]
    files: list[Path] = []
    for r in roots:
        if r.is_dir():
            files += sorted(p for p in r.glob('*.py'))
    files += [Path('build.py')]
    bad: list[str] = []
    for f in files:
        if f.exists():
            bad += check(f)
    if bad:
        print(f'[失败] {len(bad)} 处可疑调用：')
        for b in bad[:40]:
            print('   ', b)
        return 1
    print(f'[通过] 扫描 {len(files)} 个文件，没有「调用了未定义的名字」')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())