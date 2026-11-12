"""Generate clipforge/resources/icon.png (512 px) and icon.ico (16-256 px) with Pillow.

Run:  .venv/Scripts/python.exe tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "clipforge" / "resources"
MASTER = 1024                      # drawn large, then downsampled for clean edges
TOP_LEFT = (46, 128, 255)          # blue
BOTTOM_RIGHT = (139, 82, 255)      # violet


def _gradient(size: int) -> Image.Image:
    """Diagonal blue -> violet gradient (top-left to bottom-right)."""
    horiz = Image.linear_gradient("L").rotate(90).resize((size, size))
    vert = Image.linear_gradient("L").resize((size, size))
    mix = ImageChops.add(horiz.point(lambda v: v // 2), vert.point(lambda v: v // 2))
    return Image.composite(Image.new("RGB", (size, size), BOTTOM_RIGHT),
                           Image.new("RGB", (size, size), TOP_LEFT), mix)


def build_master() -> Image.Image:
    s = MASTER
    margin = int(s * 0.04)
    radius = int(s * 0.23)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((margin, margin, s - margin, s - margin), radius, fill=255)

    base = _gradient(s).convert("RGBA")
    # soft highlight near the top for depth
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-s * 0.2, -s * 0.55, s * 1.2, s * 0.45), fill=(255, 255, 255, 0))
    base = Image.alpha_composite(base, glow.filter(ImageFilter.GaussianBlur(s * 0.03)))
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    img.paste(base, (0, 0), mask)

    # play triangle (rounded corners via supersampled polygon + blur-threshold)
    cx, cy = s * 0.515, s * 0.5
    h = s * 0.50
    w = h * 0.88
    pts = [(cx - w / 2, cy - h / 2), (cx - w / 2, cy + h / 2), (cx + w / 2, cy)]
    tri = Image.new("L", (s, s), 0)
    ImageDraw.Draw(tri).polygon(pts, fill=255)
    tri = tri.filter(ImageFilter.GaussianBlur(s * 0.018)).point(lambda v: 255 if v > 128 else 0)
    tri = tri.filter(ImageFilter.GaussianBlur(1.2))

    # split notch: a diagonal cut through the triangle, with a gap showing the gradient
    cut = Image.new("L", (s, s), 0)
    gap = s * 0.028
    d = ImageDraw.Draw(cut)
    x0, x1 = cx - w * 0.22, cx + w * 0.12
    d.polygon([(x0 - gap, cy + h * 0.62), (x0 + gap, cy + h * 0.62),
               (x1 + gap, cy - h * 0.62), (x1 - gap, cy - h * 0.62)], fill=255)
    cut = cut.filter(ImageFilter.GaussianBlur(1.2))
    tri_cut = ImageChops.multiply(tri, ImageChops.invert(cut))

    shadow = tri_cut.filter(ImageFilter.GaussianBlur(s * 0.025))
    shadow_layer = Image.new("RGBA", (s, s), (20, 10, 70, 0))
    shadow_layer.putalpha(shadow.point(lambda v: int(v * 0.35)))
    shadow_layer = ImageChops.offset(shadow_layer, 0, int(s * 0.015))
    img = Image.alpha_composite(img, shadow_layer)
    white = Image.new("RGBA", (s, s), (255, 255, 255, 255))
    white.putalpha(tri_cut)
    img = Image.alpha_composite(img, white)
    # keep everything inside the rounded square
    img.putalpha(ImageChops.multiply(img.getchannel("A"), mask.filter(ImageFilter.GaussianBlur(0.6))))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    master = build_master()
    png = master.resize((512, 512), Image.LANCZOS)
    png.save(OUT / "icon.png")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [master.resize((n, n), Image.LANCZOS) for n in sizes]
    frames[-1].save(OUT / "icon.ico", format="ICO", sizes=[(n, n) for n in sizes],
                    append_images=frames[:-1])
    print("wrote", OUT / "icon.png", OUT / "icon.ico")


if __name__ == "__main__":
    main()
