"""Generate the Inno Setup wizard bitmaps from clipforge/resources/logo.png.

Run: .venv/Scripts/python.exe installer/make_wizard_images.py
The generated .bmp files are committed so building the installer needs no Pillow.
"""
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
LOGO = HERE.parent / "clipforge" / "resources" / "logo.png"


def make(size: tuple, out: str, fill: float) -> None:
    logo = Image.open(LOGO).convert("RGB")
    bg = logo.getpixel((2, 2))              # logo background colour, so it blends in
    canvas = Image.new("RGB", size, bg)
    side = int(min(size) * fill)
    mark = logo.resize((side, side), Image.LANCZOS)
    canvas.paste(mark, ((size[0] - side) // 2, (size[1] - side) // 2 if size[0] == size[1] else size[1] // 4))
    canvas.save(HERE / out, format="BMP")


if __name__ == "__main__":
    make((164, 314), "wizard-large.bmp", 1.0)
    make((55, 55), "wizard-small.bmp", 1.0)
    # 2x variants for high-DPI displays
    make((328, 628), "wizard-large@2x.bmp", 1.0)
    make((110, 110), "wizard-small@2x.bmp", 1.0)
