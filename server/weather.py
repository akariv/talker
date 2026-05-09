"""Open-Meteo client + WMO code mapping for the panel's left tile.

Single in-process cache. Stale-on-error: if a refresh fails and we have
something cached, we keep returning it past its TTL.
"""

import json
import logging
import time
import urllib.request
from dataclasses import dataclass

import config

log = logging.getLogger(__name__)

_OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=weather_code,is_day"
    "&daily=temperature_2m_max,temperature_2m_min"
    "&timezone={tz}&forecast_days=1"
)


@dataclass(frozen=True)
class Weather:
    icon: str
    temp_max: float
    temp_min: float


_cache: dict = {"value": None, "fetched_at": 0.0}


def get_weather() -> Weather | None:
    """Returns the latest weather snapshot, or None if we have never
    succeeded fetching one."""
    now = time.monotonic()
    age = now - _cache["fetched_at"]
    if _cache["value"] is not None and age < config.WEATHER["ttl_seconds"]:
        return _cache["value"]

    fresh = _fetch()
    if fresh is not None:
        _cache["value"] = fresh
        _cache["fetched_at"] = now
        return fresh
    # Stale-on-error: return whatever we have, even if past TTL.
    return _cache["value"]


def _fetch() -> Weather | None:
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
        tmax = float(data["daily"]["temperature_2m_max"][0])
        tmin = float(data["daily"]["temperature_2m_min"][0])
    except (KeyError, IndexError, TypeError, ValueError) as e:
        log.warning(f"weather payload malformed: {e} | {data}")
        return None

    return Weather(icon=_icon_for_wmo(code, is_day),
                   temp_max=tmax, temp_min=tmin)


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
