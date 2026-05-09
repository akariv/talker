"""Render the display-cam frame.

Landscape canvas (296×128) laid out as:
  * Left h×h square: today's weather — icon (top) + high/low temp range.
  * Right (w−h)×h: small clock in the top-right corner, then up to 3
    upcoming calendar event lines (calendars + filters defined in config).

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
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import calendars
import weather

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

_ICON_DIR = Path(__file__).parent / "weather-icons"

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}
_icon_cache: dict[str, Image.Image] = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    f = _font_cache.get(size)
    if f is None:
        f = ImageFont.truetype(_font_path(), size)
        _font_cache[size] = f
    return f


def _load_icon(name: str) -> Image.Image:
    img = _icon_cache.get(name)
    if img is None:
        img = Image.open(_ICON_DIR / f"{name}.png").convert("RGBA")
        _icon_cache[name] = img
    return img


def _paste_icon(black: Image.Image, name: str, x: int, y: int) -> None:
    """Paint the dark pixels of `<name>.png` into the black plane at (x,y).

    The icon is alpha-composited onto a white background and thresholded;
    any pixel darker than mid-gray after that becomes panel black (0 in
    the '1'-mode plane), the rest stays white (1).
    """
    src = _load_icon(name)
    bg = Image.new("RGB", src.size, "white")
    bg.paste(src, (0, 0), src)
    gray = bg.convert("L")
    ink_mask = gray.point(lambda v: 255 if v < 128 else 0).convert("1")
    black.paste(0, (x, y), ink_mask)


def _measure(draw: ImageDraw.ImageDraw, text: str, size: int) -> tuple[int, int]:
    font = _font(size)
    x0, y0, x1, y1 = draw.textbbox((0, 0), text, font=font)
    return x1 - x0, y1 - y0


def _draw_text(draw: ImageDraw.ImageDraw, text: str, x: int, y: int,
               size: int, fill: int) -> None:
    """Draw `text` at (x, y) with anchor at the visible top-left of the glyph
    bbox. Compensates for PIL's slight bbox offset."""
    font = _font(size)
    bx0, by0, _, _ = draw.textbbox((0, 0), text, font=font)
    draw.text((x - bx0, y - by0), text, font=font, fill=fill)


def _draw_text_centered(draw: ImageDraw.ImageDraw, text: str,
                        cx: int, y: int, size: int, fill: int) -> None:
    w, _ = _measure(draw, text, size)
    _draw_text(draw, text, cx - w // 2, y, size, fill)


def _draw_text_right(draw: ImageDraw.ImageDraw, text: str,
                     right_x: int, y: int, size: int, fill: int) -> None:
    w, _ = _measure(draw, text, size)
    _draw_text(draw, text, right_x - w, y, size, fill)


def _truncate_to_width(draw: ImageDraw.ImageDraw, text: str,
                       max_w: int, size: int) -> str:
    if _measure(draw, text, size)[0] <= max_w:
        return text
    ell = "…"
    # Trim greedily until "<prefix>…" fits.
    while text and _measure(draw, text + ell, size)[0] > max_w:
        text = text[:-1]
    return (text + ell) if text else ""


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

def _draw_weather(black: Image.Image, draw_b: ImageDraw.ImageDraw,
                  square: int) -> None:
    """Left h×h square: weather icon at top, temp range below."""
    w = weather.get_weather()
    if w is None:
        _draw_text_centered(draw_b, "—", square // 2, 50, 48, fill=0)
        return

    # 64×64 icon, top-centered with 12px top padding.
    icon_x = (square - 64) // 2
    icon_y = 12
    _paste_icon(black, w.icon, icon_x, icon_y)

    # Temp range below the icon. Don't overlap with the icon's bottom (76).
    temp_text = f"{round(w.temp_max)}°/{round(w.temp_min)}°"
    _draw_text_centered(draw_b, temp_text, square // 2, 90, size=26, fill=0)


def _format_when(start: datetime, all_day: bool, now: datetime) -> str:
    """Compact "when" prefix for a calendar line."""
    delta = start - now
    minutes = int(delta.total_seconds() // 60)
    if 0 <= minutes < 60:
        return "now" if minutes == 0 else f"in {minutes}m"

    today = now.date()
    tomorrow = today + timedelta(days=1)
    sd = start.date()

    if all_day:
        if sd == today:
            return "today"
        if sd == tomorrow:
            return "tomorrow"
        return start.strftime("%a")  # Mon, Tue, ...

    hhmm = start.strftime("%H:%M")
    if sd == today:
        return hhmm
    if sd == tomorrow:
        return f"tom {hhmm}"
    return f"{start.strftime('%a')} {hhmm}"


def _draw_calendar(draw_b: ImageDraw.ImageDraw, draw_r: ImageDraw.ImageDraw,
                   x0: int, y0: int, width: int, now: datetime) -> None:
    """Draw 3 calendar lines into the right column.

    Each line: red `<when>` prefix, gap, black summary truncated to fit.
    """
    events = calendars.get_upcoming_events(now)
    if not events:
        return

    line_h = 22
    text_size = 16
    # Gap between the "when" prefix and the summary, in pixels.
    gap = 6

    for i, ev in enumerate(events):
        if i >= 3:
            break
        y = y0 + i * line_h
        when = _format_when(ev.start, ev.all_day, now)
        # `when` in red.
        _draw_text(draw_r, when, x0, y, text_size, fill=1)
        when_w, _ = _measure(draw_r, when, text_size)
        # Summary in black, truncated to remaining width.
        avail = width - when_w - gap
        summary = _truncate_to_width(draw_b, ev.summary, avail, text_size)
        _draw_text(draw_b, summary, x0 + when_w + gap, y, text_size, fill=0)


def _draw_clock(draw_b: ImageDraw.ImageDraw, right_x: int, y: int,
                now: datetime) -> None:
    """Small HH:MM in the top-right corner."""
    _draw_text_right(draw_b, now.strftime("%H:%M"), right_x, y,
                     size=14, fill=0)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def render_frame(w: int, h: int, now: datetime) -> bytes:
    """Render a landscape frame and rotate into panel-native scan order."""
    if (w, h) != (LANDSCAPE_W, LANDSCAPE_H):
        raise ValueError(f"unsupported logical size {w}x{h}; "
                         f"expected {LANDSCAPE_W}x{LANDSCAPE_H}")

    black = Image.new("1", (w, h), 1)   # white background
    red = Image.new("1", (w, h), 0)     # no red
    d_black = ImageDraw.Draw(black)
    d_red = ImageDraw.Draw(red)

    # Left square: weather.
    _draw_weather(black, d_black, square=h)

    # Right area geometry.
    right_x0 = h
    right_w = w - h
    right_padding = 6

    # Top-right clock.
    _draw_clock(d_black, right_x=w - right_padding, y=2, now=now)

    # Calendar list, leaving room above for the clock.
    cal_y0 = 22
    cal_x0 = right_x0 + right_padding
    cal_w = right_w - 2 * right_padding
    _draw_calendar(d_black, d_red, cal_x0, cal_y0, cal_w, now)

    # Rotate 90° CW into panel-native (128, 296).
    black_n = black.rotate(-90, expand=True)
    red_n = red.rotate(-90, expand=True)
    assert black_n.size == (PANEL_NATIVE_W, PANEL_NATIVE_H)

    return black_n.tobytes() + red_n.tobytes()


# Backwards-compat alias so anything importing the old name still works
# until the call site is updated.
render_current_time_frame = render_frame
