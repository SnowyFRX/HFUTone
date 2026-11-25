"""上传链路的自检。

2026-11-25 之前学习通「强制人脸」那条一直是废的，但一直没报错：命令行里
一切正常，只是 face_id() 永远返回历史那个 objectId。查下来是三处叠在一起：

  1) 历史人脸图床 p.ananas.chaoxing.com 对 http:// 回 301，
     fetch_bytes 没带 -L -> 空 body -> 上传根本没发生
  2) 上传走 multipart，可请求头里固定带着 App 的
     content-type: application/x-www-form-urlencoded，
     它把 curl -F 自动生成的 boundary 盖掉了：服务端按 urlencoded 解 multipart，
     字段全是 null（{"code":-5,"msg":"参数错误, puid不能为空, null"}）
  3) 就算传上去，文件字节没变 -> crcStorageStatus 命中 -> 服务端把同一个
     objectId 原样还回来，等于没换脸

这个测试守住 1 和 2（3 是搬 byte 的行为，靠真机实测，见提交信息）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import yktcore

captured = []


class FakeProc:
    returncode = 0
    stdout = '{"result":true}\n---YKTSTATUS---200'
    stderr = ''


def fake_run(args, **kw):
    captured.append(list(args))
    return FakeProc()


yktcore.subprocess.run = fake_run
yktcore.curl_path = lambda force=False: 'curl'
yktcore.ca_cert_arg = lambda: []

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print('  [FAIL]', msg)
    else:
        print('  [通过]', msg)


APP_HEAD = {
    'User-Agent': 'Dalvik/2.1.0 (schild:x) com.chaoxing.mobile',
    'content-type': 'application/x-www-form-urlencoded',
}


def joined(args):
    return ' '.join(args)


print('--- 1. multipart 上传必须摘掉 urlencoded content-type ---')
captured.clear()
yktcore.request('POST', 'https://pan-yz.chaoxing.com/upload?_token=t',
                cookie='k=v', form={'puid': '402869987'},
                files={'file': 'x.jpg'}, extra_headers=APP_HEAD)
args = captured[-1]
cmd = joined(args)
check('-F' in args, '走了 curl -F（multipart）')
check('puid=402869987' in cmd, '表单字段 puid 发出去了')
check('file=@x.jpg' in cmd, '文件字段发出去了')
check('application/x-www-form-urlencoded' not in cmd,
      '没有把 urlencoded 的 content-type 发出去（发了服务端就解析不出字段）')
check('User-Agent' in cmd, '其它 App 头照旧（UA/schild 不能丢）')

print('--- 2. 普通表单不该被误伤：content-type 仍然要发 ---')
captured.clear()
yktcore.request('POST', 'https://mobilelearn.chaoxing.com/pptSign/stuSignajax',
                cookie='k=v', form={'activeId': '1'}, extra_headers=APP_HEAD)
cmd = joined(captured[-1])
check('application/x-www-form-urlencoded' in cmd, 'urlencoded 表单头还在')
check('activeId=1' in cmd, '表单体还在')

print('--- 3. 图床 301 必须跟随 ---')
captured.clear()
yktcore.fetch_bytes('http://p.ananas.chaoxing.com/star3/origin/x.jpg',
                    timeout=5, follow=True)
check('-L' in captured[-1], 'follow=True 时带了 -L')

captured.clear()
yktcore.fetch_bytes('http://p.ananas.chaoxing.com/star3/origin/x.jpg', timeout=5)
check('-L' not in captured[-1], '默认不乱跟随（下载别的资源时行为不变）')

print()
if fails:
    print(f'{[len(fails)]} 条不通过')
    sys.exit(1)
print('上传链路自检全部通过')