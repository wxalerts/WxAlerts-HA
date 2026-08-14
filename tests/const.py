"""Sample payloads, captured from the live wxalerts feed.

The alert and lightning bodies below are real messages taken off
``172.16.50.118:1883`` (the LAN face of the same broker the integration talks
to over websockets), trimmed of their long ``description`` and geometry so the
fixtures stay readable. Anything a test asserts on is verbatim.
"""

from __future__ import annotations

import json

# --- Topics ---------------------------------------------------------------

SAME_SANTA_ROSA = "012113"  # Santa Rosa County, FL
SAME_AUTAUGA = "001001"  # Autauga County, AL

TOPIC_TORNADO = f"wxalerts/nws/v1/same/{SAME_SANTA_ROSA}/0012"
TOPIC_SEVERE = f"wxalerts/nws/v1/same/{SAME_SANTA_ROSA}/0044"
TOPIC_HEAT = f"wxalerts/nws/v1/same/{SAME_AUTAUGA}/0008"

# A leaf lightning cell inside geohash box "dj6" / "dj6n".
TOPIC_GLM_LEAF = "wxalerts/glm/v1/d/j/6/n/7"

# --- Alert payloads -------------------------------------------------------

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
# action CON — i.e. an update to a hazard already in effect).
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

# --- Lightning payload ----------------------------------------------------

# Real capture, geohash rewritten to a cell inside the test's dj6 box.
GLM_BURST: dict = {
    "geohash": "dj6n7",
    "satellite": "goes19",
    "window_start": "2026-08-13T23:57:40+00:00",
    "window_end": "2026-08-13T23:58:00+00:00",
    "count": 2,
    "flashes": [
        {
            "t": "2026-08-13T23:57:39.195468+00:00",
            "lat": 30.6103,
            "lon": -87.0547,
            "energy_j": 1.21e-13,
            "area_m2": 390000000.0,
        },
        {
            "t": "2026-08-13T23:57:44.695584+00:00",
            "lat": 30.6110,
            "lon": -87.0530,
            "energy_j": 3.28e-13,
            "area_m2": 519000000.0,
        },
    ],
}


def encode(payload: dict) -> bytes:
    """JSON-encode a payload the way the broker delivers it."""
    return json.dumps(payload).encode()


TOMBSTONE = b""

# --- Config entry data ----------------------------------------------------

LOCATION_SANTA_ROSA = {
    "zone_entity_id": "zone.home",
    "name": "Home",
    "latitude": 30.6435,
    "longitude": -87.0545,
    "same": SAME_SANTA_ROSA,
    "county": "FLC113",
    "state": "FL",
    "geohash": "dj6n7",
}

LOCATION_AUTAUGA = {
    "zone_entity_id": "zone.cabin",
    "name": "Cabin",
    "latitude": 32.5361,
    "longitude": -86.6435,
    "same": SAME_AUTAUGA,
    "county": "ALC001",
    "state": "AL",
    "geohash": "djf3h",
}

# A zone outside any US county: lightning only, no SAME code.
LOCATION_OFFSHORE = {
    "zone_entity_id": "zone.boat",
    "name": "Boat",
    "latitude": 29.0,
    "longitude": -87.5,
    "same": None,
    "county": None,
    "state": None,
    "geohash": "dj1ub",
}
