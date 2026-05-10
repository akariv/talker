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

import hashlib
import json
import os
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

import calendars
import config
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
WEATHER_ICON_SIZE = 60
WEATHER_TEMP_FONT = 18
WEATHER_TEMP_Y = 62  # fixed; doesn't shift with icon size

# Icon binarization: gamma-darken midtones, then hard-threshold. The gamma
# pass rescues light-gray detail (e.g., the inner cloud in the overcast
# icon) that would otherwise be dropped by the threshold.
ICON_GAMMA = 1.7
ICON_THRESHOLD = 128
_ICON_GAMMA_LUT = [
    int(round(255 * (v / 255) ** ICON_GAMMA)) for v in range(256)
]

# Calendar lines: each event renders as two stacked lines.
CAL_DATE_FONT = 12
CAL_TITLE_FONT = 16
CAL_DATE_TO_TITLE = 14    # date-row top → title-row top
CAL_TITLE_BAND_H = 16     # height occupied by the title row
CAL_EVENT_BLOCK_H = CAL_DATE_TO_TITLE + CAL_TITLE_BAND_H  # 30
CAL_INTER_EVENT_GAP = 2
CAL_EVENT_STRIDE = CAL_EVENT_BLOCK_H + CAL_INTER_EVENT_GAP  # 32

# Horizontal gap between the weather column and the calendar text.
# Was 6; +2 per the layout pass.
CAL_LEFT_PADDING = 8
CAL_RIGHT_PADDING = 2

# Bottom-left date geometry.
DATE_FONT = 14
DATE_X = 4
DATE_BOTTOM_MARGIN = 2

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
    """Paint `<name>.png` into the black plane at (x,y).

    The icon is alpha-composited onto white, optionally resized to
    `size`×`size` (Lanczos), gamma-darkened, then thresholded to 1-bit.
    The gamma pass keeps light-gray strokes legible after thresholding.
    """
    src = _load_icon(name)
    bg = Image.new("RGB", src.size, "white")
    bg.paste(src, (0, 0), src)
    if size != src.size[0]:
        bg = bg.resize((size, size), Image.LANCZOS)
    darkened = bg.convert("L").point(_ICON_GAMMA_LUT)
    ink_mask = darkened.point(
        lambda v: 255 if v < ICON_THRESHOLD else 0
    ).convert("1")
    black.paste(0, (x, y), ink_mask)


def _bidi(text: str) -> str:
    """Apply the Unicode Bidi Algorithm so RTL scripts (Hebrew, Arabic) render
    in visual order. PIL draws codepoints LTR with no bidi handling, so
    Hebrew without this would appear reversed."""
    return get_display(text) if text else text


def _is_rtl(text: str) -> bool:
    """First-strong-directional heuristic: if the first strongly-directional
    character is RTL (Hebrew/Arabic/etc.), the whole line is treated as RTL
    for alignment purposes."""
    for ch in text:
        d = unicodedata.bidirectional(ch)
        if d in ("R", "AL"):
            return True
        if d == "L":
            return False
    return False


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
                  column_w: int, now: datetime) -> None:
    """Left column: small weather icon (top) + temp range below.

    `now` is the local-tz datetime; weather.get_weather uses it to decide
    whether to show today's or tomorrow's temperature pair.
    """
    w = weather.get_weather(now)
    if w is None:
        _draw_text_centered(draw_b, "—", column_w // 2, 36, 24, fill=0)
        return

    icon_size = WEATHER_ICON_SIZE
    icon_x = (column_w - icon_size) // 2
    icon_y = 0
    _paste_icon(black, w.icon, icon_x, icon_y, size=icon_size)

    # Temp range at a fixed y so it stays put if the icon resizes.
    temp_text = f"{round(w.temp_first)}°/{round(w.temp_second)}°"
    _draw_text_centered(draw_b, temp_text, column_w // 2,
                        y=WEATHER_TEMP_Y,
                        size=WEATHER_TEMP_FONT, fill=0)


def _format_when(start: datetime, end: datetime | None,
                 all_day: bool, now: datetime) -> str:
    """Compact 'when' prefix for a calendar line.

    All-day events use day-name format ('today', 'tomorrow', 'Mon', ...).
    Timed events use 'HH:MM - HH:MM' (or just 'HH:MM' when there's no end
    or the event spans days), preceded by a day prefix when not today.
    """
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
        return start.strftime("%a")

    # Timed event: build "HH:MM" or "HH:MM - HH:MM".
    hhmm_start = start.strftime("%H:%M")
    if end is not None and end.date() == start.date() and end > start:
        time_part = f"{hhmm_start} - {end.strftime('%H:%M')}"
    else:
        # No end, or multi-day span — just show the start time.
        time_part = hhmm_start

    if sd == today:
        return time_part
    if sd == tomorrow:
        return f"tom {time_part}"
    return f"{start.strftime('%a')} {time_part}"


def _draw_calendar(draw_b: ImageDraw.ImageDraw, draw_r: ImageDraw.ImageDraw,
                   x0: int, width: int, height: int,
                   now: datetime) -> None:
    """Draw up to EVENTS_TO_SHOW events, vertically centered in `height`.

    Each event is two lines:
      • date/time — small, black, left-aligned
      • title     — larger, red, truncated; right-aligned for RTL strings,
                    left-aligned otherwise.
    """
    events = calendars.get_upcoming_events(now)
    if not events:
        return

    n = min(len(events), config.EVENTS_TO_SHOW)
    total_h = n * CAL_EVENT_BLOCK_H + (n - 1) * CAL_INTER_EVENT_GAP
    y0 = max(0, (height - total_h) // 2)

    right_x = x0 + width  # for right-aligning RTL titles

    for i, ev in enumerate(events[:n]):
        y = y0 + i * CAL_EVENT_STRIDE
        when = _format_when(ev.start, ev.end, ev.all_day, now)
        _draw_text(draw_b, when, x0, y, CAL_DATE_FONT, fill=0)

        title = _truncate_to_width(draw_b, ev.summary, width, CAL_TITLE_FONT)
        title_y = y + CAL_DATE_TO_TITLE
        if _is_rtl(title):
            _draw_text_right(draw_r, title, right_x, title_y,
                             CAL_TITLE_FONT, fill=1)
        else:
            _draw_text(draw_r, title, x0, title_y, CAL_TITLE_FONT, fill=1)


def _draw_date(draw_b: ImageDraw.ImageDraw, x: int, y_bottom: int,
               now: datetime) -> None:
    """Small date string ('May 9' / 'Nov 10') anchored at the bottom-left."""
    text = f"{now:%b} {now.day}"
    _draw_text(draw_b, text, x, y_bottom - DATE_FONT,
               size=DATE_FONT, fill=0)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def _render_inputs(now: datetime) -> dict:
    w = weather.get_weather(now)
    weather_part = None
    if w is not None:
        weather_part = {
            "icon": w.icon,
            "first": round(w.temp_first),
            "second": round(w.temp_second),
        }
    events_part = [
        {
            "when": _format_when(ev.start, ev.end, ev.all_day, now),
            "summary": ev.summary,
        }
        for ev in calendars.get_upcoming_events(now)
    ]
    return {
        "date": f"{now:%b} {now.day}",
        "weather": weather_part,
        "events": events_part,
    }


def render_inputs_hash(now: datetime) -> str:
    """16-char hex digest of what `render_frame` would draw at `now`.

    Stable across processes: depends only on the data the renderer
    consumes (date label, rounded weather, formatted event list). Two
    `now`s that produce identical pixels yield identical hashes.
    """
    blob = json.dumps(_render_inputs(now), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
    _draw_weather(black, d_black, column_w=WEATHER_W, now=now)

    # Bottom-left date, sits inside the weather column below the temp text.
    _draw_date(d_black, x=DATE_X, y_bottom=h - DATE_BOTTOM_MARGIN, now=now)

    # Calendar list — vertically centered in the right column.
    cal_x0 = WEATHER_W + CAL_LEFT_PADDING
    cal_w = w - cal_x0 - CAL_RIGHT_PADDING
    _draw_calendar(d_black, d_red, cal_x0, cal_w, h, now)

    # Rotate 90° CW into panel-native (128, 296).
    black_n = black.rotate(-90, expand=True)
    red_n = red.rotate(-90, expand=True)
    assert black_n.size == (PANEL_NATIVE_W, PANEL_NATIVE_H)

    return black_n.tobytes() + red_n.tobytes()


# Backwards-compat alias so anything importing the old name still works
# until the call site is updated.
render_current_time_frame = render_frame
