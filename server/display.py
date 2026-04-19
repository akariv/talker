"""Render the display-cam frame.

Landscape canvas (296×128) laid out as:
  * Left h×h square: last uploaded camera frame, center-cropped to
    square, scaled to h×h, Floyd-Steinberg dithered to 1 bit. If no
    photo has arrived yet, the square stays white.
  * Right (w-h)×h: 'CURRENT TIME' (small red) over HH:MM (black).

After drawing, both planes are rotated 90° clockwise into the panel's
native 128×296 scan order and returned as concatenated bit-packed bytes
(9 472 total).

Bit conventions match the SSD1680 driver in firmware:
  * black plane: 1 = white, 0 = black  — matches PIL '1' mode directly.
  * red plane:   1 = red,   0 = transparent — draw 'on' on a black bg.

MSB-first packing (PIL's default for mode '1') matches the panel's
`0x80 >> (x & 7)` bit layout.
"""

import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# First existing path wins. Dockerfile installs fonts-dejavu-core, so the
# Debian path is the prod target; the macOS path covers local dev.
_FONT_CANDIDATES = (
    os.environ.get("DISPLAY_CAM_FONT"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
)


def _font_path() -> str:
    for p in _FONT_CANDIDATES:
        if p and os.path.exists(p):
            return p
    raise RuntimeError(
        "no bold TTF available; install fonts-dejavu-core or "
        "set DISPLAY_CAM_FONT to a usable .ttf/.ttc path"
    )

PANEL_NATIVE_W = 128
PANEL_NATIVE_H = 296
PLANE_BYTES = PANEL_NATIVE_W * PANEL_NATIVE_H // 8  # 4 736
FRAME_BYTES = PLANE_BYTES * 2                       # 9 472

LANDSCAPE_W = PANEL_NATIVE_H  # 296
LANDSCAPE_H = PANEL_NATIVE_W  # 128


_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    f = _font_cache.get(size)
    if f is None:
        f = ImageFont.truetype(_font_path(), size)
        _font_cache[size] = f
    return f


def _draw_centered(draw: ImageDraw.ImageDraw, text: str,
                   x0: int, box_w: int, y: int,
                   fill: int, size: int) -> None:
    """Center `text` horizontally in the strip [x0, x0+box_w), top-aligned
    to `y`."""
    font = _font(size)
    bx0, by0, bx1, _ = draw.textbbox((0, 0), text, font=font)
    tw = bx1 - bx0
    draw.text((x0 + (box_w - tw) // 2 - bx0, y - by0),
              text, font=font, fill=fill)


def _photo_panel(photo_bytes: bytes, photo_w: int, photo_h: int,
                 side: int) -> Image.Image:
    """Load grayscale bytes, center-crop to square, scale to side×side,
    Floyd-Steinberg dither to 1 bit. Returns a mode-'1' image."""
    src = Image.frombytes("L", (photo_w, photo_h), photo_bytes)
    sq = min(photo_w, photo_h)
    x0 = (photo_w - sq) // 2
    y0 = (photo_h - sq) // 2
    src = src.crop((x0, y0, x0 + sq, y0 + sq))
    src = src.resize((side, side), Image.Resampling.LANCZOS)
    return src.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def render_current_time_frame(
    w: int, h: int, now: datetime,
    photo: tuple[bytes, int, int] | None = None,
) -> bytes:
    """Render a landscape frame and rotate into panel-native scan order.

    `photo` is (grayscale_bytes, width, height) for the last uploaded
    camera frame, or None if none has arrived yet. Only the compiled panel
    geometry is supported; callers must validate `w`/`h` against
    (LANDSCAPE_W, LANDSCAPE_H) and return 400 on mismatch.
    """
    if (w, h) != (LANDSCAPE_W, LANDSCAPE_H):
        raise ValueError(f"unsupported logical size {w}x{h}; "
                         f"expected {LANDSCAPE_W}x{LANDSCAPE_H}")

    black = Image.new("1", (w, h), 1)   # white background
    red = Image.new("1", (w, h), 0)     # no red
    d_black = ImageDraw.Draw(black)
    d_red = ImageDraw.Draw(red)

    # Left h×h photo panel on the black plane, if we have one.
    if photo is not None:
        photo_bytes, photo_w, photo_h = photo
        try:
            panel = _photo_panel(photo_bytes, photo_w, photo_h, h)
            black.paste(panel, (0, 0))
        except Exception:
            # Malformed photo — skip silently, leave the square white.
            pass

    # Text area: [h, w) × [0, h), width = w - h.
    text_x0 = h
    text_w = w - h

    _draw_centered(d_red, "CURRENT TIME",
                   text_x0, text_w, y=12, fill=1, size=18)
    _draw_centered(d_black, now.strftime("%H:%M"),
                   text_x0, text_w, y=46, fill=0, size=56)

    # Rotate 90° CW into panel-native (128, 296).
    black_n = black.rotate(-90, expand=True)
    red_n = red.rotate(-90, expand=True)
    assert black_n.size == (PANEL_NATIVE_W, PANEL_NATIVE_H)

    return black_n.tobytes() + red_n.tobytes()
