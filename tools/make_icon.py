"""Build the app icon set from the brand logo.

Input : clipforge/resources/logo.png        (full logo: "C" mark + wordmark, dark background)
Output: clipforge/resources/icon.png        (512 px rounded tile with the C mark)
        clipforge/resources/icon.ico        (16-256 px, for the exe, installer and shortcuts)

Run:  .venv/Scripts/python.exe tools/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "clipforge" / "resources"
# Where the "C" mark sits inside the 1254 px square logo (left, top, right, bottom).
MARK_BOX = (420, 350, 840, 700)
SIZES = [16, 24, 32, 48, 64, 128, 256]
MASTER = 1024


def build_master() -> Image.Image:
    logo = Image.open(RES / "logo.png").convert("RGBA")
    scale = logo.width / 1254
    box = tuple(int(v * scale) for v in MARK_BOX)
    mark = logo.crop(box)
    bg = logo.getpixel((8, 8))                       # the logo's own background colour
    tile = Image.new("RGBA", (MASTER, MASTER), bg)
    target_w = int(MASTER * 0.64)
    mark = mark.resize((target_w, int(mark.height * target_w / mark.width)), Image.LANCZOS)
    tile.alpha_composite(mark, ((MASTER - mark.width) // 2, (MASTER - mark.height) // 2))
    mask = Image.new("L", (MASTER, MASTER), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, MASTER - 1, MASTER - 1),
                                           radius=int(MASTER * 0.22), fill=255)
    tile.putalpha(mask)
    return tile


def main() -> None:
    master = build_master()
    master.resize((512, 512), Image.LANCZOS).save(RES / "icon.png")
    master.save(RES / "icon.ico", sizes=[(s, s) for s in SIZES])
    print("wrote", RES / "icon.png", "and", RES / "icon.ico")


if __name__ == "__main__":
    main()
