"""Tests for the display-cam frame renderer.

Runs with `cd server && pytest tests/` (pytest discovers `server/display.py`
relative to the test's working directory via sys.path).
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import display  # noqa: E402


def _bit(plane: bytes, x: int, y: int) -> int:
    """Read MSB-first bit (x, y) from a 128x296 row-major packed plane."""
    assert 0 <= x < display.PANEL_NATIVE_W
    assert 0 <= y < display.PANEL_NATIVE_H
    idx = y * (display.PANEL_NATIVE_W // 8) + (x >> 3)
    return (plane[idx] >> (7 - (x & 7))) & 1


def _nonzero_count(plane: bytes) -> int:
    return sum(bin(b).count("1") for b in plane)


def test_frame_size():
    now = datetime(2026, 4, 19, 14, 37, tzinfo=ZoneInfo("Europe/Berlin"))
    data = display.render_current_time_frame(
        display.LANDSCAPE_W, display.LANDSCAPE_H, now
    )
    assert len(data) == display.FRAME_BYTES == 9472


def test_rejects_unsupported_size():
    now = datetime(2026, 4, 19, 14, 37, tzinfo=ZoneInfo("Europe/Berlin"))
    try:
        display.render_current_time_frame(128, 296, now)
    except ValueError:
        return
    assert False, "expected ValueError for unsupported size"


def test_renders_have_content_in_both_planes():
    """Both planes should have non-trivial content for a normal render:
    red plane has CURRENT TIME pixels set, black plane has HH:MM pixels
    cleared (black ink). Exact pixel locations depend on rotation/layout
    so this is just a sanity check on totals."""
    now = datetime(2026, 4, 19, 14, 37, tzinfo=ZoneInfo("Europe/Berlin"))
    data = display.render_current_time_frame(
        display.LANDSCAPE_W, display.LANDSCAPE_H, now
    )
    black = data[: display.PLANE_BYTES]
    red = data[display.PLANE_BYTES :]

    red_bits = _nonzero_count(red)
    # Black ink count: fraction of cleared bits (since 1 = white).
    black_ink = 8 * len(black) - _nonzero_count(black)

    assert 40 < red_bits < 2000, \
        f"expected modest red ink for 'CURRENT TIME', got {red_bits}"
    assert 200 < black_ink < 8000, \
        f"expected modest black ink for HH:MM, got {black_ink}"


def test_photo_panel_dithered_into_left_area():
    """With a photo payload, the rotated native image's left h×h (before
    rotation) becomes a strip on the panel. After 90° CW rotation the
    photo area lands on the bottom 128 rows of the 128×296 native image.

    Quick check: without a photo, the black plane should have very few
    'black' pixels overall (just the HH:MM text). With a noisy photo, the
    black plane should pick up hundreds more black pixels from dithering."""
    now = datetime(2026, 4, 19, 14, 37, tzinfo=ZoneInfo("Europe/Berlin"))

    baseline = display.render_current_time_frame(
        display.LANDSCAPE_W, display.LANDSCAPE_H, now
    )
    baseline_black = baseline[: display.PLANE_BYTES]
    baseline_ink = 8 * len(baseline_black) - _nonzero_count(baseline_black)

    # Synthetic "noisy" photo: mid-gray everywhere, which dithers to a
    # dense ~50% black checker.
    pw, ph = 320, 240
    noisy = bytes([128] * (pw * ph))
    with_photo = display.render_current_time_frame(
        display.LANDSCAPE_W, display.LANDSCAPE_H, now,
        photo=(noisy, pw, ph),
    )
    with_photo_black = with_photo[: display.PLANE_BYTES]
    with_photo_ink = 8 * len(with_photo_black) - _nonzero_count(with_photo_black)

    assert with_photo_ink > baseline_ink + 1000, (
        f"expected photo dither to add substantial black ink; "
        f"baseline={baseline_ink}, with_photo={with_photo_ink}"
    )


def test_bit_ordering_is_msb_first():
    """Sanity-check that the packer matches the firmware's
    `0x80 >> (x & 7)` bit layout: the top-left pixel of the black plane,
    which is background white (=1 in PIL / firmware), must be the MSB of
    byte 0."""
    now = datetime(2026, 4, 19, 14, 37, tzinfo=ZoneInfo("Europe/Berlin"))
    data = display.render_current_time_frame(
        display.LANDSCAPE_W, display.LANDSCAPE_H, now
    )
    black = data[: display.PLANE_BYTES]
    # x=0, y=0 → bit 7 of byte 0. White background → bit set.
    assert _bit(black, 0, 0) == 1
