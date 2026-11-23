"""离线回放回归：不联网，所有请求读 fixtures/*.json。

    set YKT_REPLAY=D:/DSHSYBA/HFUTone/fixtures
    set YKT_CONFIG_DIR=%TEMP%/hfutone-test
    python tests/replay.py

退出码 0 = 全部通过。任何一条断言失败就打印差异并返回 1。
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))


def build_replay_dir() -> Path:
    """把 fixtures 复制到临时目录，用带课的那份当 on-lesson。

    默认 on-lesson.json 是"没有课"的空夹具，回归要跑通签到链路，
    所以换成 on-lesson-one.json，并补一个 classroomId 让考勤接口能查。
    """
    tmp = Path(tempfile.mkdtemp(prefix='hfutone-replay-'))
    atexit.register(shutil.rmtree, tmp, True)      # 跑完自己收摊，别在仓库里留垃圾
    for f in (ROOT / 'fixtures').iterdir():
        if f.is_file():
            shutil.copy(f, tmp)
    one = json.loads((ROOT / 'fixtures' / 'on-lesson-one.json').read_text('utf-8'))
    room = one['data']['onLessonClassrooms'][0]
    room.setdefault('classroomId', 880123)
    (tmp / 'on-lesson.json').write_text(json.dumps(one, ensure_ascii=False), 'utf-8')
    return tmp


os.environ['YKT_REPLAY'] = str(build_replay_dir())
# 配置目录必须完全隔离：回放里会 set_cookie('sessionid=replay')、写"已签到"标记，
# 落到仓库 .config 就把真实登录态和真实标记冲了。用一次性临时目录。
_cfg = Path(tempfile.mkdtemp(prefix='hfutone-cfg-'))
atexit.register(shutil.rmtree, _cfg, True)
os.environ['YKT_CONFIG_DIR'] = str(_cfg)

import yktapi          # noqa: E402
import yktcore         # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = '') -> None:
    print(('  ok   ' if cond else '  FAIL ') + name + (f'   {detail}' if detail and not cond else ''))
    if not cond:
        failures.append(f'{name}: {detail}')


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass
    print(f'回放目录 {yktcore.replay_dir()}')
    print(f'配置目录 {yktcore.CONFIG_DIR}')

    y = yktapi.Ykt()

    print('\n[1] 课程列表')
    y.set_cookie('sessionid=replay; csrftoken=replay')
    rows = y.on_lesson()
    check('至少一节在播课堂', len(rows) >= 1, str(rows))
    if rows:
        check('字段齐全', all(rows[0].get(k) for k in ('lessonId', 'name')), str(rows[0]))

    print('\n[2b] 服务器说已签到的课')
    one = json.loads((ROOT / 'fixtures' / 'on-lesson-one.json').read_text('utf-8'))
    room = one['data']['onLessonClassrooms'][0]
    room.setdefault('classroomId', 880123)
    replay_on = Path(yktcore.replay_dir(), 'on-lesson.json')

    room['signinStatus'] = 1
    replay_on.write_text(json.dumps(one, ensure_ascii=False), 'utf-8')
    signed_rows = y.on_lesson()
    check('signinStatus=1 判成已签到',
          bool(signed_rows and signed_rows[0].get('checkedAt')), str(signed_rows[:1]))
    check('rows 带出 signinStatus 字段',
          bool(signed_rows) and signed_rows[0].get('signinStatus') == 1, str(signed_rows[:1]))

    room['signinStatus'] = 0
    replay_on.write_text(json.dumps(one, ensure_ascii=False), 'utf-8')
    unsign_rows = y.on_lesson()
    check('signinStatus=0 不判成已签到（本地也没标记）',
          bool(unsign_rows) and not unsign_rows[0].get('checkedAt'), str(unsign_rows[:1]))

    print('\n[3] 签到 + 考勤核对')
    lid = rows[0]['lessonId'] if rows else '12345'
    r, cred = y.checkin(lid, source=21)
    check('checkin 返回 200', r.status == 200, f'status={r.status} body={core_head(r.body)}')
    code = yktcore.jcode(r.body)
    check('code 为 0', code == 0, f'code={code} body={core_head(r.body)}')
    check('拿到 set-auth（Bearer）', bool(cred.bearer), cred.as_dict().__str__())
    check('拿到 lessonToken', bool(cred.lesson_token), cred.as_dict().__str__())

    room_id = str(room.get('classroomId') or '')
    n_act, detail, mine = y.rollcall(lid, room_id)
    check('考勤接口有响应', n_act >= 0, detail)
    check('rollcall 不再拿活动数据推断"我签了"', mine is False, f'mine={mine}')

    y.store.unmark_checked(lid)
    res = y.signin_full(lid, room_id, '回放课堂', signin_status=0)
    check('signin_full 结论 ok', res.get('ok') is True, str(res))
    check('状态落在已知档位',
          res.get('status') in ('checked', 'already', 'joined', 'need_scan', 'ended'), str(res))
    check('checkin 文案是"签到成功"，判成 checked', res.get('status') == 'checked', str(res))
    check('已写入已签到记录', lid in y.store.load_checked(), str(y.store.load_checked()))

    print('\n[3b] 只进班、没签到（老师还没发起签到）')
    y.store.unmark_checked(lid)
    ck = Path(yktcore.replay_dir(), 'checkin.json')
    ck_backup = ck.read_text('utf-8')
    ck.write_text('{"code":0,"msg":"OK","data":{}}', 'utf-8')
    res2 = y.signin_full(lid, room_id, '回放课堂', signin_status=0)
    check('code 0 但文案不是签到成功 => joined', res2.get('status') == 'joined', str(res2))
    check('joined 不写已签到标记', lid not in y.store.load_checked(), str(y.store.load_checked()))
    ck.write_text(ck_backup, 'utf-8')

    print('\n[3c] 服务器已记录签到（signinStatus=1）')
    y.store.unmark_checked(lid)
    room['signinStatus'] = 1
    replay_on.write_text(json.dumps(one, ensure_ascii=False), 'utf-8')
    ck.write_text('{"code":0,"msg":"OK","data":{}}', 'utf-8')
    res3 = y.signin_full(lid, room_id, '回放课堂', signin_status=1)
    check('服务器已签 => already', res3.get('status') == 'already', str(res3))
    check('already 写回已签到标记', lid in y.store.load_checked(), str(y.store.load_checked()))
    ck.write_text(ck_backup, 'utf-8')
    room['signinStatus'] = 0
    replay_on.write_text(json.dumps(one, ensure_ascii=False), 'utf-8')

    print('\n[3d] 已签到文案（两种写法都要认）')
    y.store.unmark_checked(lid)
    ck.write_text(json.dumps({'code': 0, 'msg': '已签到, 请勿重复签到', 'data': {}},
                             ensure_ascii=False), 'utf-8')
    res4 = y.signin_full(lid, room_id, '回放课堂', signin_status=0)
    check('code 0 + "已签到, 请勿重复签到" => already', res4.get('status') == 'already', str(res4))
    check('already 写回标记', lid in y.store.load_checked(), str(y.store.load_checked()))
    y.store.unmark_checked(lid)
    ck.write_text(json.dumps({'code': 2160, 'msg': '您已签到', 'data': {}},
                             ensure_ascii=False), 'utf-8')
    res5 = y.signin_full(lid, room_id, '回放课堂', signin_status=0)
    check('code 2160 + "您已签到" => already', res5.get('status') == 'already', str(res5))

    print('\n[3e] 动态二维码课堂')
    y.store.unmark_checked(lid)
    ck.write_text(json.dumps({'code': 50070, 'msg': 'DYNAMIC_QR 需扫码', 'data': {}},
                             ensure_ascii=False), 'utf-8')
    res6 = y.signin_full(lid, room_id, '回放课堂', signin_status=0)
    check('50070 + 服务器未记录 => need_scan', res6.get('status') == 'need_scan', str(res6))
    check('need_scan 不写已签到标记', lid not in y.store.load_checked(), str(y.store.load_checked()))
    res7 = y.signin_full(lid, room_id, '回放课堂', signin_status=1)
    check('50070 + 服务器已记录 => checked', res7.get('status') == 'checked', str(res7))
    ck.write_text(ck_backup, 'utf-8')

    print('\n[4] 课件')
    try:
        items = y.lesson_info(lid)
        check('课件清单非空', len(items) > 0, str(items))
        if items:
            title, slides = y.presentation(items[0]['id'], cred.bearer)
            check('课件有标题', bool(title), title)
            check('课件有页', len(slides) > 0, str(len(slides)))
            check('图片地址补全为绝对地址',
                  all(s['url'].startswith('http') for s in slides if s['cover']), str(slides[:2]))
    except RuntimeError as exc:
        check('课件链路', False, str(exc))

    print('\n[5] 答题')
    err, code = y.submit('prob-1001', 1, 'A', 1795000000, bearer=cred.bearer)
    check('时限内通道提交成功', err is None, f'err={err} code={code}')

    auto = y.submit_auto('prob-1002', 2, ['A', 'B'], 1795000000, bearer=cred.bearer)
    check('submit_auto 有结论', isinstance(auto, dict) and 'ok' in auto, str(auto))

    print('\n[6] 结果归一化')
    check('单选大写', yktapi.normalize_result(1, ' a ') == 'A')
    check('多选排序去重', yktapi.normalize_result(2, 'B,a,b') == ['A', 'B'])
    check('填空成数组', yktapi.normalize_result(3, '甲, 乙') == ['甲', '乙'])
    check('主观成对象',
          yktapi.normalize_result(5, '作答')['content'] == '作答')
    check('毫秒转秒', yktapi.norm_dt(1795000000000, 0) == 1795000000)
    check('已作答识别', yktapi.is_already_answered(50028, ''))
    check('补交通道判定', yktapi.can_retry_channel(50026, ''))

    print()
    if failures:
        print(f'失败 {len(failures)} 项：')
        for f in failures:
            print('  -', f)
        return 1
    print('全部通过')
    return 0


def core_head(body: str, n: int = 120) -> str:
    return (body or '')[:n].replace('\n', ' ')


if __name__ == '__main__':
    sys.exit(main())