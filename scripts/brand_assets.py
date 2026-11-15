"""Готовит файлы логотипа для сайта из исходника brand/logo-master.png (PNG с прозрачным фоном).

    python scripts/brand_assets.py

Круг логотипа (центр и радиус в пикселях исходника) нужен для значков приложения:
из него вырезается «медальон». Если исходник заменить, проверьте CIRCLE.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "brand" / "logo-master.png"
OUT = ROOT / "booking" / "static" / "booking" / "brand"
CIRCLE = (499.7, 344.6, 343.0)  # центр x, y и радиус круга с лыжником
RING = (2, 52, 78)  # тёмно-синий внешний круг — фон значков


def resized(img: Image.Image, width: int) -> Image.Image:
    height = round(img.height * width / img.width)
    return img.resize((width, height), Image.Resampling.LANCZOS)


def medallion(img: Image.Image) -> Image.Image:
    cx, cy, r = CIRCLE
    box = (round(cx - r), round(cy - r), round(cx + r), round(cy + r))
    disk = img.crop(box)
    size = disk.width
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(disk, (0, 0), mask)
    return out


def icon(disk: Image.Image, size: int, scale: float, background: bool) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), RING + (255,) if background else (0, 0, 0, 0))
    inner = round(size * scale)
    d = disk.resize((inner, inner), Image.Resampling.LANCZOS)
    canvas.alpha_composite(d, ((size - inner) // 2, (size - inner) // 2))
    return canvas


def main():
    logo = Image.open(SRC).convert("RGBA")
    OUT.mkdir(parents=True, exist_ok=True)
    # Логотип целиком: страница входа (до 360 px шириной) и шапка (высота 44 px) — с запасом для чётких экранов
    resized(logo, 760).save(OUT / "logo.webp", quality=88, method=6)
    header = logo.resize((round(logo.width * 96 / logo.height), 96), Image.Resampling.LANCZOS)
    header.save(OUT / "logo-header.webp", quality=90, method=6)
    disk = medallion(logo)
    icon(disk, 32, 1.0, False).save(OUT / "favicon-32.png", optimize=True)
    icon(disk, 180, 1.0, True).convert("RGB").save(OUT / "apple-touch-icon.png", optimize=True)
    for size in (192, 512):  # maskable: главное — в центральных 80%
        icon(disk, size, 0.9, True).convert("RGB").save(OUT / f"icon-{size}.png", optimize=True)
    for f in sorted(OUT.iterdir()):
        print(f"{f.name}: {f.stat().st_size // 1024} КБ")


if __name__ == "__main__":
    main()
