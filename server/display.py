"""Render the display-cam frame.

Landscape canvas (296×128) laid out as:
  * Left WEATHER_W×h: today's weather — small icon (top) + high/low temp.
  * Right (w−WEATHER_W)×h: small clock in the top-right corner, then up
    to 3 upcoming calendar entries. Each entry is two lines:
      • date/time line, smaller, black
      • event title, larger, red.
    Calendars + filters come from the Firestore-backed display config.

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

from bidi.algorithm import get_display
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

# Weather column width — about 60% of the panel height. Leaves a wider
# right column for calendar entries.
WEATHER_W = 77
WEATHER_ICON_SIZE = 48
WEATHER_TEMP_FONT = 18

# Calendar lines: each event renders as two stacked lines.
CAL_DATE_FONT = 12
CAL_TITLE_FONT = 16
CAL_EVENT_BLOCK_H = 32   # date(12) + title(16) + 4px inter-event gap
CAL_DATE_TO_TITLE = 14   # baseline-ish offset from date row to title row

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


def _paste_icon(black: Image.Image, name: str, x: int, y: int,
                size: int = 64) -> None:
    """Paint the dark pixels of `<name>.png` into the black plane at (x,y).

    The icon is alpha-composited onto white, optionally resized to
    `size`×`size` (Lanczos), then thresholded; any pixel darker than mid-
    gray becomes panel black (0 in the '1'-mode plane), the rest stays
    white (1).
    """
    src = _load_icon(name)
    bg = Image.new("RGB", src.size, "white")
    bg.paste(src, (0, 0), src)
    if size != src.size[0]:
        bg = bg.resize((size, size), Image.LANCZOS)
    gray = bg.convert("L")
    ink_mask = gray.point(lambda v: 255 if v < 128 else 0).convert("1")
    black.paste(0, (x, y), ink_mask)


def _bidi(text: str) -> str:
    """Apply the Unicode Bidi Algorithm so RTL scripts (Hebrew, Arabic) render
    in visual order. PIL draws codepoints LTR with no bidi handling, so
    Hebrew without this would appear reversed."""
    return get_display(text) if text else text


def _measure(draw: ImageDraw.ImageDraw, text: str, size: int) -> tuple[int, int]:
    font = _font(size)
    x0, y0, x1, y1 = draw.textbbox((0, 0), _bidi(text), font=font)
    return x1 - x0, y1 - y0


def _draw_text(draw: ImageDraw.ImageDraw, text: str, x: int, y: int,
               size: int, fill: int) -> None:
    """Draw `text` at (x, y) with anchor at the visible top-left of the glyph
    bbox. Compensates for PIL's slight bbox offset and applies bidi."""
    font = _font(size)
    visual = _bidi(text)
    bx0, by0, _, _ = draw.textbbox((0, 0), visual, font=font)
    draw.text((x - bx0, y - by0), visual, font=font, fill=fill)


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
                  column_w: int) -> None:
    """Left column: small weather icon (top) + temp range below."""
    w = weather.get_weather()
    if w is None:
        _draw_text_centered(draw_b, "—", column_w // 2, 36, 24, fill=0)
        return

    icon_size = WEATHER_ICON_SIZE
    icon_x = (column_w - icon_size) // 2
    icon_y = 8
    _paste_icon(black, w.icon, icon_x, icon_y, size=icon_size)

    # Temp range a little below the icon.
    temp_text = f"{round(w.temp_max)}°/{round(w.temp_min)}°"
    _draw_text_centered(draw_b, temp_text, column_w // 2,
                        y=icon_y + icon_size + 6,
                        size=WEATHER_TEMP_FONT, fill=0)


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
    """Draw up to 3 events; each event is a date/time line (small black) on
    top of the title (larger red, ellipsis-truncated to the column width)."""
    events = calendars.get_upcoming_events(now)
    if not events:
        return

    for i, ev in enumerate(events):
        if i >= 3:
            break
        y = y0 + i * CAL_EVENT_BLOCK_H
        when = _format_when(ev.start, ev.all_day, now)
        _draw_text(draw_b, when, x0, y, CAL_DATE_FONT, fill=0)

        title = _truncate_to_width(draw_b, ev.summary, width, CAL_TITLE_FONT)
        _draw_text(draw_r, title, x0, y + CAL_DATE_TO_TITLE,
                   CAL_TITLE_FONT, fill=1)


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

    # Left column: weather (narrower than before so calendars get more room).
    _draw_weather(black, d_black, column_w=WEATHER_W)

    # Right area geometry.
    right_x0 = WEATHER_W
    right_w = w - WEATHER_W
    right_padding = 6

    # Top-right clock.
    _draw_clock(d_black, right_x=w - right_padding, y=2, now=now)

    # Calendar list, leaving room above for the clock.
    cal_y0 = 20
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
