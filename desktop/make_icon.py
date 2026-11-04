# -*- coding: utf-8 -*-
"""生成桌面壳图标：墨色圆角底 + 用户提供的三绿气泡图案（与微信本体拉开区分）。

素材：desktop/assets/bubbles.png（512x512 透明底）。改素材后重跑本脚本再构建。
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
ART = ROOT / "assets" / "bubbles.png"
ICON_DIR = ROOT / "src-tauri" / "icons"

TILE = (34, 39, 43, 255)          # 墨色底，接近 UI 左栏深色
TILE_RADIUS = 0.24                 # 圆角比例
ART_RATIO = 0.74                   # 图案占底的比例


def compose(size: int) -> Image.Image:
    scale = 4 if size <= 128 else 2
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * TILE_RADIUS), fill=TILE)
    art = Image.open(ART).convert("RGBA")
    inner = int(s * ART_RATIO)
    art = art.resize((inner, inner), Image.LANCZOS)
    off = (s - inner) // 2
    img.alpha_composite(art, (off, off))
    return img.resize((size, size), Image.LANCZOS)


def main():
    if not ART.exists():
        raise SystemExit(f"缺少素材 {ART}")
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for name, size in {"32x32.png": 32, "128x128.png": 128,
                       "128x128@2x.png": 256, "icon.png": 512}.items():
        compose(size).save(ICON_DIR / name)
    base = compose(256)
    base.save(ICON_DIR / "icon.ico",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"icons written to {ICON_DIR}")


if __name__ == "__main__":
    main()
