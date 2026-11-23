"""probe_identity.py —— 雨课堂「身份 × 凭据」矩阵探针。

回答一个问题：App 登录拿到的凭据，在 App 头下到底能不能用？以及这套凭据
能不能顺便干 web 头的事（也就是「只登录一次」是否成立）。

用法（在 HFUTone 根目录）：
    YKT_CONFIG_DIR=<配置目录> python tools/probe_identity.py

配置目录决定读哪份 ykt.json：
    源码版  D:\\DSHSYBA\\HFUTone\\.config
    打包版  D:\\DSHSYBA\\HFUTone\\dist\\HFUTone\\.config
不给 YKT_CONFIG_DIR 就用项目的 .config。

输出四行 × 两个接口：
    web 凭据 + web 头     应 200
    web 凭据 + App 头     已知 50000（x-client: app 触发另一套鉴权）
    App 凭据 + web 头     ← 决定 App 凭据能不能顶替 web
    App 凭据 + App 头     ← 决定动态码免扫是否成立
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

import yktcore as core                                        # noqa: E402

ENDPOINTS = [
    ('on-lesson', core.SERVER + '/api/v3/classroom/on-lesson'),
    ('user_info', core.SERVER + '/v/course_meta/user_info'),
]


def probe(store: core.Store) -> int:
    web_ck = store.get_cookie()
    app_ck = store.get_app_cookie()
    print(f'配置目录 : {store.path.parent}')
    print(f'web 凭据 : {"有 " + str(len(web_ck)) + " 字符" if web_ck else "无"}')
    print(f'App 凭据 : {"有 " + str(len(app_ck)) + " 字符" if app_ck else "无"}')
    print(f'当前身份 : {"App" if core.APP_IDENTITY else "web"}')
    print()

    combos = [
        ('web凭据 + web头', web_ck, None),
        ('web凭据 + App头', web_ck, dict(core.APP_HEADERS)),
        ('App凭据 + web头', app_ck, None),
        ('App凭据 + App头', app_ck, dict(core.APP_HEADERS)),
    ]

    verdict = {}
    for name, url in ENDPOINTS:
        print(f'=== {name}')
        for label, ck, hdr in combos:
            if not ck:
                print(f'  {label:18s} —（没有这套凭据，跳过）')
                continue
            r = core.request('GET', url, cookie=ck, referer=core.SERVER + '/',
                             timeout=15, extra_headers=hdr, tag='probe')
            code = core.jcode(r.body)
            body = core.head(r.body, 70)
            # user_info 这套接口没有 code 字段，别用它当鉴权判据
            if name == 'on-lesson':
                passed = (code == 0)
            else:
                passed = bool(core.jdata(r.body))
            print(f'  {label:18s} status={r.status} code={code} {body}')
            verdict.setdefault(label, []).append(passed)
        print()

    print('=== 结论')
    app_app = verdict.get('App凭据 + App头') or []
    if app_app and all(app_app):
        print('  [OK] App 凭据在 App 头下可用：动态码考勤可以免扫。')
    elif app_app:
        print('  [NG] App 凭据在 App 头下不可用：App 登录这条线到此为止。')
    else:
        print('  …  还没有 App 凭据，先做一次 App 登录。')

    app_web = verdict.get('App凭据 + web头') or []
    if app_web and all(app_web):
        print('  [OK] App 凭据也能干 web 头的事：只需要登录一次，可以只保留 App 身份。')
    elif app_web:
        print('  [!]  App 凭据不能顶替 web 身份：两套都要留，web 是签到主链路。')
    return 0


def main() -> int:
    store = core.Store()
    core.restore_identity(store)
    return probe(store)


if __name__ == '__main__':
    raise SystemExit(main())