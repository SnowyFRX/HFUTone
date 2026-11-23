# HFUTone

工大校园两个客户端合到一块：

| 功能 | 来源项目 | 实现 |
| --- | --- | --- |
| 直播课堂（课表、三路流、mpv 播放） | HFUTlive | Python + PyQt5/QtWebEngine 壳 + 本地 HTTP 服务 + mpv |
| 长江雨课堂（扫码登录、一键签到、课件、随堂题） | 「冰冰冰糖」ykt_ui（C# WinForms） | Python 重写协议层，TLS 走内置 LibreSSL curl |
| 学习通签到（五种签到、自动扫课、一键签到） | course_helper（Flutter/Dart，GPL-3.0） | Python 重写协议层，密码学自带实现，无第三方依赖 |

一个窗口：左边直播常驻，右边雨课堂 + 学习通可按需展开；打包成一个 `HFUTone.exe`。

---

## 目录

```
HFUTone/
├── app_qt.pyw            Qt 主窗口（无控制台入口）
├── launch_qt.pyw         pythonw 启动壳（双击用）
├── HFUTone.spec          PyInstaller 打包配置
├── version_info.txt      exe 版本资源
├── src/
│   ├── hfutlive.py       直播课堂：CAS 登录、课表、取流、拉播放器
│   ├── yktcore.py        雨课堂传输层：curl 调用、cookie/DPAPI、日志、回放
│   ├── yktapi.py         雨课堂业务层：登录/课表/签到/课件/答题/随堂题解析
│   ├── yktws.py          最小 RFC6455 WebSocket 客户端（随堂题只有 WS 有）
│   ├── hfutplay.py       直播画面：ffmpeg 抽帧成 JPEG，帧广播（PPT 内嵌）
│   ├── cxcrypto.py       学习通要的密码学：纯 Python AES-128 ECB/CBC、RSA 模幂、md5
│   ├── cxapi.py          学习通接口层：登录/课程/活动/五种签到/验证码/人脸/上传
│   ├── cxsign.py         学习通编排层：扫课、判类型、自动签、待输入归一化
│   └── wsserve.py        最小 RFC6455 服务端（把 JPEG 帧推给页面）
├── web/
│   ├── server.py         本地 HTTP 服务（127.0.0.1:8765），三套 API 都在这里
│   ├── index.html        单页：`.split` 里左直播 + 右侧栏（雨课堂 + 学习通）
│   ├── app.js / app.css  直播课堂界面（原样保留）
│   ├── ykt.js / ykt.css  雨课堂界面 + 直播画面卡片 + 设置弹层
│   ├── cx.js             学习通签到界面（复用 ykt.css 的行与按钮骨架）
│   ├── ppt.js            设置项与画面：WebSocket 收帧、canvas 绘制、点击放大
│   ├── mobile.css        窄屏单列布局（Android WebView 用，桌面规则不动）
│   └── mobile.js         Android 行为适配：撤掉直播块、首屏拉一次课程
├── android/              独立 Android App（原生壳 + Chaquopy 内嵌 Python）
├── vendor/curl/          内置 LibreSSL 版 curl（.gz，首次运行解压到 .config/bin）
├── vendor/ffmpeg/        ffmpeg 7.1（PPT 抽帧用；网页里没有 H.264 解码器）
├── fixtures/             离线回放夹具（YKT_REPLAY 指向它，不联网跑回归）
└── assets/
```

运行时数据落在 `.config/`（程序目录写不进去时自动退到 `%LOCALAPPDATA%\HFUTone`）：
`ykt.json`（DPAPI 加密的 cookie 与已签到记录）、`cx.json`（学习通 cookie、
设备指纹与 sc/cid）、`logs/`、`bin/curl.exe`。

---

## 跑起来

源码：

```bat
python app_qt.pyw
```

打包：

```bat
python -m PyInstaller HFUTone.spec --noconfirm --clean
:: 产物 dist\HFUTone\HFUTone.exe
```

离线自检（不联网，全部走夹具）：

```bat
set YKT_REPLAY=D:/DSHSYBA/HFUTone/fixtures
python src/yktapi.py
```

---

## Android

`android/` 是与桌面版共用业务层的独立 App：原生 Java 外壳 + Chaquopy 内嵌 Python，
WebView 打开 `127.0.0.1` 上的同一套 `web/` 页面。业务代码只有一份（`src/`、`web/`）。

```
android/
├── app/src/main/java/com/hfutone/app/
│   ├── App.java              PyApplication，初始化 Python 运行时
│   ├── MainActivity.java     解压 assets/web → 调 android_boot.boot() → WebView 加载本地端口
│   └── KeepAliveService.java 常驻通知的前台服务（dataSync）
└── app/build.gradle          Chaquopy 3.12、ABI x86_64/arm64、网页资源构建期复制进 assets
```

构建（本机用 Android Studio 自带的 JDK + gradle 9.6）：

```bash
export JAVA_HOME="C:/Program Files/Android/Android Studio/jbr"
export ANDROID_HOME="C:/Users/<你>/AppData/Local/Android/Sdk"
gradle -p android assembleDebug
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
# 产物：android/app/build/outputs/apk/debug/app-debug.apk
```

`local.properties` 不要写成中文路径 —— Java properties 按 ISO-8859-1 读，用户名带中文
会直接找不到 SDK。用 `ANDROID_HOME` 环境变量最省事。

三个只在 Android 侧存在的约束：

1. **配置目录**：apk 里的 assets 与源码树都是只读的。`android_boot.py` 在 `import server`
   之前把 `YKT_CONFIG_DIR` 指到 `files/config`、`HFUT_WEB_DIR` 指到解压出来的 `files/web`，
   顺序不能动 —— `server` 导入时就把这两个路径定死了。
2. **没有 curl**：`YKT_NO_CURL=1` 强制走纯 Python 传输层。
3. **端口会顺延**：force-stop 不走 `onDestroy`，旧监听 socket 可能还没释放，而
   `LocalServer` 又刻意不允许复用地址。所以 boot 从 8765 往后找第一个能绑的端口，
   把实际端口返回给原生侧 —— 原生侧不能假设端口就是 8765。

### 界面：窄屏是单列，不是「桌面缩小版」

`web/mobile.css` 在 `max-width: 820px` 下把整页翻成文档流：整页纵向滚动、主区在上、
雨课堂/学习通在下、层叠课程卡只渲染当前一张、面板不再内滚。桌面规则一条不改。

`web/mobile.js` 只在 UA 含 Android 时给 `<body>` 加 `.is-android`，做三件事：

- **首屏主动拉一次课程**：桌面是「侧栏展开时才拉」，手机上根本没有展开这个动作，
  不拉就会永远停在「正在加载…」。做法是先直连 `/api/ykt/refresh` 把后端的 `loaded`
  置真，再等首轮 pull 回来后调前端 `refresh()` 重绘（只调前端不行：那一刻
  `state.loggedIn` 还是 false，会让 refresh 退化成只 pull 不拉课）。
- **「在线课堂」按钮改成「滚到面板 + 刷新」**：面板在 Android 上常驻，开合没有意义，
  未签到红点仍然挂在按钮上。
- **撤掉直播课表 / 直播画面 / 底部播放条**：直播画面要 Media3/ExoPlayer 重写（桌面是
  ffmpeg 抽帧 + mpv 外部进程）；课表要校园网 + 学号密码，手机上首屏摆一条「加载超时」
  的红字只会让人以为整个 App 坏了。签到走的是雨课堂/学习通，跟这张课表无关。
  设置弹层里「自动推流」「拉取的流」两组同理，一起隐藏。

### 保活：为什么是 specialUse，为什么服务里还要再 boot 一次

`KeepAliveService` 挂常驻通知把进程钉在前台：签到窗口只有开课那几分钟，用户那时多半
在别的应用里等验证码，退到后台被回收就全停了。

两个坑：

1. **前台服务类型不能用 dataSync。** Android 14 起 dataSync 类型在 24 小时内累计只能
   前台跑 6 小时，超时系统直接 `onTimeout` 停服务——正好覆盖掉下午那几节课。改成
   `specialUse`（声明 `PROPERTY_SPECIAL_USE_FGS_SUBTYPE=campus_signin_guard`），没有
   这个时长限制。API 34 起 `startForeground` 要用三参版本且类型与 manifest 一致。
2. **服务单独重启时没有 Activity。** 进程被系统回收后 `START_STICKY` 会把服务拉起来，
   但界面不会出现，Python 的 HTTP 服务也没起——守护等于断了。所以解压 assets 与
   `android_boot.boot()` 抽成了 `Boot.ensure()`，MainActivity 和 KeepAliveService
   都走它，服务侧在后台线程里补一次。

`MainActivity.onDestroy` 刻意不调 `android_boot.shutdown()`：Activity 退出只是界面没了，
HTTP 服务和两条监控线程还在这个进程里跑着，关掉就等于停了守护。

实测：HOME 键退出后 `kill -9` 掉进程，系统 1 秒内重启服务，日志出现
`守护服务：Python 服务就绪 http://127.0.0.1:8766/`（8765 还被上一个进程的 socket
占着，正好验证了端口顺延那段），重新打开 App 直接接到 8766，界面与状态都正常。

在本机模拟器（720×1280 / Android 15）上验证过的：冷启动 1.4 秒出界面、雨课堂状态在
2 秒内由「正在加载…」变成「黄尊鹏 · 暂无在播」、雨课堂/学习通切换、设置弹层（直播两组
已隐藏）、HOME 键退出 25 秒后进程与 HTTP 服务仍在。

## 雨课堂这一栏怎么用

登录三选一：

1. **扫码登录** —— 点【扫码登录】，微信扫一下，手机上点确认。二维码 60 秒过期后自动换新码。
2. **粘贴 cookie** —— 浏览器登录 `changjiang.yuketang.cn`，F12 里把 Cookie 整串复制进【粘贴 cookie】。
3. 已有 cookie 会从 `.config` 里自动读，开箱就是登录态。

登录后：

- **刷新课程** 拉「进行中的课堂」。
- **一键签到** 串行签所有未签的课，每节单独判定。
- **看课件** 拉 `presentationList` 与每页图片（图片经本地代理取，绕开前端跨域）。
- **题目** 连 `wss://…/wsapp/` 拿 `timeline`，解析出单选/多选/填空/判断/主观，填答案直接提交；时限内通道被拒会自动改走补交通道（`problem/retry`）。

### 签到判定：只认服务器的 `signinStatus`

`POST /api/v3/lesson/checkin` 的 `code 0` 只代表**进班成功**——老师还没发起签到时它照样返回 0。
所以"我签上了没"不靠它，也不靠 `rollcall`：

- **`on-lesson` 每节课带的 `signinStatus`** 才是服务器眼中的答案。它 > 0 就直接在列表里标
  「已签到 · 服务器记录」，红点也不亮——换机器、清过配置都不影响。
- **`rollcall` 只用来数活动**。它的 `data` 长这样 `{"2688": 56088391}`（键是签到活动 id，
  值是班号/时间戳），以前那版"遍历前 5 个键、任意值 > 0 就算我签过"是纯脑补：老师一发起点，
  全班都会被判成已签到。现在 `mine` 一律返回 `False`，判定交给 `signinStatus`。
- **`jstr()` 先查顶层再查 `data`**。`msg` 在顶层，旧实现只翻 `data`，拿到的是空串，
  于是"签到成功 / ALREADY / DYNAMIC_QR / LESSON_END"这些文案判断全部失效——这才是最隐蔽的一处。

结论五档：

| 状态 | 含义 | 是否算签到完成 |
| --- | --- | --- |
| `checked` | 服务器确认签到（`签到成功`、`data.status=1`，或动态码课堂但 `signinStatus` 已 > 0） | 是，写本地标记 |
| `already` | 服务器说已签到（`ALREADY`、`已签到, 请勿重复签到`、`您已签到`、`signinStatus` > 0） | 是，写本地标记 |
| `joined` | 只进了班，考勤里还没有记录（老师多半还没发起签到） | 否，红点保持，界面显示「已进班 · 待签到」 |
| `need_scan` | 动态二维码课堂（50070）且服务器没记录，必须手机扫码 | 否 |
| `ended` | 签到已结束（`LESSON_END`） | 否 |

`HFUTone/tests/replay.py` 对着 `fixtures/` 把这几档全跑一遍（`[3]`–`[3e]`），回放用一次性
临时配置目录，不碰真实 `.config`。

---

## 学习通这一栏怎么用

顶栏那个「在线课堂」按钮展开右侧栏，里面是一张卡片，卡片顶部两个 tab：
**雨课堂** / **学习通**。登录状态就写在各自小标题的右边（`● 黄尊鹏 · 暂无在播`）。
切换条在卡片内部，整张卡高度固定（CSS 变量 `--side-panel-h`），所以来回切的时候
下面的直播画面尺寸纹丝不动；卡片自己也不出滚动条——内容多了只有里面的列表在滚，
滚动条本身藏起来（`scrollbar-width: none`）。

点「学习通」tab，再点「登录」填学习通手机号和密码，
走的是网页登录口（`passport2.chaoxing.com/fanyalogin`），和你在浏览器里登录是同一套流程。

登录后点「刷新签到」：它会把所有在上的课的活动列表拉一遍，只留进行中的签到，
每条标出**类型**和**还缺什么**：

| 类型 | 能全自动? | 缺什么 |
| --- | --- | --- |
| 普通签到 | 是（有拍照要求时用上传的图） | — |
| 位置签到 | 是（要坐标，见下） | 位置 |
| 手势签到 | 否 | 手势（九宫格数字，如 `12369`） |
| 签到码签到 | 否 | 签到码 |
| 二维码签到 | 否 | 二维码（用手机扫老师屏幕，把跳转到的 URL 粘进来） |

「一键签到」只签**能全自动**的那几种。缺输入的会跳过并在行上写明缺什么，
点那一行的「签到」会弹出对应输入框，填完自动提交。

### 五个必须照抄的地方

1. **所有签到接口都是 `application/x-www-form-urlencoded`**，不是 JSON。
   `yktcore.request()` 为此加了 `form=` 和 `files=` 两条分支。
2. **签名要 `sc` 和 `cid`。** 它们来自 `getUserInfo` 返回的 `clientId`——那段是拿
   RSA **公钥**做模幂 `c^e mod n` 再剥 PKCS#1 填充解出来的（不是笔误，服务端就这么干）。
   没有它们位置签到签不了，所以登录后一定调一次 `refresh_user()`。
3. **`deviceCode` 每次现算，`uniqueId` 必须固定。** 前者是随机 UUID 过 AES-ECB；
   后者进了 `schild` 和 User-Agent，每次换会被当成异常设备。
4. **`Accept-Encoding: gzip` 不能写进请求头。** curl 不像浏览器那样自动解压，
   带上它服务端就回二进制，`json.loads` 直接炸。要压缩得用 `curl --compressed`。
5. **cookie 要全收，不能只留 `sessionid`。** 雨课堂那套 `parse_set_cookies` 只挑两个键，
   学习通的登录态散在 `_uid / _d / uf / fid / DSSTASH_LOG` 上，少一个就退成未登录，
   所以 `cxapi.parse_all_cookies()` 单独实现了一份。

### 为什么自带一套 AES/RSA

这台机器上 `Crypto` / `cryptography` / `rsa` 一个都没装，而学习通整套接口全靠它们过门。
`src/cxcrypto.py` 是纯 Python 实现（AES-128 ECB/CBC + RSA 模幂 + PKCS#1），
无第三方依赖，打包不会多带任何 DLL。自检用 FIPS-197 官方向量钉死：

```
python src/cxcrypto.py     # [OK] cxcrypto 自检通过
python tests/test_cxsign.py  # 编排层：分类/缺输入/验证码回环/一键自动/二维码解析
```

### 已知限制

- **扫码登录没做界面。** 后端 `/api/cx/qr` 能拿到 `uuid/enc`（实测可用），
  但前端要画二维码得再带一个 QR 编码库，暂时只开账号密码登录。
- **人脸识别走的是原项目那条「复用历史人脸」的路子**：取首次采集的图、改 EXIF、
  重新上传换 objectId。没有历史人脸时它会失败，此时退回不带人脸的提交（服务端会拒并报原因）。
- **验证码要人滑。** 服务端回 `validate_<enc2>` 时行上会显示「缺验证码」，
  点签到弹框填。没有做自动过验证码。

---

## 两条硬性约束（都是踩出来的）

1. **雨课堂的 HTTP 必须走内置 curl。** 学校网关按 TLS 指纹把 .NET/OpenSSL 的请求打成 HTML 拦截页，
   于是所有 JSON 接口都"拿到网页"。`vendor/curl` 是 LibreSSL 版，指纹与浏览器一致；
   `yktcore.request()` 每次 `subprocess` 调它，不走 Python 的 ssl。
2. **课件与答题要的 Header 不一样。** `set-auth`（Bearer）用于 `presentation/fetch` 和答题，
   `lessonToken` 用于 WebSocket 的 `hello.auth`，两个都从签到响应里拿，缺一个都跑不通。

---

## 直播与雨课堂的分层

不管老师用不用雨课堂，HFUT 那一侧都会直播推流——所以直播是底座，雨课堂是附加层，
两者不是平级 tab（那会暗示"二选一"），而是主区 + 可收起侧栏：

- `.split` 横向分栏：`#viewLive`（`flex: 1`）常驻，`#viewYkt`（`.side`，360px）默认收起。
- 开关走 `.is-open` + `flex-basis/opacity/translateX` 过渡（380ms，`cubic-bezier(.22,.61,.36,1)`），
  面板定宽所以展开是"揭开"而不是把文字挤扁；收起动画跑完才 `visibility: hidden`，不留聚焦陷阱。
- 顶栏 `#btnYkt` 开关侧栏；未签到的课堂数落在按钮上的 `.badge` 红点里，不弹窗打断看直播。
- `boot()` 里 `setInterval(pull, 15000)` 全局轮询，不管侧栏开没开都在跑——签到是有时间窗的，
  错过就没了。窗口窄于 1180px 时侧栏转成右侧浮层，不挤压直播区。
- 直播的底栏（开启直播/关闭播放器）始终在，侧栏展开不影响。

---

## 设置、自动推流与内嵌画面

顶栏「设置」打开一个两栏面板，改完立刻写进 `.config/settings.json`：

| 项 | 默认 | 作用 |
| --- | --- | --- |
| 课程开播时自动接画面 | 开 | 程序在线时每 12 秒看一次有没有课开播，一开播就把画面接进来 |
| PPT 独立窗口 | 关 | 侧栏嵌入照旧；勾上就再单开一个播放器窗口，等于最初版本的三路全开 |
| 老师流 / 学生流 | 开 / 关 | 单独开播放器窗口；学生流没有音频，默认关掉 |
| 雨课堂签到通道 | 两条都试 | `两条都试` / `只走扫码` / `只走 App 头`，见下 |
| 学习通位置签到坐标 | 空 | 地址 + 经纬度，位置签到要用，见下 |

勾上「PPT 独立窗口」后，同一路 PPT 会被拉两次（侧栏转码 + 独立窗口），
换来看得见的三路画面。当前课已经在推的话不用重开——自动推流下一轮（最多 12 秒）
就把缺的那路补上；自动推流关着就手动再点一次播放。

### 雨课堂签到走哪条通道

雨课堂的签到有两条互不相同的路，哪条能过取决于老师开的考勤类型：

| 通道 | 做什么 | 什么时候用 |
| --- | --- | --- |
| App 头 | 伪装安卓客户端进班（`x-client: app` + 全套 App UA，Referer 留空） | 能过动态码 50070；**没在真课上验证过** |
| 扫码 | 把二维码 URL 原样 POST 给 `/api/v3/app/scan`，服务器解析出 `lessonId` | 拿到二维码就能用（course_helper 那条路） |

默认 `两条都试`：先扫码（有码时），不通再回退 App 头。多花约 0.3 秒，
但两条腿走路命中率最高。只想走一条就在设置里改。

### 学习通位置签到为什么要在设置里填坐标

位置签到提交时要 `address` + 经纬度，而学习通签到通常只有 **5 秒窗口**——
当场输坐标根本来不及。所以坐标提前配在设置里，签到那一刻直接拿来用。

填了坐标，位置签到才会进「一键签到」的自动范围；没填的话它会标成「缺位置」，
等你点单条时再弹框要。经纬度可以从高德/百度地图上长按地点直接复制。

自动推流只认 `statusName` 为「正在直播／直播中／直播／进行中」的场次；课全部结束后自己收掉，
但**手动拉起的画面不会被它关掉**（那只归用户管）。自动接上画面时侧栏会自动展开一次。

### 内嵌画面为什么不是 `<video src="...m3u8">`

这台机器上的 QtWebEngine 是 **5.15.2 = Chromium 83**，没有编解码器许可：

```
canPlayType('video/mp4; codecs="avc1.42E01E"')  ->  ""      （空）
MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E,mp4a.40.2"')  ->  false
```

学校的流全是 H.264 + AAC，所以 m3u8/rtmp 放进网页必然是黑框。做法是把解码搬到 ffmpeg：

```
ffmpeg -i <rtmp> -vf fps=20 -pix_fmt yuvj420p -q:v 4 -f image2pipe -vcodec mjpeg pipe:1
        ↓JPEG 帧
本机 WebSocket（/api/live/ppt/ws）→ 页面 canvas 逐帧画
```

- `-pix_fmt yuvj420p` 不能省：mjpeg 只吃 full-range，不加这行 ffmpeg 直接以 `-22` 退出。
- `-fflags nobuffer -flags low_delay -probesize 65536 -analyzeduration 0` 只给 rtmp/http 源；
  本地文件加上它一帧都读不出（mp4 的 moov 在末尾），所以按源类型分档。
- 读帧线程按 `FFD8…FFD9` 切帧，分辨率从 JPEG 的 SOF 段读出来，所以 canvas 内部分辨率
  = 源分辨率（1080p 就是 1920×1080），不是先缩再糊。CSS 只负责把它缩到 310px 显示。
- 卡片小，看小字时**点一下画面**就铺满窗口（Esc 或再点一下退回）。
- 侧栏宽度可以拖：左边那条竖线（`.side-grip`）往左拉，PPT 画面跟着变宽，双击复位；
  拖过的宽度存在 `localStorage` 的 `hfutone.side-w`，上限是窗口宽度的 62%（再宽直播区就没地方了）。
- 纵向空间是抢出来的，不是滚出来的：`.side` 不滚动，`.live-card` 高度跟着画面走（16:9，不留黑边），
  空间不够时先压雨课堂面板（它内部自己滚），画面无论如何都完整留在视野里。
- 别在侧栏里给放大层用 `position: fixed`：`.side` 有 `overflow:hidden` 和过渡用的 `transform`，
  会把它当成 containing block，铺不开；放大层挂在 body 下。`inset` 简写同样不能写，
  Chromium 83 不认，得老老实实 `top/right/bottom/left`。