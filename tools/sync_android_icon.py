"""把桌面版图标同步成安卓各密度的 launcher 图标。

安卓 launcher 图标分两套：
  1. 传统方形 ic_launcher.png（API < 26 用）—— 各密度固定尺寸
  2. 自适应图标 mipmap-anydpi-v26/ic_launcher.xml —— 前景 + 背景两层，
     前景内容必须留安全边距（系统会按不同形状裁切，圆的方的都可能）

源图是 1024×1024 的 RGBA，带透明边。自适应图标的前景层按 Android 规范
只占中间 72dp/108dp，所以要把原图缩到 72% 再居中贴到透明画布上，
否则圆形裁切时四角会被切掉。

用法：python tools/sync_android_icon.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print('需要 Pillow：pip install Pillow')
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'assets' / '_src' / 'icon-1024.png'
RES = ROOT / 'android' / 'app' / 'src' / 'main' / 'res'

# 传统图标：密度 -> 边长(px)
DENSITIES = {
    'mipmap-mdpi': 48,
    'mipmap-hdpi': 72,
    'mipmap-xhdpi': 96,
    'mipmap-xxhdpi': 144,
    'mipmap-xxxhdpi': 192,
}

# 自适应图标前景：108dp 画布，内容区 72dp（留 18dp 安全边距）
FOREGROUND = {
    'mipmap-mdpi': 108,
    'mipmap-hdpi': 162,
    'mipmap-xhdpi': 216,
    'mipmap-xxhdpi': 324,
    'mipmap-xxxhdpi': 432,
}


def main() -> int:
    if not SRC.is_file():
        print(f'源图不存在：{SRC}')
        return 1
    base = Image.open(SRC).convert('RGBA')
    print(f'源图 {SRC.name} {base.size[0]}×{base.size[1]}')

    # 1) 传统方形图标
    for folder, size in DENSITIES.items():
        out = RES / folder
        out.mkdir(parents=True, exist_ok=True)
        img = base.resize((size, size), Image.LANCZOS)
        img.save(out / 'ic_launcher.png', 'PNG', optimize=True)
        # 圆形图标（部分启动器会用）
        img.save(out / 'ic_launcher_round.png', 'PNG', optimize=True)
        print(f'  {folder}/ic_launcher.png  {size}×{size}')

    # 2) 自适应图标前景层：内容缩到 72/108，居中放透明画布
    for folder, canvas in FOREGROUND.items():
        out = RES / folder
        out.mkdir(parents=True, exist_ok=True)
        inner = max(1, round(canvas * 72 / 108))
        fg = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
        scaled = base.resize((inner, inner), Image.LANCZOS)
        off = (canvas - inner) // 2
        fg.paste(scaled, (off, off), scaled)
        fg.save(out / 'ic_launcher_foreground.png', 'PNG', optimize=True)
        print(f'  {folder}/ic_launcher_foreground.png  {canvas}×{canvas} (内容 {inner})')

    # 3) 自适应图标 XML + 背景色
    anydpi = RES / 'mipmap-anydpi-v26'
    anydpi.mkdir(parents=True, exist_ok=True)
    xml = '''<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@color/ic_launcher_background" />
    <foreground android:drawable="@mipmap/ic_launcher_foreground" />
</adaptive-icon>
'''
    (anydpi / 'ic_launcher.xml').write_text(xml, 'utf-8')
    (anydpi / 'ic_launcher_round.xml').write_text(xml, 'utf-8')
    print(f'  {anydpi.name}/ic_launcher.xml (+ round)')

    # 4) 背景色（取源图左上角不透明像素的近似色；这里用页面米色打底）
    values = RES / 'values'
    values.mkdir(parents=True, exist_ok=True)
    colors = values / 'colors.xml'
    text = colors.read_text('utf-8') if colors.exists() else ''
    if 'ic_launcher_background' not in text:
        if '<resources>' in text:
            text = text.replace('<resources>',
                                '<resources>\n    <color name="ic_launcher_background">#F7F4ED</color>')
        else:
            text = ('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
                    '    <color name="ic_launcher_background">#F7F4ED</color>\n</resources>\n')
        colors.write_text(text, 'utf-8')
        print(f'  values/colors.xml  ic_launcher_background=#F7F4ED')

    print('完成')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
