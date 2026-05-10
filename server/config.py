"""Display configuration that's safe to commit to git.

Calendar URLs are NOT here — they're secret-bearing iCal URLs and live in
Firestore at `config/display` (see `db.get_display_config`). Seed them with
`scripts/seed_calendars.py`.
"""

WEATHER = {
    "lat": 52.31,                   # Amstelveen
    "lon": 4.87,
    "tz": "Europe/Amsterdam",
    "ttl_seconds": 300,             # 5-min cache; longer is wasted past idle
}

CALENDAR_TTL_SECONDS = 300          # 5-min cache per .ics URL
EVENT_HORIZON_DAYS = 7              # how far ahead we expand recurring events
EVENTS_TO_SHOW = 4
