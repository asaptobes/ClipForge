"""Render the ClipForge promo clip (1080x1920, 30 fps, 18 s): a 3D product reel.

The REAL interface captures in promo/ui/ are mapped onto a tilted glass slab that floats in space.  A small
pinhole-camera pipeline (numpy + Pillow's PERSPECTIVE transform) flies around it: wide hero shot, close-ups of
the timeline for SPLIT / DELETE / DETACH AUDIO, then the Export dialog floating above the window.  The edits are
animated by compositing real pixels cropped from the captures (clips lift off in z, fly away, slide, peel
audio off the video ...).  Titles are extruded 3D cards.  Pure Python: numpy + Pillow, frames piped to FFmpeg.

Run:  .venv/Scripts/python.exe tools/make_promo.py                     -> promo/ClipForge-promo-9x16.mp4
      .venv/Scripts/python.exe tools/make_promo.py --stills 1,4,8      -> promo/stills/tXX.XX.png
      .venv/Scripts/python.exe tools/make_promo.py --sheet 0,0.5,1,...  -> promo/sheet.png (360x640 thumbs)
      .venv/Scripts/python.exe tools/make_promo.py --video-sheet 16     -> promo/final_sheet.png (from the mp4)

Tweaking: timings, camera keyframes (CAM_KEYS), captions (CAPTIONS) and copy are constants / one function per
scene below.  Everything is a function of time t (seconds) only, so any frame can be rendered alone.
"""

from __future__ import annotations

import json
import math
import multiprocessing as mp
import subprocess
import sys
import time as _time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from clipforge import paths  # noqa: E402

W, H, FPS, DUR = 1080, 1920, 30, 659 / 30   # was 20.0; 659 frames (CTA keeps its 2.95 s length after the shift)
OUT_DIR = ROOT / "promo"
UI = OUT_DIR / "ui"

# ---------------------------------------------------------------- scene timing (seconds)
T_FLY0 = 3.95             # interface slab starts flying in (hook + logo are time-compressed to fit before it)
OLD_FLY0 = 4.25           # ... the hook/logo scenes are authored against this timeline
T_DEMO_END = 15.3         # 3D interface demo until here
T_FEAT_END = T_DEMO_END + 3.7   # feature chips until here (was 17.05, a 1.75 s scene), then CTA
T_FEAT_FADE = 0.2        # features scene fades out over its last 0.2 s
T_FLASH = (2.23, T_DEMO_END, T_FEAT_END)

# --- DOWNLOAD beat (absolute seconds)
T_CLICK = 5.00            # cursor clicks the URL field (focus ring)
TYPE_T = (5.08, 5.62)     # URL types in character by character
T_ENTER = 5.80            # -> "Fetching video info..."
T_CARD = 6.10             # info card pops in with a z-lift
T_CARD_LAND = 6.48
T_DL = 6.95               # Download button clicked
PROG_T = (7.00, 7.60)     # progress fill 0 -> 100 %
T_DONE = 7.95             # complete: flash, panel clears, clip appears on the timeline
T_DROP = (8.05, 8.50)     # clip drops onto the timeline in z

# --- edit beats: the original SPLIT ... EXPORT choreography, shifted by D
D = 2.3
GLIDE1 = (5.98 + D, 6.50 + D)     # playhead 49 -> 1148
SPLIT1 = 6.55 + D
GLIDE2 = (6.80 + D, 7.28 + D)     # playhead 1148 -> 2063
SPLIT2 = 7.30 + D
SELECT_T = 7.50 + D       # click on the middle clip
LIFT_T = 7.90 + D         # middle clip leaves the surface
FLY_T = (8.20 + D, 8.75 + D)      # ... and flies away
SLIDE_T = (8.30 + D, 8.85 + D)    # right clip ripples left
FIT_T = (8.95 + D, 9.30 + D)      # timeline re-fits (clips stretch)
DETACH_T = 9.60 + D       # 07 -> 08 (audio lane appears)
PEEL_T = (9.80 + D, 10.45 + D)    # audio strips peel off and drop into the audio lane
AUDIO_SLIDE_T = (10.55 + D, 11.05 + D)
EXPORT_CLICK_T = 11.70 + D
DIALOG_T = (11.78 + D, 12.20 + D)
PROGRESS_T = (12.30 + D, 12.72 + D)

# camera keyframes for the interface scene: (t, target_x, target_y, zoom, yaw, pitch, roll)
# target is in window-capture pixels (3200x2000); zoom = screen px per capture px at the target.
CAM_KEYS = [
    (T_FLY0, 1600, 1250, 0.150, 58, -28, 12),   # far away, spun round
    (4.85, 370, 430, 1.34, -14, -10, -1),       # close-up of the Download panel (left of the window)
    (6.45, 370, 450, 1.38, -12, -9, 0),
    (7.95, 370, 520, 1.46, -12, -9, 0),         # slow push while the bar fills
    (8.35, 1150, 1230, 0.50, -19, -11, -1),     # pull back + pan right: the clip lands on the timeline
] + [(t + D, *rest) for t, *rest in [
    (6.50, 800, 1330, 1.20, -15, -13, -2),      # close-up: first cut
    (6.78, 880, 1330, 1.20, -13, -12, -1),
    (7.32, 1560, 1340, 1.05, -4, -12, 1),       # follow the playhead to the second cut
    (7.62, 1600, 1350, 1.05, 9, -14, 2),        # close-up on the middle clip
    (8.40, 1700, 1380, 0.95, 15, -15, 3),
    (9.45, 2250, 1400, 0.92, 11, -13, 0),       # stretched clips
    (9.90, 1750, 1480, 1.15, 12, -16, -2),      # audio lane
    (10.85, 2050, 1480, 1.05, 4, -14, 1),
    (11.50, 1600, 1200, 0.33, 25, -10, 2),      # pull out for the export button
    (12.05, 1600, 1060, 0.52, -10, -7, -1),     # push in on the dialog
    (13.00, 1600, 1060, 0.57, -3, -5, 0),
]]

CAPTIONS = [  # text, start, end, chip
    ("PASTE A LINK.", 4.45, 5.75, None),
    ("DOWNLOAD.", 5.82, 8.10, "01"),
    ("SPLIT.", 5.95 + D, 7.45 + D, "02"),
    ("DELETE.", 7.52 + D, 9.22 + D, "03"),
    ("DETACH AUDIO.", 9.30 + D, 11.12 + D, "04"),
    ("EXPORT.", 11.30 + D, 13.0 + D, "05"),
]

BG_TOP, BG_BOT = (10, 15, 36), (6, 8, 19)
BLUE, VIOLET = (59, 130, 255), (139, 82, 255)
WHITE, MUTED = (245, 247, 255), (140, 150, 185)
GREEN, RED = (52, 211, 153), (255, 77, 109)
LANE_BG = (17, 18, 21)
PANEL_BG, FIELD_BG = (19, 20, 23), (21, 22, 26)
TRACK_BG, FILL_BLUE = (21, 22, 26), (77, 141, 255)

F_BLACK = "C:/Windows/Fonts/seguibl.ttf"
F_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
F_SEMI = "C:/Windows/Fonts/seguisb.ttf"

FOCAL = 1250.0
SLAB_T = 95.0             # slab thickness in capture pixels


# ================================================================== easing helpers
def cl(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def prog(t: float, a: float, b: float) -> float:
    return cl((t - a) / (b - a))


def out_cubic(x: float) -> float:
    x = cl(x)
    return 1 - (1 - x) ** 3


def out_back(x: float, s: float = 1.9) -> float:
    x = cl(x) - 1
    return 1 + (s + 1) * x ** 3 + s * x ** 2


def in_out(x: float) -> float:
    x = cl(x)
    return x * x * (3 - 2 * x)


def in_out5(x: float) -> float:
    x = cl(x)
    return x * x * x * (x * (6 * x - 15) + 10)


def lerp(a: float, b: float, x: float) -> float:
    return a + (b - a) * x


def mix(c1, c2, x: float):
    return tuple(int(lerp(a, b, x)) for a, b in zip(c1, c2))


# ================================================================== 2D drawing primitives
def blit(img: Image.Image, spr: Image.Image, x: float, y: float, a: float = 1.0) -> None:
    """Alpha-composite an RGBA sprite onto an RGB image at (x, y) with extra opacity ``a``."""
    if a <= 0.004:
        return
    x, y = int(round(x)), int(round(y))
    iw, ih = img.size
    sw, sh = spr.size
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + sw, iw), min(y + sh, ih)
    if x0 >= x1 or y0 >= y1:
        return
    crop = spr.crop((x0 - x, y0 - y, x1 - x, y1 - y))
    alpha = crop.getchannel("A")
    if a < 1:
        alpha = alpha.point(lambda v: int(v * a))
    img.paste(crop.convert("RGB"), (x0, y0), alpha)


@lru_cache(maxsize=96)
def glow_sprite(r: int, color: tuple) -> Image.Image:
    yy, xx = np.mgrid[-r:r, -r:r]
    d = np.sqrt(xx ** 2 + yy ** 2) / r
    a = np.clip(1 - d, 0, 1) ** 2
    arr = np.zeros((2 * r, 2 * r, 4), np.uint8)
    arr[..., :3] = color
    arr[..., 3] = (a * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def glow(img: Image.Image, cx: float, cy: float, r: int, color: tuple, a: float) -> None:
    r = max(2, int(r) // 2 * 2)
    blit(img, glow_sprite(r, tuple(color)), cx - r, cy - r, a)


@lru_cache(maxsize=512)
def rr_mask(w: int, h: int, r: int) -> Image.Image:
    """Anti-aliased rounded-rectangle alpha mask (corner tiles only, so it is cheap even for 3200x2000)."""
    r = max(1, min(r, w // 2, h // 2))
    s = 4
    tile = Image.new("L", (2 * r * s, 2 * r * s), 0)
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, 2 * r * s - 1, 2 * r * s - 1), r * s, fill=255)
    tile = tile.resize((2 * r, 2 * r), Image.LANCZOS)
    m = Image.new("L", (w, h), 255)
    m.paste(tile.crop((0, 0, r, r)), (0, 0))
    m.paste(tile.crop((r, 0, 2 * r, r)), (w - r, 0))
    m.paste(tile.crop((0, r, r, 2 * r)), (0, h - r))
    m.paste(tile.crop((r, r, 2 * r, 2 * r)), (w - r, h - r))
    return m


@lru_cache(maxsize=256)
def hgrad(w: int, h: int, c1: tuple, c2: tuple, vertical: bool = False) -> Image.Image:
    n = h if vertical else w
    t = np.linspace(0, 1, n)[:, None]
    line = np.array(c1)[None, :] * (1 - t) + np.array(c2)[None, :] * t
    arr = np.repeat(line[:, None, :], w, axis=1) if vertical else np.repeat(line[None, :, :], h, axis=0)
    return Image.fromarray(arr.astype(np.uint8), "RGB")


def _box(box):
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    return x0, y0, x1 - x0, y1 - y0


def fill_rr(img, box, r, fill, a: float = 1.0) -> None:
    x0, y0, w, h = _box(box)
    if w < 4 or h < 4 or a <= 0.004:
        return
    mask = rr_mask(w, h, min(r, w // 2, h // 2))
    if a < 1:
        mask = mask.point(lambda v: int(v * a))
    src = fill if isinstance(fill, Image.Image) else Image.new("RGB", (w, h), fill)
    if src.size != (w, h):
        src = src.resize((w, h))
    img.paste(src, (x0, y0), mask)


def stroke_rr(img, box, r, color, wd: int, a: float = 1.0) -> None:
    x0, y0, w, h = _box(box)
    if w < 2 * wd + 4 or h < 2 * wd + 4 or a <= 0.004:
        return
    outer = rr_mask(w, h, min(r, w // 2, h // 2))
    iw, ih = w - 2 * wd, h - 2 * wd
    inner = Image.new("L", (w, h), 0)
    inner.paste(rr_mask(iw, ih, max(1, min(r - wd, iw // 2, ih // 2))), (wd, wd))
    ring = ImageChops.subtract(outer, inner)
    if a < 1:
        ring = ring.point(lambda v: int(v * a))
    src = color if isinstance(color, Image.Image) else Image.new("RGB", (w, h), color)
    if src.size != (w, h):
        src = src.resize((w, h))
    img.paste(src, (x0, y0), ring)


def text_w(text: str, size: float, font: str, tracking: float = 0.0) -> float:
    f = ImageFont.truetype(font, max(8, int(size)))
    return sum(f.getlength(c) + tracking * size for c in text) if tracking else f.getlength(text)


@lru_cache(maxsize=2048)
def text_sprite(text, size, font, fill, tracking_px=0, grad=None) -> Image.Image:
    f = ImageFont.truetype(font, size)
    asc, desc = f.getmetrics()
    w = int(sum(f.getlength(c) + tracking_px for c in text)) if tracking_px else int(f.getlength(text))
    pad = size // 4
    mask = Image.new("L", (w + 2 * pad, asc + desc + 2 * pad), 0)
    d = ImageDraw.Draw(mask)
    if tracking_px:
        x = pad
        for c in text:
            d.text((x, pad), c, font=f, fill=255)
            x += f.getlength(c) + tracking_px
    else:
        d.text((pad, pad), text, font=f, fill=255)
    rgb = hgrad(mask.width, mask.height, grad[0], grad[1]) if grad else Image.new("RGB", mask.size, fill)
    spr = rgb.convert("RGBA")
    spr.putalpha(mask)
    return spr


def fit_size(text, size, font, max_w, tracking=0.0):
    w = text_w(text, size, font, tracking)
    return size * max_w / w if max_w and w > max_w else size


def tsprite(text, size, font=F_BLACK, fill=WHITE, grad=None, max_w=None, tracking=0.0):
    """Text sprite fitted to max_w (quantised so the sprite cache stays small)."""
    s = int(round(fit_size(text, size, font, max_w, tracking)))
    return text_sprite(text, s, font, tuple(fill), int(tracking * s), grad)


def put_text(img, text, cx, cy, size, font=F_BLACK, fill=WHITE, a=1.0, tracking=0.0, grad=None, max_w=None):
    if a <= 0.01:
        return
    spr = tsprite(text, size, font, fill, grad, max_w, tracking)
    blit(img, spr, cx - spr.width / 2, cy - spr.height / 2, a)


# ================================================================== brand art
def _load_logo():
    im = Image.open(ROOT / "clipforge" / "resources" / "logo.png").convert("RGB")
    arr = np.asarray(im).astype(np.float32)
    alpha = np.clip((arr.max(axis=2) - 34) / 50, 0, 1) * 255
    full = Image.fromarray(np.dstack([arr, alpha]).astype(np.uint8), "RGBA")
    return full.crop((415, 345, 845, 705)), full.crop((225, 725, 1035, 905))


MARK, WORD = _load_logo()


@lru_cache(maxsize=64)
def logo_sprite(which: str, w: int) -> Image.Image:
    src = MARK if which == "mark" else WORD
    return src.resize((w, int(src.height * w / src.width)), Image.LANCZOS)


# ================================================================== camera + projective warp
def rot(yaw: float, pitch: float, roll: float) -> np.ndarray:
    y, p, r = (math.radians(v) for v in (yaw, pitch, roll))
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    rx = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    rz = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    return rz @ rx @ ry


class Cam:
    """Pinhole camera that looks at world point (tx, ty, tz).  ``zoom`` = screen px per world px at the target."""

    def __init__(self, tx=0.0, ty=0.0, tz=0.0, zoom=1.0, yaw=0.0, pitch=0.0, roll=0.0, f=FOCAL, cx=W / 2, cy=H / 2):
        self.T = np.array([tx, ty, tz], float)
        self.f, self.cx, self.cy = f, cx, cy
        self.D = f / zoom
        self.R = rot(yaw, pitch, roll)

    def cam_pts(self, P) -> np.ndarray:
        Pc = (np.asarray(P, float) - self.T) @ self.R.T
        Pc[:, 2] += self.D
        return Pc

    def project(self, P):
        Pc = self.cam_pts(P)
        z = np.maximum(Pc[:, 2], 1e-3)
        return np.stack([self.cx + self.f * Pc[:, 0] / z, self.cy + self.f * Pc[:, 1] / z], 1), Pc[:, 2]


def persp_coeffs(dst, src):
    """PIL PERSPECTIVE coefficients mapping output (dst) pixels -> source pixels."""
    a, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        b.append(v)
    return tuple(np.linalg.solve(np.array(a, float), np.array(b, float)))


def poly_area(p) -> float:
    x, y = p[:, 0], p[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2


def _pad_canvas(spr: Image.Image, p: int) -> Image.Image:
    w, h = spr.size
    if spr.mode == "L":
        c = Image.new("L", (w + 2 * p, h + 2 * p), 0)
    else:
        mean = spr.resize((1, 1), Image.BOX).getpixel((0, 0))[:3]
        c = Image.new("RGBA", (w + 2 * p, h + 2 * p), tuple(mean) + (0,))
    c.paste(spr, (p, p))
    return c


def _bbox(pts, size=(W, H)):
    x0 = max(0, int(math.floor(pts[:, 0].min())) - 1)
    y0 = max(0, int(math.floor(pts[:, 1].min())) - 1)
    x1 = min(size[0], int(math.ceil(pts[:, 0].max())) + 2)
    y1 = min(size[1], int(math.ceil(pts[:, 1].max())) + 2)
    return x0, y0, x1, y1


def warp_rgba(spr: Image.Image, pts, resample=Image.BICUBIC):
    """Warp an RGBA/L sprite onto the quad ``pts`` (TL,TR,BR,BL, screen px).  Returns (img, x0, y0) or None."""
    x0, y0, x1, y1 = _bbox(pts)
    if x1 <= x0 or y1 <= y0:
        return None
    p = 3
    w, h = spr.size
    canvas = _pad_canvas(spr, p)
    dst = pts - np.array([x0, y0])
    src = [(p, p), (p + w, p), (p + w, p + h), (p, p + h)]
    try:
        co = persp_coeffs(dst, src)
    except np.linalg.LinAlgError:
        return None
    return canvas.transform((x1 - x0, y1 - y0), Image.PERSPECTIVE, co, resample), x0, y0


def draw_quad(img, spr, P3, cam, a=1.0, mip=True, bias=0.15):
    """Project the textured quad (world corners TL,TR,BR,BL) and composite it onto ``img``."""
    if a <= 0.004:
        return
    pts, Z = cam.project(P3)
    if Z.min() < 60:
        return
    if mip:
        sc = math.sqrt(max(poly_area(pts), 1e-6) / (spr.width * spr.height))
        lv = int(max(0, min(4, math.floor(math.log2(1 / max(sc, 1e-6)) + bias))))
        if lv:
            spr = spr.reduce(2 ** lv)
    res = warp_rgba(spr, pts)
    if res is None:
        return
    out, x0, y0 = res
    alpha = out.getchannel("A")
    if a < 1:
        alpha = alpha.point(lambda v: int(v * a))
    img.paste(out.convert("RGB"), (x0, y0), alpha)


def paint_mask(img, mask, pts, color, a=1.0):
    """Fill the projected quad of an L mask with a solid colour (rendered at half resolution, then upscaled)."""
    x0, y0, x1, y1 = _bbox(pts)
    if x1 <= x0 or y1 <= y0:
        return
    x0, y0 = x0 // 2 * 2, y0 // 2 * 2
    bw, bh = (x1 - x0 + 1) // 2, (y1 - y0 + 1) // 2
    p = 2
    w, h = mask.size
    canvas = _pad_canvas(mask, p)
    try:
        co = persp_coeffs((pts - np.array([x0, y0])) / 2, [(p, p), (p + w, p), (p + w, p + h), (p, p + h)])
    except np.linalg.LinAlgError:
        return
    out = canvas.transform((bw, bh), Image.PERSPECTIVE, co, Image.BILINEAR).resize((bw * 2, bh * 2), Image.BILINEAR)
    if a < 1:
        out = out.point(lambda v: int(v * a))
    img.paste(Image.new("RGB", out.size, color), (x0, y0), out)


def warp_lowres(mask, pts, s=4):
    """L mask warped into a (W/s, H/s) canvas (used for soft shadows / glows)."""
    p = 2
    w, h = mask.size
    canvas = _pad_canvas(mask, p)
    src = [(p, p), (p + w, p), (p + w, p + h), (p, p + h)]
    try:
        co = persp_coeffs(pts / s, src)
    except np.linalg.LinAlgError:
        return None
    return canvas.transform((W // s, H // s), Image.PERSPECTIVE, co, Image.BILINEAR)


def soft_shadow(img, mask, P3, cam, offset, blur, alpha, color=(0, 0, 0), add=False):
    from PIL import ImageFilter
    if alpha <= 0.004:
        return
    pts, Z = cam.project(np.asarray(P3, float) + np.array(offset, float))
    if Z.min() < 60:
        return
    m = warp_lowres(mask, pts)
    if m is None:
        return
    m = m.filter(ImageFilter.GaussianBlur(blur)).resize((W, H), Image.BILINEAR)
    if add:
        r, g, b = color
        tint = Image.merge("RGB", [m.point(lambda v, c=c: int(v * alpha * c / 255)) for c in (r, g, b)])
        img.paste(ImageChops.add(img, tint))
    else:
        img.paste(Image.new("RGB", (W, H), color), (0, 0), m.point(lambda v: int(v * alpha)))


def covers_frame(pts, margin=100):
    """True when the projected quad covers the whole frame with ``margin`` px to spare."""
    area = float(np.dot(pts[:, 0], np.roll(pts[:, 1], -1)) - np.dot(pts[:, 1], np.roll(pts[:, 0], -1)))
    sgn = 1 if area > 0 else -1
    for cx_, cy_ in ((0, 0), (W, 0), (W, H), (0, H)):
        for i in range(4):
            a, b = pts[i], pts[(i + 1) % 4]
            e = b - a
            n = math.hypot(*e)
            if n < 1e-6 or sgn * (e[0] * (cy_ - a[1]) - e[1] * (cx_ - a[0])) / n < margin:
                return False
    return True


# ---------------------------------------------------------------- slab (the glass window)
@lru_cache(maxsize=1)
def slab_mask():
    return rr_mask(960, 600, 22)


@lru_cache(maxsize=8)
def sheen_img(w: int, h: int):
    xs = np.linspace(0, 1, 48)[None, :]
    ys = np.linspace(0, 1, 30)[:, None]
    v = np.clip(1 - (xs * 0.75 + ys * 0.95) / 1.05, 0, 1) ** 2 * 26
    g = Image.fromarray(v.astype(np.uint8), "L").resize((w, h), Image.BICUBIC)
    return Image.merge("RGB", (g, g, g))


def sweep_img(w: int, h: int, s: float, strength=70.0):
    xs = np.linspace(0, 1, 96)[None, :]
    ys = np.linspace(0, 1, 60)[:, None]
    u = (xs + 0.55 * ys) / 1.55
    v = np.exp(-((u - s) / 0.07) ** 2) * strength + np.exp(-((u - s + 0.11) / 0.025) ** 2) * strength * 0.5
    g = Image.fromarray(np.clip(v, 0, 255).astype(np.uint8), "L").resize((w, h), Image.BICUBIC)
    return Image.merge("RGB", (g, g, g.point(lambda q: min(255, int(q * 1.05)))))


@lru_cache(maxsize=16)
def rim_sprite(w: int, h: int, f: int) -> Image.Image:
    rw = max(2, int(round(5 / f)))
    outer = rr_mask(w, h, max(4, 72 // f))
    inner = Image.new("L", (w, h), 0)
    inner.paste(rr_mask(w - 2 * rw, h - 2 * rw, max(2, 72 // f - rw)), (rw, rw))
    ring = ImageChops.subtract(outer, inner).point(lambda v: int(v * 0.8))
    col = hgrad(w, h, (205, 228, 255), (150, 110, 255))
    spr = col.convert("RGBA")
    spr.putalpha(ring)
    return spr


@lru_cache(maxsize=16)
def rgba_window(w: int, h: int, f: int):
    return rr_mask(w, h, max(4, 72 // f))


EDGE_FRONT, EDGE_BACK = (150, 190, 255), (20, 26, 64)


def draw_slab(img, tex, cam, a=1.0, sweep=None):
    w, h = tex.size
    P = np.array([[0, 0, 0], [w, 0, 0], [w, h, 0], [0, h, 0]], float)
    pts, Z = cam.project(P)
    if Z.min() < 80 or a <= 0.004:
        return
    if not covers_frame(pts):
        m = slab_mask()
        soft_shadow(img, m, P, cam, (0, 330, 520), 9, 0.62 * a)
        soft_shadow(img, m, P, cam, (0, 170, 120), 24, 0.55 * a, color=(40, 80, 255), add=True)
        n = 6
        if np.abs(cam.project(P + np.array([0, 0, SLAB_T]))[0] - pts).max() > 0.7:
            for k in range(n, 0, -1):
                pk, zk = cam.project(P + np.array([0, 0, SLAB_T * k / n]))
                if zk.min() > 60:
                    paint_mask(img, m, pk, mix(EDGE_FRONT, EDGE_BACK, k / n), a)
    sc = math.sqrt(poly_area(pts) / (w * h))
    lv = int(max(0, min(3, math.floor(math.log2(1 / max(sc, 1e-6)) + 0.15))))
    f = 2 ** lv
    mip = tex.reduce(f) if f > 1 else tex
    mw, mh = mip.size
    rgb = ImageChops.add(mip, sheen_img(mw, mh))
    if sweep is not None:
        rgb = ImageChops.add(rgb, sweep_img(mw, mh, sweep))
    rgba = rgb.convert("RGBA")
    rgba.putalpha(rgba_window(mw, mh, f))
    rgba = Image.alpha_composite(rgba, rim_sprite(mw, mh, f))
    res = warp_rgba(rgba, pts)
    if res is None:
        return
    out, x0, y0 = res
    alpha = out.getchannel("A")
    if a < 1:
        alpha = alpha.point(lambda v: int(v * a))
    img.paste(out.convert("RGB"), (x0, y0), alpha)


def draw_lifted(img, cam, L):
    """A cut-out of the UI floating above the slab (z = lift towards the viewer, capture px)."""
    spr, x, y, z = L["spr"], L["x"], L["y"], L["z"]
    a = L.get("a", 1.0)
    w, h = spr.size
    sc = L.get("scale", 1.0)
    hw, hh = w * sc / 2, h * sc / 2
    Rl = rot(*L.get("rot", (0, 0, 0)))
    local = np.array([[-hw, -hh, 0], [hw, -hh, 0], [hw, hh, 0], [-hw, hh, 0]])
    P = local @ Rl.T + np.array([x + w / 2, y + h / 2, -z])
    if z > 2 and L.get("shadow", True):
        flat = P.copy()
        flat[:, 0] += z * 0.30
        flat[:, 1] += z * 0.50
        flat[:, 2] = 0
        soft_shadow(img, spr.getchannel("A"), flat, cam, (0, 0, 0), 2 + z / 28, 0.62 * a * min(1, z / 40))
    draw_quad(img, spr, P, cam, a)


# ---------------------------------------------------------------- 3D text / cards in screen-like space
_SIL: dict = {}


def silhouette(spr: Image.Image, color) -> Image.Image:
    key = (id(spr), color)
    if key not in _SIL:
        s = Image.new("RGBA", spr.size, tuple(color) + (0,))
        s.putalpha(spr.getchannel("A"))
        _SIL[key] = (s, spr)
    return _SIL[key][0]


def draw_card(img, cam, spr, cx, cy, z=0.0, rot_=(0, 0, 0), scale=1.0, a=1.0, fit=True, extrude=0.0,
              ext=((70, 90, 175), (22, 28, 70)), n_ext=6):
    """Flat sprite as a card in 3D.  (cx, cy) = design position in screen px; z = lift toward the viewer.
    fit=True keeps its on-screen size independent of z (pure parallax); fit=False gives true perspective growth."""
    if a <= 0.01:
        return
    Z = cam.D - z
    k = (Z if fit else cam.D) / cam.f
    wx = cam.T[0] + (cx - cam.cx) * k
    wy = cam.T[1] + (cy - cam.cy) * k
    hw, hh = spr.width * k * scale / 2, spr.height * k * scale / 2
    Rl = rot(*rot_)
    local = np.array([[-hw, -hh, 0], [hw, -hh, 0], [hw, hh, 0], [-hw, hh, 0]])
    P = local @ Rl.T + np.array([wx, wy, -z])
    if extrude > 0:
        normal = Rl @ np.array([0, 0, 1.0])
        for i in range(n_ext, 0, -1):
            col = mix(ext[0], ext[1], i / n_ext)
            draw_quad(img, silhouette(spr, col), P + normal * (extrude * k * i / n_ext), cam, a, mip=False)
    draw_quad(img, spr, P, cam, a, mip=False)


def orbit_cam(t: float, yaw_extra=0.0) -> Cam:
    """Camera for the 'flat' scenes: design coordinates are identity-mapped at z=0, with a slow orbit."""
    return Cam(W / 2, 960, 0, 1.0, 6.5 * math.sin(t * 0.9 + 0.5) + yaw_extra, -2.5 * math.sin(t * 0.7),
               0.0, cy=960)


# ================================================================== background (depth: floor grid, bokeh)
def _make_particles():
    rng = np.random.default_rng(11)
    n = 120
    pos = np.stack([rng.uniform(-2800, 2800, n), rng.uniform(-2600, 2600, n), rng.uniform(-700, 4600, n)], 1)
    rad = rng.uniform(2.5, 8.0, n)
    big = rng.random(n) < 0.14
    rad[big] = rng.uniform(26, 60, big.sum())
    col = [BLUE, VIOLET, (120, 200, 255), (190, 170, 255)]
    cidx = rng.integers(0, 4, n)
    alpha = np.where(big, rng.uniform(0.10, 0.20, n), rng.uniform(0.35, 0.8, n))
    return pos, rad, [col[i] for i in cidx], alpha, rng.uniform(0, 6.28, n), rng.uniform(8, 40, n)


PART = _make_particles()
_ys = np.linspace(0, 1, H)[:, None, None]
BASE = Image.fromarray((np.array(BG_TOP) * (1 - _ys) + np.array(BG_BOT) * _ys * np.ones((1, W, 1))).astype(np.uint8),
                       "RGB")


@lru_cache(maxsize=1)
def horizon_sprite():
    w, h = 1700, 340
    yy, xx = np.mgrid[0:h, 0:w]
    a = np.exp(-((yy - h / 2) / 55.0) ** 2) * np.exp(-((xx - w / 2) / 620.0) ** 2) * 0.85
    arr = np.zeros((h, w, 4), np.uint8)
    arr[..., :3] = (96, 84, 255)
    arr[..., 3] = (a * 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


@lru_cache(maxsize=1)
def vignette():
    yy, xx = np.mgrid[0:H, 0:W]
    d = np.sqrt(((xx - W / 2) / (W * 0.75)) ** 2 + ((yy - H / 2) / (H * 0.68)) ** 2)
    v = np.clip(1 - 0.30 * np.clip(d - 0.55, 0, 1) ** 1.4 * 2.0, 0.55, 1)
    g = Image.fromarray((v * 255).astype(np.uint8), "L")
    return Image.merge("RGB", (g, g, g))


def background(t, yaw=0.0, pitch=0.0, roll=0.0, tx=0.0, ty=0.0, rings=(), energy=1.0):
    cam = Cam(tx, ty, 0, 1.0, yaw, pitch, roll, cy=H / 2)
    img = BASE.copy()
    glow(img, W * 0.85 + 70 * math.sin(t * 0.7) - yaw * 9, 380 + 50 * math.cos(t * 0.5), 800, BLUE, 0.26 * energy)
    glow(img, W * 0.10 + 60 * math.cos(t * 0.6) - yaw * 9, 1500 + 60 * math.sin(t * 0.45), 860, VIOLET, 0.24 * energy)
    hy = cam.cy + cam.f * cam.R[1, 2] / cam.R[2, 2] if cam.R[2, 2] > 0.2 else H * 0.5
    hx = cam.cx + cam.f * cam.R[0, 2] / cam.R[2, 2] if cam.R[2, 2] > 0.2 else W * 0.5
    blit(img, horizon_sprite(), hx - 850, hy - 170, 0.55 * energy)

    S = 2
    grid = Image.new("L", (W * S, H * S), 0)
    gd = ImageDraw.Draw(grid)
    fy, sp = 760.0, 380.0
    scroll = (t * 140) % sp
    zs = np.geomspace(150, 14000, 22) - 1250
    for i in range(-18, 19):
        P = np.stack([np.full_like(zs, i * sp), np.full_like(zs, fy), zs], 1)
        q, Zc = cam.project(P)
        for j in range(len(zs) - 1):
            if Zc[j] > 140 and Zc[j + 1] > 140:
                al = (1 - min(1, Zc[j] / 9500)) ** 1.6 * (0.55 if i % 4 else 1.0)
                gd.line([tuple(q[j] * S), tuple(q[j + 1] * S)], fill=int(150 * al * energy), width=2)
    xs = np.linspace(-7000, 7000, 22)
    for j in range(0, 26):
        zz = j * sp - scroll - 700
        P = np.stack([xs, np.full_like(xs, fy), np.full_like(xs, zz)], 1)
        q, Zc = cam.project(P)
        for k in range(len(xs) - 1):
            if Zc[k] > 140 and Zc[k + 1] > 140:
                al = (1 - min(1, Zc[k] / 9500)) ** 1.6
                gd.line([tuple(q[k] * S), tuple(q[k + 1] * S)], fill=int(150 * al * energy), width=2)
    for (r, ra) in rings:
        th = np.linspace(0, 2 * math.pi, 90)
        P = np.stack([r * np.cos(th), np.full_like(th, fy), 260 + r * np.sin(th)], 1)
        q, Zc = cam.project(P)
        for k in range(len(th) - 1):
            if Zc[k] > 140 and Zc[k + 1] > 140:
                gd.line([tuple(q[k] * S), tuple(q[k + 1] * S)], fill=int(255 * ra), width=5)
    mask = grid.reduce(S)
    img.paste(hgrad(W, H, (110, 150, 255), (150, 110, 255)), (0, 0), mask)

    pos = PART[0].copy()
    pos[:, 1] = ((pos[:, 1] - t * PART[5] + 2600) % 5200) - 2600
    pos[:, 0] += 70 * np.sin(t * 0.4 + PART[4])
    q, Zc = cam.project(pos)
    for i in range(len(pos)):
        if Zc[i] < 200:
            continue
        r = PART[1][i] * cam.f / Zc[i]
        x, y = q[i]
        if r < 1.0 or x < -80 or x > W + 80 or y < -80 or y > H + 80:
            continue
        fade = float(np.clip(1.15 - Zc[i] / 6200, 0.18, 1))
        tw = 0.75 + 0.25 * math.sin(t * 2 + PART[4][i])
        glow(img, x, y, int(max(3, r * 2.3)), PART[2][i], float(PART[3][i]) * fade * tw * energy)
    return img


# ================================================================== scene 1: hook (3D extruded words)
def scene_hook(img, t):
    cam = orbit_cam(t)
    words = [("YOUR CLIPS.", 0.00, 760, None), ("TOO LONG.", 0.80, 1000, None),
             ("CUT IT.", 1.60, 1250, (BLUE, VIOLET))]
    out = prog(t, 2.4, 2.75)
    for text, t0, y, grad in words:
        lt = t - t0
        if lt < 0 or out >= 1:
            continue
        p = out_back(prog(lt, 0, 0.30), 1.5)
        z = lerp(480, 0, p) + 900 * in_out(out) * 1.0
        pitch = lerp(-58, 0, p) + 10 * out
        shake = math.sin(lt * 90) * 14 * (1 - prog(lt, 0, 0.3))
        spr = tsprite(text, 250, F_BLACK, WHITE, grad, 940)
        draw_card(img, cam, spr, W / 2 + shake, y, z=z, rot_=(0, pitch, 0), a=prog(lt, 0, 0.08) * (1 - out),
                  fit=False, extrude=46, ext=((92, 72, 215), (24, 20, 72)) if grad else ((120, 135, 200), (26, 32, 78)))
    bw = 640 * out_cubic(prog(t, 1.9, 2.4)) * (1 - out)
    if bw > 8:
        bar = fill_rr_sprite(int(bw), 16)
        draw_card(img, cam, bar, W / 2, 1430, z=-40, a=1.0, extrude=14, ext=((60, 50, 160), (20, 18, 60)), n_ext=3)


def fill_rr_sprite(w, h, c1=BLUE, c2=VIOLET):
    spr = hgrad(w, h, c1, c2).convert("RGBA")
    spr.putalpha(rr_mask(w, h, h // 2))
    return spr


# ================================================================== scene 2: logo reveal
def scene_logo(img, t):
    cam = orbit_cam(t)
    a_out = 1 - in_out(prog(t, 4.25, 4.7))
    if a_out <= 0.01:
        return
    fly = 700 * (1 - a_out) ** 1.5
    glow(img, W / 2, 800, 560, BLUE, 0.5 * prog(t, 2.8, 3.2) * a_out)
    glow(img, W / 2, 800, 420, VIOLET, 0.35 * prog(t, 2.9, 3.3) * a_out)
    p = out_back(prog(t, 2.75, 3.45), 1.3)
    yaw = lerp(110, 0, p) + 9 * math.sin(max(0, t - 3.4) * 1.6) * prog(t, 3.4, 3.7)
    mark = logo_sprite("mark", 520)
    draw_card(img, cam, mark, W / 2, 800 - 40 * (1 - a_out), z=lerp(300, 60, p) + fly, rot_=(yaw, -4 * math.sin(t * 1.3), 0),
              a=prog(t, 2.75, 2.95) * a_out, extrude=70, ext=((88, 70, 214), (22, 18, 78)), n_ext=7)
    wp = out_cubic(prog(t, 3.4, 3.9))
    word = logo_sprite("word", 720)
    draw_card(img, cam, word, W / 2, 1150, z=30 + fly * 0.6, rot_=(0, lerp(-70, 0, wp), 0), a=wp * a_out,
              extrude=18, ext=((70, 80, 170), (20, 22, 60)), n_ext=3)
    words = [("DOWNLOAD.", 3.9), ("EDIT.", 4.1), ("EXPORT.", 4.3)]
    size, gap = 58, 36
    widths = [text_w(w_, size, F_BOLD, 0.08) for w_, _ in words]
    x = W / 2 - (sum(widths) + gap * 2) / 2
    for (w_, t0), wd in zip(words, widths):
        pp = out_cubic(prog(t, t0 - 0.2, t0 + 0.05))
        spr = text_sprite(w_, size, F_BOLD, tuple(WHITE if w_ == "EDIT." else MUTED), int(0.08 * size), None)
        draw_card(img, cam, spr, x + wd / 2, 1350 + 26 * (1 - pp), z=lerp(220, 90, pp) + fly, rot_=(0, lerp(30, 0, pp), 0),
                  a=pp * a_out, extrude=8, ext=((60, 70, 130), (20, 22, 52)), n_ext=2)
        x += wd + gap


# ================================================================== UI textures (real captures)
@lru_cache(maxsize=None)
def _raw(name: str) -> Image.Image:
    return Image.open(UI / f"{name}.png").convert("RGB")


@lru_cache(maxsize=1)
def layout():
    return json.loads((UI / "layout.json").read_text())["states"]


@lru_cache(maxsize=None)
def state(key: str) -> Image.Image:
    """Cleaned-up capture (read-only!).  'c' variants have the capture's own playhead removed, 'x' variants have
    the clips / audio lane cleared so that we can animate them as separate pieces."""
    if key == "01c":
        im = _raw("01_loaded").copy()
        box = (49 - 26, 1404, 49 + 26, 1900)
        im.paste(_raw("02_playhead1").crop(box), box[:2])
        return im
    if key == "d5c":      # download finished: panel cleared, clip on the timeline (own playhead removed)
        im = _raw("d5_done").copy()
        box = (49 - 26, 1404, 49 + 26, 1900)
        im.paste(_raw("02_playhead1").crop(box), box[:2])
        return im
    if key == "d5e":      # ... and the timeline clip lifted out (drop animation)
        im = state("d5c").copy()
        im.paste(Image.new("RGB", (3103 - 43, 302), LANE_BG), (43, 1488))
        return im
    if key == "d3g":      # info card + button row lifted out of the panel
        im = _raw("d3_info").copy()
        im.paste(Image.new("RGB", (648, 336), PANEL_BG), (44, 300))
        return im
    if key == "d1b":      # URL field emptied (placeholder + caret removed) so we can type into it ourselves
        im = _raw("d1_url_focus").copy()
        im.paste(Image.new("RGB", (566 - 52, 276 - 206), FIELD_BG), (52, 206))
        return im
    if key == "03c":
        im = _raw("03_split1").copy()
        im.paste(_raw("01_loaded").crop((1148 - 26, 1404, 1148 + 26, 1488)), (1148 - 26, 1404))
        im.paste(_raw("01_loaded").crop((1148 + 3, 1470, 1148 + 5, 1488)), (1146, 1470))
        im.paste(Image.new("RGB", (2, 302), LANE_BG), (1146, 1488))
        im.paste(_raw("03_split1").crop((1151, 1790, 1153, 1900)), (1146, 1790))
        return im
    if key == "06x":
        im = _raw("06_mid_selected").copy()
        im.paste(_raw("01_loaded").crop((2063 - 26, 1404, 2063 + 26, 1488)), (2063 - 26, 1404))
        im.paste(Image.new("RGB", (3103 - 43, 302), LANE_BG), (43, 1488))
        im.paste(_raw("06_mid_selected").crop((2069, 1790, 2073, 1900)), (2061, 1790))
        return im
    if key == "08x":
        im = _raw("08_detached").copy()
        im.paste(Image.new("RGB", (3103 - 43, 100), LANE_BG), (43, 1783))
        return im
    names = {"05": "05_split2", "06": "06_mid_selected", "07": "07_ripple_deleted", "08": "08_detached"}
    return _raw(names[key])


def crop_img(im: Image.Image, rect, pad=0, radius=0):
    x, y, w, h = rect
    c = im.crop((x - pad, y - pad, x + w + pad, y + h + pad)).convert("RGBA")
    if radius:
        c.putalpha(rr_mask(c.width, c.height, radius))
    return c


def crop(key_name: str, rect, pad=0, radius=0):
    """RGBA cut-out of a capture rect [x,y,w,h] (optionally with rounded alpha)."""
    x, y, w, h = rect
    im = _raw(key_name).crop((x - pad, y - pad, x + w + pad, y + h + pad)).convert("RGBA")
    if radius:
        im.putalpha(rr_mask(im.width, im.height, radius))
    return im


@lru_cache(maxsize=None)
def clip_crop(name: str, idx: int, kind: str = "video", pad=0, radius=0):
    c = layout()[name][f"{kind}_clips"][idx]["rect"]
    return crop(name, c, pad, radius)


@lru_cache(maxsize=1)
def cursor_sprite():
    s = 4
    pts = [(0, 0), (0, 17), (4.6, 13.5), (7.4, 20), (10, 18.8), (7.2, 12.6), (12.4, 12.4)]
    big = Image.new("RGBA", (16 * s * 5, 24 * s * 5), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    q = [(8 + x * s * 5, 8 + y * s * 5) for x, y in pts]
    d.polygon(q, fill=(255, 255, 255, 255), outline=(14, 18, 38, 255), width=int(1.6 * s * 5))
    return big.resize((big.width // 5, big.height // 5), Image.LANCZOS)


@lru_cache(maxsize=1)
def playhead_sprite():
    s = 4
    w, hh = 40, 1900 - 1426
    big = Image.new("RGBA", (w * s, hh * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    cx = 20 * s
    d.rectangle((cx - 2 * s, 14 * s, cx + 0 * s, hh * s), fill=RED + (255,))
    d.polygon([(cx - 14 * s, 6 * s), (cx + 12 * s, 6 * s), (cx + 12 * s, 28 * s), (cx - 1 * s, 42 * s),
               (cx - 14 * s, 28 * s)], fill=(255, 77, 94, 255))
    return big.resize((w, hh), Image.LANCZOS)


def paint_playhead(tex, x, head=True, glow_a=0.0):
    spr = playhead_sprite()
    if not head:
        spr = spr.crop((0, 60, spr.width, spr.height))
        blit(tex, spr, x - 20 + 1, 1426 + 60, 1.0)
        return
    if glow_a > 0:
        glow(tex, x, 1560, 120, RED, glow_a)
    blit(tex, spr, x - 20 + 1, 1426, 1.0)


def paint_cursor(tex, x, y, a=1.0, scale=1.0):
    spr = cursor_sprite()
    if scale != 1.0:
        spr = spr.resize((int(spr.width * scale), int(spr.height * scale)), Image.LANCZOS)
    shadow = Image.new("RGBA", spr.size, (0, 0, 0, 0))
    shadow.putalpha(spr.getchannel("A").point(lambda v: int(v * 0.35)))
    blit(tex, shadow, x + 6, y + 8, a)
    blit(tex, spr, x, y, a)


def paint_ripple(tex, x, y, t, t0, color=WHITE, r1=170, dur=0.5):
    p = (t - t0) / dur
    if not 0 <= p < 1:
        return
    r = int(lerp(20, r1, out_cubic(p)))
    d = int(2 * r + 12)
    spr = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    big = Image.new("L", (d * 3, d * 3), 0)
    ImageDraw.Draw(big).ellipse((6 * 3, 6 * 3, (d - 6) * 3, (d - 6) * 3), outline=255, width=int(max(2, 10 * (1 - p)) * 3))
    spr.putalpha(big.resize((d, d), Image.LANCZOS).point(lambda v: int(v * (1 - p))))
    spr.paste(Image.new("RGB", (d, d), color), (0, 0), spr.getchannel("A"))
    blit(tex, spr, x - d / 2, y - d / 2, 1.0)


def paint_flash(tex, x, t, t0, y0=1470, y1=1800):
    p = (t - t0) / 0.45
    if not 0 <= p < 1:
        return
    k = 1 - p
    glow(tex, x, (y0 + y1) / 2, int(110 + 220 * p), (170, 205, 255), 0.85 * k)
    w = max(2, int(14 * k))
    tex.paste(mix(LANE_BG, (255, 255, 255), k), (int(x - w / 2), y0, int(x + w / 2), y1))
    glow(tex, x, 1440, 130, (255, 255, 255), 0.7 * k)


def move(t, keys):
    """Cursor path: keys = [(t, x, y), ...] eased."""
    if t <= keys[0][0]:
        return keys[0][1], keys[0][2]
    for (ta, xa, ya), (tb, xb, yb) in zip(keys, keys[1:]):
        if t < tb:
            p = in_out5(prog(t, ta, tb))
            return lerp(xa, xb, p), lerp(ya, yb, p)
    return keys[-1][1], keys[-1][2]


PREVIEW = (1002, 124, 2890, 1186)


def preview_blend(tex, a_name, b_name, p):
    if p <= 0:
        return
    a = _raw(a_name).crop(PREVIEW)
    b = _raw(b_name).crop(PREVIEW)
    tex.paste(Image.blend(a, b, cl(p)), PREVIEW[:2])


def playhead_x(t):
    if t < GLIDE1[0]:
        return 49.0
    if t < GLIDE2[0]:
        return lerp(49, 1148, in_out5(prog(t, *GLIDE1)))
    if t < FIT_T[0]:
        return lerp(1148, 2063, in_out5(prog(t, *GLIDE2)))
    k = 1 + 0.4286 * in_out(prog(t, *FIT_T))
    return 48 + (2063 - 48) * k


# ---------------------------------------------------------------- the timeline director
def url_text():
    return layout()["d2_url_typed"]["download_ui"]["url_text"]["text"]


@lru_cache(maxsize=1)
def url_font():
    return ImageFont.truetype("C:/Windows/Fonts/SegUIVar.ttf", 38)


def typing_tex(t):
    """Empty focused field + our own typed text (clipped to the field, scrolling like the real one) + caret."""
    tex = state("d1b").copy()
    txt = url_text()
    n = int(round(len(txt) * prog(t, *TYPE_T) ** 0.95))
    if n <= 0:
        paint_caret(tex, 64, t)
        return tex
    f = url_font()
    cur = txt[:n]
    w = f.getlength(cur)
    off = min(0.0, 548 - (66 + w))
    tmp = Image.new("RGB", (620, 110), FIELD_BG)
    ImageDraw.Draw(tmp).text((66 + off, 257 - 190), cur, font=f, fill=(231, 233, 236), anchor="ls")
    tex.paste(tmp.crop((60, 16, 566, 86)), (60, 206))
    tex.paste(_raw("d2_url_typed").crop((572, 208, 644, 272)), (572, 208))     # the field's clear button
    paint_caret(tex, 66 + off + w + 3, t)
    return tex


def paint_caret(tex, x, t):
    if int(t * 3.4) % 2 == 0:
        ImageDraw.Draw(tex).rectangle((int(x), 216, int(x) + 3, 264), fill=(240, 244, 255))


def progress_tex(t):
    """Download panel while the bar fills: the captured label text switches 10 -> 45 -> 80 % (like the real app),
    the bar itself is repainted so it fills smoothly up to 100 %."""
    f = in_out5(prog(t, *PROG_T)) * 0.96 + 0.04 * prog(t, *PROG_T)
    tex = _raw("d4_dl_10" if f < 0.3 else "d4_dl_45" if f < 0.65 else "d4_dl_80").copy()
    x0, y0, tw, th = 44, 552, 604, 16
    tex.paste(Image.new("RGB", (tw, th), TRACK_BG), (x0, y0), rr_mask(tw, th, 8))
    fw = max(th, int(tw * f))
    done = t >= PROG_T[1]
    if f > 0.985:      # last stretch: the captured label would still say 80 %, so write our own
        tex.paste(Image.new("RGB", (604, 56), PANEL_BG), (44, 578))
        ImageDraw.Draw(tex).text((44, 620), "100%  ·  Download complete", font=url_font(), fill=(120, 232, 184), anchor="ls")
    col = GREEN if done else FILL_BLUE
    fill_rr(tex, (x0, y0, x0 + fw, y0 + th), 8, col)
    glow(tex, x0 + fw, y0 + 8, 54, GREEN if done else (120, 180, 255), 0.55)
    return tex


def seg_tex(t, upto=None):
    """Window texture for time t (segment chosen by min(t, upto) so we can cross-fade out of the previous one)."""
    sel = t if upto is None else min(t, upto - 1e-4)
    if sel < T_CLICK:
        tex = _raw("00_empty").copy()
    elif sel < TYPE_T[1] + 0.05:
        tex = typing_tex(t)
    elif sel < T_ENTER:
        tex = _raw("d2_url_typed").copy()
    elif sel < T_CARD:
        tex = _raw("d2b_fetching").copy()
    elif sel < T_DL:
        tex = state("d3g").copy() if sel < T_CARD_LAND else _raw("d3_info").copy()
        if T_DL - 0.3 <= t < T_DL + 0.25:      # Download button press feedback
            k = math.sin(prog(t, T_DL - 0.3, T_DL) * math.pi / 2) * (1 - prog(t, T_DL, T_DL + 0.25))
            glow(tex, 502, 592, 190, (150, 190, 255), 0.55 * k)
    elif sel < T_DONE:
        tex = progress_tex(t)
    elif sel < SPLIT1:
        tex = state("d5e" if sel < T_DROP[1] else "d5c").copy()
        preview_blend(tex, "01_loaded", "02_playhead1", prog(t, *GLIDE1))
        if T_DONE <= t < T_DONE + 0.5:
            k = 1 - prog(t, T_DONE, T_DONE + 0.5)
            glow(tex, 340, 700, 520, GREEN, 0.28 * k)
    elif sel < SPLIT2:
        tex = state("03c").copy()
        preview_blend(tex, "03_split1", "04_playhead2", prog(t, *GLIDE2))
    elif sel < SELECT_T:
        tex = state("05").copy()
    elif sel < LIFT_T:
        tex = state("06").copy()
    elif sel < FIT_T[1]:
        tex = compose_delete(t)
    elif sel < DETACH_T:
        tex = state("07").copy()
    else:
        tex = compose_detach(t)
    return tex


def compose_delete(t):
    tex = state("06x").copy()
    k = 1 + 0.4286 * in_out(prog(t, *FIT_T))
    xr = lerp(2062, 1148, in_out(prog(t, *SLIDE_T)))
    left = clip_crop("06_mid_selected", 0)
    right = clip_crop("06_mid_selected", 2)
    for spr, x0 in ((left, 48), (right, xr)):
        w = spr.width
        xx = 48 + (x0 - 48) * k
        s = spr if k == 1 else spr.resize((int(round(w * k)), spr.height), Image.BICUBIC)
        tex.paste(s.convert("RGB"), (int(round(xx)), 1488))
    return tex


def compose_detach(t):
    tex = state("08x").copy()
    n7 = state("07")
    if t < DETACH_T + 0.22:
        tex = Image.blend(n7, tex, in_out(prog(t, DETACH_T, DETACH_T + 0.22)))
    if t >= PEEL_T[1]:
        a1, a2 = (clip_crop("08_detached", i, "audio") for i in (0, 1))
        tex.paste(a1.convert("RGB"), (48, 1786))
        if t < AUDIO_SLIDE_T[0] or t >= AUDIO_SLIDE_T[1] + 0.12:
            dx = 268 * in_out(prog(t, *AUDIO_SLIDE_T)) if t >= AUDIO_SLIDE_T[1] else 0
            tex.paste(a2.convert("RGB"), (1618 + int(dx), 1786))
    return tex


def cursor_state(t):
    """(x, y, opacity, scale) of the mouse cursor in capture px."""
    if t < 7.7:               # download beat
        x, y = move(t, [(4.50, 880, 760), (T_CLICK - 0.05, 346, 248), (T_CLICK + 0.05, 346, 248), (T_CLICK + 0.35, 600, 400), (6.35, 600, 400), (6.85, 502, 592),
                        (7.25, 520, 600), (7.9, 900, 900)])
        vis = prog(t, 4.5, 4.75) * (1 - prog(t, 7.3, 7.6))
        return x, y, vis, 1.0
    keys1 = [(5.45 + D, 2400, 700), (5.97 + D, 49 + 8, 1436), (GLIDE1[1], 1148 + 8, 1436), (6.62 + D, 1190, 1470),
             (6.78 + D, 1148 + 8, 1436), (GLIDE2[1], 2063 + 8, 1436), (SELECT_T, 1700, 1690)]
    keys2 = [(SELECT_T, 1700, 1690), (8.1 + D, 1760, 1800), (9.9 + D, 1900, 1840), (10.5 + D, 1950, 1840),
             (10.5 + 0.55 + D, 1950 + 268, 1840), (11.2 + D, 2700, 1200), (11.65 + D, 3052, 56)]
    x, y = move(t, keys1 if t < SELECT_T else keys2)
    vis = prog(t, 5.45 + D, 5.7 + D) * (1 - prog(t, 8.2 + D, 8.5 + D)) +         prog(t, 9.8 + D, 10.0 + D) * (1 - prog(t, 11.4 + D, 11.8 + D)) * (t > 9.7 + D)
    if 8.0 + D < t < 9.7 + D:
        vis = 0
    if t >= 11.8 + D:
        vis = 0
    return x, y, vis, 1.25


def demo_tex(t):
    tex = seg_tex(t)
    for ts, d in ((T_CLICK, 0.08), (TYPE_T[1] + 0.05, 0.08), (T_ENTER, 0.10), (T_CARD, 0.12), (T_DL, 0.10), (T_DONE, 0.16),
                  (SPLIT1, 0.12), (SPLIT2, 0.12), (SELECT_T, 0.16), (FIT_T[1], 0.24)):
        if ts <= t < ts + d:
            tex = Image.blend(seg_tex(t, upto=ts), tex, in_out((t - ts) / d))
    # --- overlays painted in capture space
    ph = playhead_x(t)
    paint_flash(tex, 1148, t, SPLIT1)
    paint_flash(tex, 2063, t, SPLIT2)
    if t >= T_DONE and t < DETACH_T + 0.22:
        moving = GLIDE1[0] < t < GLIDE1[1] or GLIDE2[0] < t < GLIDE2[1] or FIT_T[0] < t < FIT_T[1]
        keep_head = not (t >= FIT_T[1] + 0.24)
        if keep_head:
            paint_playhead(tex, ph, glow_a=0.35 if moving else 0.0)
    elif t >= DETACH_T + 0.22:
        paint_playhead(tex, 2924, head=False)
    # cursor
    cx_, cy_, vis, csc = cursor_state(t)
    if vis > 0.01:
        paint_cursor(tex, cx_, cy_, vis, csc)
    paint_ripple(tex, 346, 240, t, T_CLICK - 0.02, (170, 205, 255), 120)
    paint_ripple(tex, 502, 592, t, T_DL - 0.02, (190, 215, 255), 140)
    if T_DONE <= t < T_DONE + 0.6:
        paint_ripple(tex, 648, 560, t, T_DONE, (120, 240, 190), 150, 0.6)
    paint_ripple(tex, 1148 + 8, 1436, t, SPLIT1 - 0.12, (170, 205, 255))
    paint_ripple(tex, 2063 + 8, 1436, t, SPLIT2 - 0.12, (170, 205, 255))
    paint_ripple(tex, 1700, 1690, t, SELECT_T - 0.05, (255, 209, 102), 150)
    if EXPORT_CLICK_T - 0.4 <= t < EXPORT_CLICK_T + 0.6:
        pulse = prog(t, EXPORT_CLICK_T - 0.4, EXPORT_CLICK_T) * (1 - prog(t, EXPORT_CLICK_T, EXPORT_CLICK_T + 0.6))
        glow(tex, 3050, 51, 330, BLUE, 0.9 * pulse)
        paint_ripple(tex, 3050, 51, t, EXPORT_CLICK_T, (190, 215, 255), 190, 0.55)
    # dim while the export dialog is up
    dim = 0.58 * in_out(prog(t, DIALOG_T[0] - 0.05, DIALOG_T[1]))
    if dim > 0.01:
        tex = Image.blend(tex, Image.new("RGB", tex.size, (6, 8, 18)), dim)
    return tex


@lru_cache(maxsize=1)
def dialog_base():
    return _raw("10_export_dialog")


def dialog_sprite(t):
    base = dialog_base().copy()
    w, h = base.size
    d = ImageDraw.Draw(base)
    pp = prog(t, *PROGRESS_T)
    if t >= PROGRESS_T[0] - 0.12:
        # press feedback on the Export button, then a brand-style progress card over the button row
        box = (40, 1552, w - 40, 1668)
        a = out_cubic(prog(t, PROGRESS_T[0] - 0.12, PROGRESS_T[0] + 0.08))
        card = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), (15, 18, 30))
        cw, ch = card.size
        done = t >= PROGRESS_T[1] + 0.02
        bar = (28, 74, cw - 28, 92)
        fill_rr(card, bar, 9, (36, 42, 66))
        bw = int((bar[2] - bar[0]) * (1.0 if done else out_cubic(pp) * 0.7 + pp * 0.3))
        if bw > 10:
            fill_rr(card, (bar[0], bar[1], bar[0] + bw, bar[3]), 9,
                    hgrad(bw, 18, GREEN, (30, 170, 120)) if done else hgrad(bw, 18, BLUE, VIOLET))
        put_text(card, "Exported" if done else "Exporting...", 28 + text_w("Exported", 38, F_BLACK) / 2, 38, 38, F_BLACK,
                 GREEN if done else WHITE)
        put_text(card, "100%" if done else f"{int(pp * 100):d}%", cw - 28 - 40, 38, 34, F_BOLD, MUTED if not done else GREEN)
        stroke_rr(card, (0, 0, cw, ch), 20, hgrad(cw, ch, GREEN, (30, 170, 120)) if done else hgrad(cw, ch, BLUE, VIOLET), 3)
        base.paste(Image.blend(base.crop(box), card, a), box[:2])
    spr = base.convert("RGBA")
    spr.putalpha(rr_mask(w, h, 40))
    return Image.alpha_composite(spr, dialog_rim(w, h))


@lru_cache(maxsize=1)
def dialog_rim(w, h):
    outer = rr_mask(w, h, 40)
    inner = Image.new("L", (w, h), 0)
    inner.paste(rr_mask(w - 6, h - 6, 37), (3, 3))
    ring = ImageChops.subtract(outer, inner).point(lambda v: int(v * 0.85))
    spr = hgrad(w, h, (205, 228, 255), (150, 110, 255)).convert("RGBA")
    spr.putalpha(ring)
    return spr


def demo_lifts(t):
    L = []
    mid_rect = layout()["06_mid_selected"]["video_clips"][1]["rect"]
    # --- DOWNLOAD: the info card (+ quality / Download row) pops out of the panel with a z-lift
    if T_CARD <= t < T_CARD_LAND:
        p = out_back(prog(t, T_CARD, T_CARD_LAND), 1.5)
        spr = crop_img(_raw("d3_info"), (44, 300, 604, 336), radius=16)
        L.append(dict(spr=spr, x=44, y=300 + 30 * (1 - p), z=210 * (1 - p) + 4, a=prog(t, T_CARD, T_CARD + 0.1),
                      rot=(0, -8 * (1 - p), 0), scale=1 + 0.03 * (1 - p)))
    # --- DOWNLOAD done: the clip drops onto the timeline
    if T_DROP[0] <= t < T_DROP[1]:
        p = out_back(prog(t, *T_DROP), 1.1)
        spr = crop_img(state("d5c"), (48, 1488, 3050, 300), radius=14)
        L.append(dict(spr=spr, x=48, y=1488 - 230 * (1 - p), z=340 * (1 - p) + 2, a=prog(t, T_DROP[0], T_DROP[0] + 0.12),
                      rot=(0, 0, 0), scale=1.0))
    # --- DELETE: middle clip lifts, flies off
    if LIFT_T <= t < FLY_T[1] + 0.02:
        spr = clip_crop("06_mid_selected", 1, pad=3, radius=14)
        p1 = out_cubic(prog(t, LIFT_T, LIFT_T + 0.34))
        p2 = in_out(prog(t, *FLY_T))
        L.append(dict(spr=spr, x=mid_rect[0] - 3 + 640 * p2, y=mid_rect[1] - 3 - 760 * p2, z=170 * p1 + 1000 * p2,
                      rot=(26 * p2, -9 * p1 - 14 * p2, -22 * p2), a=1 - prog(t, FLY_T[0] + 0.2, FLY_T[1]),
                      scale=1 + 0.05 * p1))
    # --- DETACH: audio strips peel off the video clips and drop into the audio lane
    if PEEL_T[0] <= t < PEEL_T[1]:
        a_in = prog(t, PEEL_T[0], PEEL_T[0] + 0.14)
        up = out_cubic(prog(t, PEEL_T[0], PEEL_T[0] + 0.28))
        down = in_out5(prog(t, PEEL_T[0] + 0.25, PEEL_T[1]))
        for i in (0, 1):
            r = layout()["08_detached"]["audio_clips"][i]["rect"]
            L.append(dict(spr=clip_crop("08_detached", i, "audio", radius=12), x=r[0],
                          y=lerp(1690, r[1], down), z=125 * up * (1 - down) + 2 * (1 - down), a=a_in,
                          rot=(0, -10 * up * (1 - down), 0)))
    if AUDIO_SLIDE_T[0] <= t < AUDIO_SLIDE_T[1] + 0.12:
        r = layout()["08_detached"]["audio_clips"][1]["rect"]
        z = 55 * min(1, prog(t, AUDIO_SLIDE_T[0], AUDIO_SLIDE_T[0] + 0.12)) * (1 - prog(t, AUDIO_SLIDE_T[1], AUDIO_SLIDE_T[1] + 0.12))
        L.append(dict(spr=clip_crop("08_detached", 1, "audio", radius=12), x=r[0] + 268 * in_out(prog(t, *AUDIO_SLIDE_T)),
                      y=r[1], z=z))
    # --- EXPORT dialog floating above the window
    if t >= DIALOG_T[0]:
        p = out_back(prog(t, *DIALOG_T), 1.4)
        pop = 1 + 0.03 * math.sin(prog(t, PROGRESS_T[1], PROGRESS_T[1] + 0.2) * math.pi)
        spr = dialog_sprite(t)
        L.append(dict(spr=spr, x=1600 - spr.width / 2, y=1000 - spr.height / 2 - 10, z=lerp(950, 330, p) + 30 * math.sin(t * 1.7),
                      rot=(2.5 * math.sin(t * 1.1), 1.2 * math.sin(t * 0.9), 0), a=prog(t, DIALOG_T[0], DIALOG_T[0] + 0.14),
                      scale=pop))
    return L


# ================================================================== scene 3: the 3D interface demo
def demo_pose(t):
    keys = CAM_KEYS
    if t <= keys[0][0]:
        p = list(keys[0][1:])
    elif t >= keys[-1][0]:
        p = list(keys[-1][1:])
    else:
        for a, b in zip(keys, keys[1:]):
            if a[0] <= t < b[0]:
                x = in_out(prog(t, a[0], b[0]))
                p = [lerp(a[1], b[1], x), lerp(a[2], b[2], x), math.exp(lerp(math.log(a[3]), math.log(b[3]), x)),
                     lerp(a[4], b[4], x), lerp(a[5], b[5], x), lerp(a[6], b[6], x)]
                break
    # never perfectly still: a slow handheld-orbit drift
    p[3] += 2.2 * math.sin(t * 1.1)
    p[4] += 1.0 * math.sin(t * 1.5 + 1)
    p[5] += 0.6 * math.sin(t * 0.9)
    p[0] += 14 * math.sin(t * 0.8) / max(p[2], 0.2) * 0.3
    return p


def cam_from(p):
    return Cam(p[0], p[1], 0, p[2], p[3], p[4], p[5], cy=1090)


def sweep_at(t):
    for t0, t1 in ((4.05, 4.9), (T_DONE, T_DONE + 0.8), (7.7 + D, 8.4 + D), (10.1 + D, 10.8 + D), (12.1 + D, 12.9 + D)):
        if t0 <= t < t1:
            return lerp(-0.25, 1.3, in_out((t - t0) / (t1 - t0)))
    return None


def demo_world(t):
    p = demo_pose(t)
    cam = cam_from(p)
    img = background(t, p[3] * 0.5, p[4] * 0.5, p[5] * 0.4, (p[0] - 1600) * 0.10, (p[1] - 1000) * 0.10)
    tex = demo_tex(t)
    a = prog(t, T_FLY0, T_FLY0 + 0.45)
    draw_slab(img, tex, cam, a=a, sweep=sweep_at(t))
    for L in demo_lifts(t):
        draw_lifted(img, cam, L)
    return img


def caption_plate(w, h):
    base = Image.new("RGB", (w, h), (11, 15, 34))
    stroke_rr(base, (0, 0, w, h), 34, hgrad(w, h, BLUE, VIOLET), 3)
    spr = base.convert("RGBA")
    spr.putalpha(rr_mask(w, h, 34).point(lambda v: int(v * 0.90)))
    return spr


CAPTION_PLATE = caption_plate(940, 190)


def demo_overlay(img, t, p):
    cam = orbit_cam(t, yaw_extra=-p[3] * 0.32)
    for text, t0, t1, chip in CAPTIONS:
        if not (t0 <= t < t1):
            continue
        lt = t - t0
        vis = prog(lt, 0, 0.08) * (1 - prog(t, t1 - 0.14, t1))
        s = out_back(prog(lt, 0, 0.28), 1.5)
        zin = lerp(420, 0, s) + 260 * prog(t, t1 - 0.14, t1)
        pitch = lerp(46, 0, s) - 18 * prog(t, t1 - 0.14, t1)
        cy = 400
        draw_card(img, cam, CAPTION_PLATE, W / 2, cy, z=zin, rot_=(0, pitch, 0), a=vis, fit=False, extrude=36,
                  ext=((72, 70, 190), (18, 16, 60)), n_ext=5)
        spr = tsprite(text, 140, F_BLACK, WHITE, None, 820)
        draw_card(img, cam, spr, W / 2, cy, z=zin + 26, rot_=(0, pitch, 0), a=vis, fit=False, extrude=14,
                  ext=((110, 125, 195), (30, 36, 84)), n_ext=3)
        if chip:
            canvas = hgrad(124, 62, BLUE, VIOLET).copy()
            put_text(canvas, chip, 62, 31, 38, F_BOLD, WHITE)
            c = canvas.convert("RGBA")
            c.putalpha(rr_mask(124, 62, 31))
            draw_card(img, cam, c, W / 2 - 330, cy - 120, z=zin + 60, rot_=(0, pitch, 0), a=vis, fit=False, extrude=10,
                      ext=((70, 70, 200), (20, 18, 70)), n_ext=3)


# ================================================================== scene 4: features around a small slab
CHIPS = [("SPLIT", 300, 1372), ("TRIM", 780, 1372), ("RIPPLE DELETE", 300, 1472), ("DETACH AUDIO", 780, 1472),
         ("FAST EXPORT", 300, 1572), ("UNDO / REDO", 780, 1572)]


def feat_cam(t):
    lt = t - T_DEMO_END
    return Cam(1600, 1000, 0, 0.305, 17 * math.sin(lt * 0.9 + 0.2) - 6, -9 + 2 * math.sin(lt * 0.7), 1.0 * math.sin(lt),
               cy=900)


def scene_features_world(t):
    lt = t - T_DEMO_END
    cam = feat_cam(t)
    a_in = out_cubic(prog(t, T_DEMO_END, T_DEMO_END + 0.35))
    img = background(t, cam.R[0, 2] * 40, 0, 0, 0, 0)
    slab_a = a_in * (1 - in_out(prog(t, T_FEAT_END - T_FEAT_FADE, T_FEAT_END)))
    draw_slab(img, state("08"), cam, a=slab_a, sweep=lerp(-0.2, 1.3, prog(lt, 0.2, 1.5)) if lt < 1.5 else None)
    vis = 1 - in_out(prog(t, T_FEAT_END - T_FEAT_FADE, T_FEAT_END))
    for i, (text, cx, cy) in enumerate(CHIPS):
        t0 = T_DEMO_END + 0.32 + 0.10 * i
        p = out_back(prog(t, t0, t0 + 0.38), 1.6)
        spr = chip_card(text, 440, 82, False)
        draw_card(img, cam, spr, cx, cy + 14 * (1 - p), z=lerp(600, 250, p) + 22 * math.sin(lt * 1.6 + i), rot_=(lerp(40, 0, p), lerp(-60, 0, p), 0),
                  a=prog(t, t0, t0 + 0.1) * vis, extrude=16, ext=((60, 80, 190), (18, 20, 64)), n_ext=3)
    p = out_back(prog(t, T_DEMO_END + 0.95, T_DEMO_END + 1.30), 1.6)
    spr = chip_card("100% FREE", 560, 120, True)
    draw_card(img, cam, spr, W / 2, 1262, z=lerp(800, 420, p) + 14 * math.sin(lt * 1.3), rot_=(0, lerp(-80, 0, p), 0),
              a=prog(t, T_DEMO_END + 0.95, T_DEMO_END + 1.05) * vis, extrude=30, ext=((90, 70, 220), (22, 18, 80)), n_ext=4)
    return img


@lru_cache(maxsize=16)
def chip_card(text, w, h, filled):
    if filled:
        base = hgrad(w, h, BLUE, VIOLET).copy()
    else:
        base = Image.new("RGB", (w, h), (20, 28, 64))
        stroke_rr(base, (0, 0, w, h), h // 2, hgrad(w, h, BLUE, VIOLET), 3)
    put_text(base, text, w / 2, h / 2, 60 if filled else 46, F_BLACK if filled else F_BOLD, WHITE, max_w=w - 50)
    spr = base.convert("RGBA")
    spr.putalpha(rr_mask(w, h, h // 2))
    return spr


def scene_features_overlay(img, t):
    cam = orbit_cam(t)
    vis = 1 - in_out(prog(t, T_FEAT_END - T_FEAT_FADE, T_FEAT_END))
    for text, y, t0 in (("EVERYTHING", 330, T_DEMO_END + 0.05), ("YOU NEED.", 470, T_DEMO_END + 0.2)):
        lt = t - t0
        if lt < 0:
            continue
        s = out_back(prog(lt, 0, 0.3), 1.5)
        spr = tsprite(text, 170, F_BLACK, WHITE, (BLUE, VIOLET) if text == "YOU NEED." else None, 940)
        draw_card(img, cam, spr, W / 2, y, z=lerp(380, 0, s) + 700 * (1 - vis), rot_=(0, lerp(-50, 0, s), 0), fit=False,
                  a=prog(lt, 0, 0.08) * vis, extrude=34,
                  ext=((92, 72, 215), (24, 20, 72)) if text == "YOU NEED." else ((120, 135, 200), (26, 32, 78)), n_ext=5)


# ================================================================== scene 5: call to action
def scene_cta(img, t):
    lt = t - T_FEAT_END
    cam = orbit_cam(t)
    glow(img, W / 2, 600, 520, BLUE, 0.4 * prog(lt, 0, 0.4))
    s = out_back(prog(lt, 0, 0.5), 1.4)
    mark = logo_sprite("mark", 380)
    draw_card(img, cam, mark, W / 2, 640, z=lerp(500, 110, s), rot_=(lerp(110, 0, s) + 12 * math.sin(lt * 1.5), 0, 0),
              a=prog(lt, 0, 0.1), extrude=56, ext=((88, 70, 214), (22, 18, 78)), n_ext=6)
    for text, y, t0, size, grad in (("GET CLIPFORGE", 940, 0.3, 130, None), ("FREE ON GITHUB.", 1080, 0.55, 120, (BLUE, VIOLET))):
        l2 = lt - t0
        if l2 < 0:
            continue
        s2 = out_back(prog(l2, 0, 0.3), 1.5)
        draw_card(img, cam, tsprite(text, size, F_BLACK, WHITE, grad, 940), W / 2, y, z=lerp(300, 50, s2),
                  rot_=(0, lerp(-40, 0, s2), 0), a=prog(l2, 0, 0.08), extrude=26,
                  ext=((92, 72, 215), (24, 20, 72)) if grad else ((120, 135, 200), (26, 32, 78)), n_ext=4)
    p = out_cubic(prog(lt, 0.7, 1.05))
    pill = Image.new("RGB", (880, 130), (20, 28, 64))
    stroke_rr(pill, (0, 0, 880, 130), 65, hgrad(880, 130, BLUE, VIOLET), 3)
    put_text(pill, "github.com/asaptobes/ClipForge", 440, 65, 46, F_BOLD, WHITE, max_w=800)
    pill = pill.convert("RGBA")
    pill.putalpha(rr_mask(880, 130, 65))
    draw_card(img, cam, pill, W / 2, 1275 + 30 * (1 - p), z=lerp(200, 20, p), rot_=(lerp(-30, 0, p), 0, 0), a=p,
              extrude=22, ext=((60, 80, 190), (18, 20, 64)), n_ext=4)
    p2 = prog(lt, 1.0, 1.3)
    put_text(img, "Enjoying it? buymeacoffee.com/asaptobes", W / 2, 1450, 42, F_SEMI, (205, 214, 242), a=p2, max_w=940)
    put_text(img, "For Windows 10 / 11", W / 2, 1530, 34, F_SEMI, MUTED, a=p2, max_w=940)
    put_text(img, "Only download content you own or have permission to use.", W / 2, 1612, 28, F_SEMI,
             (135, 146, 186), a=p2, max_w=940)


# ================================================================== frame assembly
def shutter_samples(t):
    """Sub-frame accumulation count from camera speed (px/frame on screen)."""
    e = 1 / 60
    a, b = demo_pose(t - e), demo_pose(t + e)
    sp = (math.hypot(b[0] - a[0], b[1] - a[1]) * b[2] + abs(math.log(b[2] / a[2])) * 520
          + (abs(b[3] - a[3]) + abs(b[4] - a[4]) + abs(b[5] - a[5])) * 11) * 0.5
    # enough samples that neighbouring exposures sit only a few pixels apart (no ghosting)
    return 1 if sp < 6 else int(min(14, max(4, math.ceil(sp / 5))))


def render_frame(t: float, blur: bool = True) -> Image.Image:
    p = None
    # hook + logo were authored against OLD_FLY0; play them slightly faster so the interface can start earlier
    tw = t * OLD_FLY0 / T_FLY0 if t < T_FLY0 else OLD_FLY0 + (t - T_FLY0)
    if t < T_FLY0:
        img = background(tw, orbit_cam(tw).R[0, 2] * 40, 0, 0, 0, 0)
    elif t < T_DEMO_END:
        p = demo_pose(t)
        n = shutter_samples(t) if blur else 1
        if n == 1:
            img = demo_world(t)
        else:
            acc = None
            shutter = 0.5 / FPS
            for i in range(n):
                f = np.asarray(demo_world(t + (i / (n - 1) - 0.5) * shutter), np.float32)
                acc = f if acc is None else acc + f
            img = Image.fromarray((acc / n + 0.5).astype(np.uint8), "RGB")
    elif t < T_FEAT_END:
        img = scene_features_world(t)
    else:
        img = background(t, orbit_cam(t).R[0, 2] * 40, 0, 0, 0, 0)

    if tw < 2.85:
        scene_hook(img, tw)
    if 2.4 <= tw < 4.75:
        scene_logo(img, tw)
    if p is not None:
        demo_overlay(img, t, p)
    if T_DEMO_END <= t < T_FEAT_END:
        scene_features_overlay(img, t)
    if t >= T_FEAT_END:
        scene_cta(img, t)

    img = ImageChops.multiply(img, vignette())
    f = max((max(0.0, 1 - (t - c) / 0.22) for c in T_FLASH if t >= c), default=0.0) * 0.30
    if f > 0.005:
        img = Image.blend(img, Image.new("RGB", (W, H), (190, 210, 255)), f)
    fade = max(1 - prog(t, 0, 0.12), prog(t, DUR - 0.4, DUR))
    if fade > 0.005:
        img = Image.blend(img, Image.new("RGB", (W, H), BG_BOT), fade)
    return img


# ================================================================== CLI
def _render_bytes(t: float) -> bytes:
    return render_frame(t).tobytes()


def _render_small(t: float):
    return render_frame(t).resize((360, 640), Image.LANCZOS)


def _pool():
    return mp.Pool(max(1, min(14, (mp.cpu_count() or 2) - 2)))


def contact_sheet(frames, labels, cols=4, tw=360, th=640):
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), (0, 0, 0))
    d = ImageDraw.Draw(sheet)
    f = ImageFont.truetype(F_BOLD, 22)
    for i, (fr, lb) in enumerate(zip(frames, labels)):
        x, y = (i % cols) * tw, (i // cols) * th
        sheet.paste(fr if fr.size == (tw, th) else fr.resize((tw, th), Image.LANCZOS), (x, y))
        d.rectangle((x + 4, y + th - 30, x + 80, y + th - 4), fill=(0, 0, 0))
        d.text((x + 10, y + th - 29), lb, font=f, fill=(255, 235, 120))
    return sheet


def parse_times(s: str):
    return [float(x) for x in s.split(",") if x.strip()]


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    argv = sys.argv
    if "--stills" in argv:
        times = parse_times(argv[argv.index("--stills") + 1])
        (OUT_DIR / "stills").mkdir(exist_ok=True)
        for t in times:
            render_frame(t).save(OUT_DIR / "stills" / f"t{t:05.2f}.png")  # (serial; use --sheet for many)
        print("wrote", len(times), "stills to", OUT_DIR / "stills")
        return
    if "--sheet" in argv:
        times = parse_times(argv[argv.index("--sheet") + 1])
        t0 = _time.time()
        with _pool() as pool:
            frames = pool.map(_render_small, times, chunksize=1)
        out = OUT_DIR / (argv[argv.index("--sheet") + 2] if len(argv) > argv.index("--sheet") + 2 else "sheet.png")
        contact_sheet(frames, [f"{t:.2f}s" for t in times]).save(out)
        print(f"wrote {out}  ({(_time.time() - t0) / len(times):.2f}s/frame)")
        return
    if "--video-sheet" in argv:
        n = int(argv[argv.index("--video-sheet") + 1])
        ff = paths.find_tool("ffmpeg")
        frames, labels = [], []
        for i in range(n):
            t = (i + 0.5) * DUR / n
            r = subprocess.run([ff, "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(OUT_DIR / "ClipForge-promo-9x16.mp4"),
                                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
            import io
            frames.append(Image.open(io.BytesIO(r.stdout)).convert("RGB"))
            labels.append(f"{t:.2f}s")
        contact_sheet(frames, labels).save(OUT_DIR / "final_sheet.png")
        print("wrote", OUT_DIR / "final_sheet.png")
        return
    out = OUT_DIR / "ClipForge-promo-9x16.mp4"
    ff = paths.find_tool("ffmpeg")
    cmd = [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-f", "lavfi", "-t", str(DUR), "-i", "anullsrc=r=48000:cl=stereo",
           "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    total = round(DUR * FPS)
    t0 = _time.time()
    with _pool() as pool:
        for i, raw in enumerate(pool.imap(_render_bytes, [i / FPS for i in range(total)], chunksize=2)):
            proc.stdin.write(raw)
            if i % 60 == 0:
                print(f"frame {i}/{total}  {_time.time() - t0:.0f}s", flush=True)
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print("wrote", out, f"in {_time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
