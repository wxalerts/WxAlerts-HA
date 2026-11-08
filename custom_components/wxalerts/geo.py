"""Small, dependency-free geo helpers: UGC to SAME, and GeoJSON centroids."""

from __future__ import annotations

import re

_UGC_COUNTY_RE = re.compile(r"^([A-Z]{2})C(\d{3})$")

# NWS point metadata, used only when a place arrives without a SAME code. One
# request per such place per start, never polled.
NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"

# State/territory postal abbreviation to FIPS code, for converting a county
# UGC ("FLC113") into a SAME code ("0" + state FIPS + county number).
STATE_FIPS: dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
    "AS": "60", "GU": "66", "MP": "69", "PR": "72", "VI": "78",
}


def normalize_same(raw: object) -> str | None:
    """A SAME code as the six-digit string the alert topics use.

    The places payload may carry it as a number, which drops the leading zero
    every SAME code has; padding it back is the difference between subscribing
    to a real county and to nothing at all.
    """
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text.isdigit() or len(text) > 6:
        return None
    return text.zfill(6)


def ugc_county_to_same(ugc: str) -> str | None:
    """Convert a county UGC like ``FLC113`` to its SAME code ``012113``.

    SAME is "0" + two-digit state FIPS + three-digit county number.
    Returns None for zone UGCs (``FLZ206``) and unknown states: marine
    and offshore zones have no SAME code at all.
    """
    match = _UGC_COUNTY_RE.match(ugc.strip().upper())
    if not match:
        return None
    state, county = match.groups()
    fips = STATE_FIPS.get(state)
    if fips is None:
        return None
    return f"0{fips}{county}"


def same_from_county_url(url: str) -> str | None:
    """Extract the SAME code from an api.weather.gov county-zone URL,
    e.g. ``https://api.weather.gov/zones/county/FLC113``.
    """
    ugc = url.rstrip("/").rsplit("/", 1)[-1]
    return ugc_county_to_same(ugc)


def geometry_centroid(geometry: dict | None) -> tuple[float, float] | None:
    """Return (lat, lon) centroid of a GeoJSON Polygon/MultiPolygon.

    A plain vertex average of the exterior rings, good enough to place a
    map marker; the polygon itself is exposed as an attribute for anyone
    who wants the real shape.
    """
    if not geometry:
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None
    if gtype == "Polygon":
        rings = [coords[0]]
    elif gtype == "MultiPolygon":
        rings = [poly[0] for poly in coords if poly]
    elif gtype == "Point":
        return (coords[1], coords[0])
    else:
        return None
    total_lat = total_lon = 0.0
    count = 0
    for ring in rings:
        for point in ring:
            total_lon += point[0]
            total_lat += point[1]
            count += 1
    if count == 0:
        return None
    return (total_lat / count, total_lon / count)
