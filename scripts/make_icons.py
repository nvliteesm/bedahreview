"""Draw the app icons into src/bedahreview/static/icons/. Needs Pillow (`pip install pillow`); not a runtime dependency.

    python scripts/make_icons.py

The PNGs are committed, so you only run this to change the icon.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "src" / "bedahreview" / "static" / "icons"
FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
ACCENT = "#0f6b5a"


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise SystemExit("No bold font found; add one to FONTS.")


def make(size: int, maskable: bool, name: str) -> None:
    base = Image.new("RGBA", (size, size), ACCENT)
    if maskable:  # full-bleed square, letter inside the central safe zone
        img, letter = base, 0.42
    else:  # rounded corners on a transparent background
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=255)
        img.paste(base, (0, 0), mask)
        letter = 0.55
    ImageDraw.Draw(img).text((size / 2, size / 2), "B", font=font(int(size * letter)), fill="white", anchor="mm")
    img.save(OUT / name)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    make(192, False, "icon-192.png")
    make(512, False, "icon-512.png")
    make(512, True, "icon-maskable-512.png")
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))
