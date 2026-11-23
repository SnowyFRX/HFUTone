# 在桌面创建 HFUTone 快捷方式（指向打包好的 exe）
$ErrorActionPreference = 'Stop'

$exe = 'D:\DSHSYBA\HFUTone\dist\HFUTone\HFUTone.exe'
if (-not (Test-Path $exe)) { throw "找不到 $exe" }

$desktop = [Environment]::GetFolderPath('Desktop')
Write-Output "桌面目录: $desktop"

$lnk = Join-Path $desktop 'HFUTone.lnk'
$icon = 'D:\DSHSYBA\HFUTone\assets\hfutlive.ico'

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = $exe
$sc.WorkingDirectory = Split-Path $exe -Parent
$sc.Description = 'HFUTone · 工大直播课堂与长江雨课堂'
if (Test-Path $icon) { $sc.IconLocation = "$icon,0" } else { $sc.IconLocation = "$exe,0" }
$sc.WindowStyle = 1
$sc.Save()

if (Test-Path $lnk) {
    $f = Get-Item $lnk
    Write-Output "已创建: $($f.FullName)  ($($f.Length) 字节)"
} else {
    throw '快捷方式没有生成'
}