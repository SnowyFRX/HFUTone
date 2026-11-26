# 工作约定

## 改 HFUTone 之前先读 git

任何升级、改动、修 bug 之前，先看历史再动手：

```bash
cd D:/DSHSYBA/HFUTone
git log --oneline -20              # 最近改了什么
git log -p -- src/<改动的文件>      # 这个文件一路怎么演变
git diff HEAD                      # 有没有未提交的改动
git show <commit>                  # 某个决定的完整上下文
```

为什么：这个项目里很多结论是**实测出来的**（哪些接口能通、哪条路被证伪），
提交信息里记着"为什么这么做"。重写之前不读，就会把已经排除的死路又走一遍。

典型例子（都在提交历史里）：
- 扫码登录被证伪：扫码只能拿 web 凭据，配 App 头一律 `50000`
- App 端没有取码接口：`/api/v3/user/login/app/pre-info` 等 10 个候选全 404
- 只有 `x-client: app` 这一个头触发 App 鉴权分支，其它头都不影响
- App 凭据通吃：`on-lesson` 在 App 头和 web 头下都 200

## 发布物里不许有登录态

`dist/HFUTone/.config/` 是程序自己生成的运行目录，里面是使用者本人的东西：
`ykt.json`（App cookie）、`ykt-cookie.txt`（sessionid）、`session.json`（网页 token）、
`cx.json`、`courses.json`（课表）、`logs/`。这些**任何情况下都不能进发布物**。

2026-11-24 出过一次事故：手工把 `dist/HFUTone` 整个压成 zip 挂到 Release，
`.config` 一起发了出去 —— 公开附件等于把登录态发给全世界，只能靠本人重新登录轮换。

所以发布只走两条，别手工压包：

```bash
python tools/make_release_zip.py                       # 白名单排除 + 收尾复检
python tools/release_check.py --zip HFUTone-*.zip      # 独立自检（产物层面）
python tools/release_check.py --dir <任意目录>          # 打包前预检
```

`tools/release_check.py` 独立于打包脚本：它只看产物里有什么，三层判据是
路径（`.config/`、`logs/`、`*.log`）、标记（`.config/.do-not-ship`，yktcore
每次启动会写，目录被改名复制也认得出）、内容（小文本里的 `sessionid=`、
`app_cookie`、`"token": "..."`）。命中即退出码 1，别赌。

## 提交前的闸（已装，新 clone 要手动启一次）

```bash
git config core.hooksPath .githooks     # 指向仓库里的 .githooks/pre-commit
```

`.githooks/pre-commit` 调 `python tools/release_check.py --git`：暂存区里只要有
凭据文件名、`.config/`、`*.log`，或者文本里出现 `sessionid=` 这类真凭据形态，
提交就被拒。GitHub 侧还有 `.github/workflows/privacy.yml` 用同一脚本兜一遍
（本地 hook 没装、或有人 `--no-verify`，CI 仍会红）。

误报的处理方式：把那个词加进 `tools/release_check.py` 的 `PLACEHOLDER_HINTS`
（`replay`、`{token}`、`<32HEX>` 这些已经在里面了），而不是给提交加 `--no-verify`。

## 发布节奏

- `1.x`（x 升级）：**新建**一个 Release。
- `1.x.y`（y 升级）：**替换**上一个 Release——沿用同一个页面，改它的 tag、标题和
  说明，附件换成新版本，不要另开一页。
- 发版后**立刻 purge 两条 jsDelivr 路径**（`@latest` 是主路，`@main` 是兜底），
  否则「检查更新」会读到旧版本。实测 purge 后 12 秒即读到新版本号：

  ```bash
  curl -sS "https://purge.jsdelivr.net/gh/SnowyFRX/HFUTone@latest/web/version.json"
  curl -sS "https://purge.jsdelivr.net/gh/SnowyFRX/HFUTone@main/web/version.json"
  ```
  返回 `"status": "finished"` 即生效。

## 提交习惯

- **提交、推送、移动 tag 之前必须先问过用户**，得到明确同意再执行。不要自己
  commit / push / `git tag -f`。
- 一次改动一个提交，提交信息**要短**：标题一行说清改了什么，正文最多一两句讲原因。
  排查过程、验证数据、长篇推理不要写进提交信息。
- 入库前确认没有 `.config/`（含 cookie、登录日志）、`dist/`、`vendor/ffmpeg/`。
- 常用：`git add -A && git commit -m "..."`，不要 `--amend` 已经推送过的提交。