"""Sample payloads.

The alert bodies are real messages taken off the wxalerts broker, trimmed of
their long ``description`` and geometry so the fixtures stay readable. The
per-user places, status and storm cell bodies follow the shapes in the v1.0.0
topic contract (Home Assistant v2 spec), since the server side is new.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

# Account
USERNAME = "ha-k7x2q9mfw3"
PASSWORD = "Wq8rT2zLp5"
ACCESS_TOKEN = "at-1f9c2d"
REFRESH_TOKEN = "rt-77ab01"
INSTANCE_ID = "5c0f9a1e2b7d4c38a6e1f0d2c3b4a596"
PREFIX = f"wxalerts/u/{USERNAME}/"
TOPIC_PLACES = f"{PREFIX}places"
TOPIC_STATUS = f"{PREFIX}status"

# Topics
SAME_SANTA_ROSA = "012113"  # Santa Rosa County, FL
SAME_AUTAUGA = "001001"  # Autauga County, AL

TOPIC_TORNADO = f"wxalerts/nws/v1/same/{SAME_SANTA_ROSA}/0012"
TOPIC_SEVERE = f"wxalerts/nws/v1/same/{SAME_SANTA_ROSA}/0044"
TOPIC_HEAT = f"wxalerts/nws/v1/same/{SAME_AUTAUGA}/0008"

# Alert payloads
TORNADO_ALERT: dict = {
    "id": 1234,
    "vtec": "KMOB.TO.W.0012.2026",
    "event": "Tornado Warning",
    "office": "KMOB",
    "phenomena": "TO",
    "significance": "W",
    "etn": 12,
    "action": "NEW",
    "status": "active",
    "severity": "Extreme",
    "urgency": "Immediate",
    "certainty": "Observed",
    "issued_at": "2026-08-11T20:15:00+00:00",
    "onset": "2026-08-11T20:15:00+00:00",
    "expires": "2026-08-11T20:45:00+00:00",
    "ends": "2026-08-11T20:45:00+00:00",
    "ugc": ["FLC113", "ALC003"],
    "same": [SAME_SANTA_ROSA, "001003"],
    "headline": "TORNADO WARNING IN EFFECT UNTIL 345 PM CDT",
    "description": "At 315 PM CDT, a severe thunderstorm capable of...",
    "instruction": "TAKE COVER NOW! Move to a basement...",
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [
                [-87.10, 30.60],
                [-87.00, 30.60],
                [-87.00, 30.70],
                [-87.10, 30.70],
                [-87.10, 30.60],
            ]
        ],
    },
    "geometry_source": "polygon",
    "sources": ["emwin", "api"],
}

SEVERE_ALERT: dict = {
    "id": 1235,
    "vtec": "KMOB.SV.W.0044.2026",
    "event": "Severe Thunderstorm Warning",
    "office": "KMOB",
    "phenomena": "SV",
    "significance": "W",
    "etn": 44,
    "action": "NEW",
    "status": "active",
    "severity": "Severe",
    "urgency": "Immediate",
    "certainty": "Observed",
    "onset": "2026-08-11T20:20:00+00:00",
    "ends": "2026-08-11T21:00:00+00:00",
    "same": [SAME_SANTA_ROSA],
    "headline": "SEVERE THUNDERSTORM WARNING IN EFFECT UNTIL 400 PM CDT",
    "instruction": "For your protection move to an interior room...",
    "geometry": {
        "type": "MultiPolygon",
        "coordinates": [
            [
                [
                    [-87.20, 30.50],
                    [-87.00, 30.50],
                    [-87.00, 30.70],
                    [-87.20, 30.70],
                    [-87.20, 30.50],
                ]
            ]
        ],
    },
    "geometry_source": "ugc",
    "sources": ["api"],
}

# Real capture: a Heat Advisory for Autauga County, AL (severity Moderate,
# action CON, i.e. an update to a hazard already in effect).
HEAT_ALERT: dict = {
    "id": 255,
    "vtec": "KBMX.HT.Y.0008.2026",
    "event": "Heat Advisory",
    "office": "KBMX",
    "phenomena": "HT",
    "significance": "Y",
    "etn": 8,
    "action": "CON",
    "status": "active",
    "severity": "Moderate",
    "urgency": "Expected",
    "certainty": "Likely",
    "issued_at": "2026-08-13T15:34:00+00:00",
    "onset": "2026-08-13T15:34:00+00:00",
    "expires": "2026-08-14T07:00:00+00:00",
    "ends": "2026-08-16T02:00:00+00:00",
    "same": [SAME_AUTAUGA],
    "headline": "Heat Advisory issued August 13 at 10:34AM CDT",
    "instruction": "Drink plenty of fluids, stay in an air-conditioned room...",
    "geometry": None,
    "geometry_source": "none",
    "sources": ["api"],
}

def encode(payload) -> bytes:
    """JSON-encode a payload the way the broker delivers it."""
    return json.dumps(payload).encode()


TOMBSTONE = b""

# Places
PLACE_HOME = {
    "id": 101,
    "name": "Home",
    "lat": 30.6435,
    "lon": -87.0545,
    "same": SAME_SANTA_ROSA,
}
PLACE_CABIN = {
    "id": 102,
    "name": "Cabin",
    "lat": 32.5361,
    "lon": -86.6435,
    "same": SAME_AUTAUGA,
}
# Offshore: no county, so storm cells only.
PLACE_BOAT = {"id": 103, "name": "Boat", "lat": 29.0, "lon": -87.5}
PLACES = [PLACE_HOME, PLACE_CABIN]

# The same places as the entry caches them (ids as strings, same normalised).
CACHED_PLACES = [
    {"id": "101", "name": "Home", "lat": 30.6435, "lon": -87.0545, "same": SAME_SANTA_ROSA},
    {"id": "102", "name": "Cabin", "lat": 32.5361, "lon": -86.6435, "same": SAME_AUTAUGA},
]


# Status and storm cells
def iso_now(offset_seconds: float = 0.0) -> str:
    return datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + offset_seconds, tz=timezone.utc
    ).isoformat()


def status(online: bool = True, age_seconds: float = 0.0) -> dict:
    return {"online": online, "updated_at": iso_now(-age_seconds)}


STORM_ID = "ps-449120"


def storm_topic(place_id, storm_id: str = STORM_ID) -> str:
    return f"{PREFIX}storms/place/{place_id}/{storm_id}"


def storm(
    place_id=101,
    *,
    episode: str = "open",
    rung: int = 40,
    rung_label: str = "Thunderstorm Risk",
    eta_min: float = 18.0,
    storm_id: str = STORM_ID,
    age_seconds: float = 0.0,
    **overrides,
) -> dict:
    """A storm cell message in the contract's shape, stamped to now."""
    return {
        "storm_id": storm_id,
        "place_id": place_id,
        "place_name": "Home",
        "episode": episode,
        "eta_min": eta_min,
        "rung": rung,
        "rung_label": rung_label,
        "prob_severe": 46,
        "prob_tor": 3,
        "prob_hail": 22,
        "prob_wind": 41,
        "speed_kmh": 52.0,
        "bearing_deg": 245.0,
        "distance_km": 15.6,
        "issued_at": iso_now(-age_seconds - 120),
        "updated_at": iso_now(-age_seconds),
        **overrides,
    }


def registration(username: str = USERNAME, password: str = PASSWORD) -> dict:
    """POST /integrations/home-assistant/instances, as the contract shapes it."""
    return {
        "instance_id": INSTANCE_ID,
        "name": "Home",
        "username": username,
        "password": password,
        "host": "mqtt.wxalerts.org",
        "port": 443,
        "ws_path": "/mqtt",
        "transport": "websockets",
        "tls": True,
        "topic_prefix": f"wxalerts/u/{username}/",
    }


def conditions_topic(place_id) -> str:
    return f"{PREFIX}conditions/place/{place_id}"


def conditions(age_seconds: float = 300.0, **overrides) -> dict:
    """A current-conditions message in the contract's shape."""
    observed = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - age_seconds, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "station_id": "KNDZ",
        "station_name": "Whiting Field",
        "distance_km": 9.4,
        "observed_at": observed,
        "temperature_c": 24.4,
        "dewpoint_c": 21.1,
        "relative_humidity": 82,
        "wind_speed_kmh": 18.5,
        "wind_gust_kmh": 33.3,
        "wind_direction_deg": 200,
        "pressure_hpa": 1012.9,
        "visibility_km": 16.09,
        "condition": "Light Rain",
        "condition_code": "rain",
        "feels_like_c": 25.6,
        "precip_last_hour_mm": 1.3,
        **overrides,
    }


def forecast_topic(place_id) -> str:
    return f"{PREFIX}forecast/place/{place_id}"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def forecast(issued_age_seconds: float = 600.0, days: int = 7, hours: int = 48) -> dict:
    """A forecast message in the contract's shape, starting yesterday (daily)
    and two hours ago (hourly), so the trimming has something to drop."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    top_of_hour = now.replace(minute=0, second=0, microsecond=0)
    from homeassistant.util import dt as dt_util

    # Home Assistant's time zone, which is what "today" is measured in.
    local_midnight = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    daily = []
    for n in range(-1, days - 1):
        day = local_midnight + timedelta(days=n)
        daily.append(
            {
                "date": day.date().isoformat(),
                "datetime": _iso(day.astimezone(timezone.utc)),
                "condition_code": "tsra" if n == 0 else "partly_cloudy",
                "temp_high_c": 31.0 + n,
                "temp_low_c": 22.0,
                "precip_probability": 60,
                "precip_mm": 8.4,
                "wind_speed_kmh": 14.0,
                "wind_gust_kmh": 40.0,
                "wind_direction_deg": 180,
                "humidity": 78,
                "uv_index": 9 if n == 0 else None,
                "summary": "Storms likely" if n == 0 else None,
            }
        )
    hourly = []
    for n in range(-2, hours - 2):
        when = top_of_hour + timedelta(hours=n)
        hourly.append(
            {
                "datetime": _iso(when),
                "condition_code": "clear",
                "temperature_c": 25.0 + n / 10,
                "apparent_temperature_c": 27.0,
                "dewpoint_c": 21.0,
                "humidity": 80,
                "precip_probability": 10,
                "precip_mm": 0.0,
                "wind_speed_kmh": 9.0,
                "wind_gust_kmh": None,
                "wind_direction_deg": 150,
                "cloud_cover": 5,
                "is_daytime": n % 2 == 0,
            }
        )
    return {
        "issued_at": _iso(now - timedelta(seconds=issued_age_seconds)),
        "source": "nws",
        "daily": daily,
        "hourly": hourly,
    }
