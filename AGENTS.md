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

## 提交习惯

- 一次改动一个提交，提交信息写**结论和原因**，不只写"改了啥"。
- 入库前确认没有 `.config/`（含 cookie、登录日志）、`dist/`、`vendor/ffmpeg/`。
- 常用：`git add -A && git commit -m "..."`，不要 `--amend` 已经推送过的提交。