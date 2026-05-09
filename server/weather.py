"""Open-Meteo client + WMO code mapping for the panel's left tile.

The cache stores a 'raw' snapshot (today's + tomorrow's high/low, plus
the current is_day flag and icon). At display time the caller passes in
`now` (local tz), and we pick the temperature pair to show:

  * day, OR night-but-before-noon  → today's max → today's min
  * night-after-noon                → tomorrow's min → tomorrow's max

The night-after-noon case is the only one that looks at tomorrow — at,
say, 03:00 we still want to see today's range because today's high is
still ahead.

Cache is stale-on-error: a failed refresh keeps returning the previous
snapshot past TTL.
"""

import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime

import config

log = logging.getLogger(__name__)

_OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=weather_code,is_day"
    "&daily=temperature_2m_max,temperature_2m_min"
    "&timezone={tz}&forecast_days=2"
)


@dataclass(frozen=True)
class Weather:
    """The view that the renderer consumes — already-picked first/second."""
    icon: str
    temp_first: float
    temp_second: float


@dataclass(frozen=True)
class _RawWeather:
    icon: str
    is_day: bool
    today_max: float
    today_min: float
    tomorrow_max: float
    tomorrow_min: float


_cache: dict = {"value": None, "fetched_at": 0.0}


def get_weather(now: datetime) -> Weather | None:
    """Returns the temperature pair to display, given local time `now`.

    None if we've never had a successful fetch.
    """
    raw = _get_or_refresh()
    if raw is None:
        return None

    show_tomorrow = (not raw.is_day) and now.hour >= 12
    if show_tomorrow:
        first, second = raw.tomorrow_min, raw.tomorrow_max
    else:
        first, second = raw.today_max, raw.today_min

    return Weather(icon=raw.icon, temp_first=first, temp_second=second)


def _get_or_refresh() -> _RawWeather | None:
    age = time.monotonic() - _cache["fetched_at"]
    if _cache["value"] is not None and age < config.WEATHER["ttl_seconds"]:
        return _cache["value"]

    fresh = _fetch()
    if fresh is not None:
        _cache["value"] = fresh
        _cache["fetched_at"] = time.monotonic()
        return fresh
    # Stale-on-error: keep returning whatever we have, even past TTL.
    return _cache["value"]


def _fetch() -> _RawWeather | None:
    url = _OPEN_METEO_URL.format(
        lat=config.WEATHER["lat"],
        lon=config.WEATHER["lon"],
        tz=config.WEATHER["tz"],
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.warning(f"weather fetch failed: {e}")
        return None

    try:
        code = int(data["current"]["weather_code"])
        is_day = bool(data["current"]["is_day"])
        tmax = data["daily"]["temperature_2m_max"]
        tmin = data["daily"]["temperature_2m_min"]
        return _RawWeather(
            icon=_icon_for_wmo(code, is_day),
            is_day=is_day,
            today_max=float(tmax[0]),
            today_min=float(tmin[0]),
            tomorrow_max=float(tmax[1]),
            tomorrow_min=float(tmin[1]),
        )
    except (KeyError, IndexError, TypeError, ValueError) as e:
        log.warning(f"weather payload malformed: {e} | {data}")
        return None


def _icon_for_wmo(code: int, is_day: bool) -> str:
    """Map an Open-Meteo WMO code to a filename in server/weather-icons/.

    Filenames omit the .png extension. Day/night variants are picked when
    they exist; otherwise we use the non-prefixed file.
    """
    d = "day_" if is_day else "night_"
    if code == 0:
        return d + "clear"
    if code in (1, 2):
        return d + "partial_cloud"
    if code == 3:
        return "overcast"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 57:
        return "mist"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return d + "rain"
    if code in (71, 73, 75, 77, 85, 86):
        return d + "snow"
    if code == 95:
        return "thunder"
    if code in (96, 99):
        return d + "rain_thunder"
    return "cloudy"
