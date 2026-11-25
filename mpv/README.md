# 内置播放器 mpv

`mpv.exe.xz` 是 `mpv.exe` 的 xz 压缩包。原文件 117 MB，超过 GitHub 单文件
100 MB 的限制，所以存压缩版（33 MB），打包时由 `build.py` 解压到
`dist/HFUTone/mpv/mpv.exe`。

## 来源

- 构建：`Mpv-x86_64-v3-20260118`（shinchiro 的 Windows 构建）
- 版本：`mpv v0.41.0-85-g468d34c9b`，2026-01-18 编译
- 架构：x86_64，v3（需要 AVX2；老 CPU 请换 v2 构建）

## 更新方法

```bash
xz -9 -e -k mpv.exe          # 生成 mpv.exe.xz
mv mpv.exe.xz mpv/mpv.exe.xz
python build.py              # 打包含新播放器
```

打包后用 `mpv/mpv.exe.xz` 解出来的 `mpv.exe` 会被 `hfutlive.find_player`
优先选中（排在 PATH 与目录扫描之前）。

## 为什么不直接入库 exe

GitHub 拒绝单个文件超过 100 MB，push 会直接失败。远端 1.4.4 那版包里的
mpv 是手工放进去的，所以仓库里没有它，别人 clone 下来打包就少一个播放器。