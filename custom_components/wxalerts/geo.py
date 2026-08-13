"""Small, dependency-free geo helpers: geohash encoding, UGC -> SAME,
and GeoJSON centroids.
"""

from __future__ import annotations

import re

from .const import STATE_FIPS

_GEOHASH_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

_UGC_COUNTY_RE = re.compile(r"^([A-Z]{2})C(\d{3})$")


def geohash_encode(lat: float, lon: float, precision: int = 5) -> str:
    """Encode a lat/lon to a geohash of the given precision.

    The wxalerts GLM topic tree is one geohash character per level, so any
    prefix of this string is a valid subscription root.
    """
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    bits = []
    even = True
    while len(bits) < precision * 5:
        if even:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                bits.append(1)
                lon_range[0] = mid
            else:
                bits.append(0)
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                bits.append(1)
                lat_range[0] = mid
            else:
                bits.append(0)
                lat_range[1] = mid
        even = not even
    chars = []
    for i in range(0, len(bits), 5):
        value = 0
        for bit in bits[i : i + 5]:
            value = (value << 1) | bit
        chars.append(_GEOHASH_BASE32[value])
    return "".join(chars)


def glm_topic_for_geohash(prefix: str) -> str:
    """Build the GLM subscription topic for a geohash prefix.

    The tree is exactly five levels deep (one character each), so a
    shorter prefix ends in a wildcard: ``d/j/6/#`` covers every leaf
    under ``dj6``.
    """
    levels = list(prefix[:5])
    topic = "wxalerts/glm/v1/" + "/".join(levels)
    if len(levels) < 5:
        topic += "/#"
    return topic


def ugc_county_to_same(ugc: str) -> str | None:
    """Convert a county UGC like ``FLC113`` to its SAME code ``012113``.

    SAME is "0" + two-digit state FIPS + three-digit county number.
    Returns None for zone UGCs (``FLZ206``) and unknown states — marine
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

    A plain vertex average of the exterior rings — good enough to place a
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
