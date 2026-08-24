"""Small, dependency-free geo helpers: geohash encoding and neighbour
arithmetic, great-circle distance, UGC -> SAME, and GeoJSON centroids.
"""

from __future__ import annotations

import math
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


MILES_PER_KM = 0.621371
EARTH_RADIUS_MI = 3958.7613


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles.

    GLM geolocation is good to a few kilometres, so a spherical earth is far
    below the noise floor here; the point of using haversine at all is that a
    flat lat/lon difference is wrong by ~14% at this latitude.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MI * math.asin(min(1.0, math.sqrt(a)))


def geohash_bounds(geohash: str) -> tuple[float, float, float, float]:
    """(lat_lo, lat_hi, lon_lo, lon_hi) for a geohash cell."""
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    even = True
    for char in geohash:
        value = _GEOHASH_BASE32.index(char)
        for shift in range(4, -1, -1):
            bit = (value >> shift) & 1
            if even:
                mid = (lon_lo + lon_hi) / 2
                if bit:
                    lon_lo = mid
                else:
                    lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if bit:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def _cell_distance_mi(
    lat: float, lon: float, bounds: tuple[float, float, float, float]
) -> float:
    """Distance from a point to the nearest part of a geohash cell.

    Zero when the point is inside. Clamping the point into the cell's range and
    measuring to that is the standard point-to-rectangle test; on a sphere it is
    an approximation, but the error is far below one flash's geolocation
    accuracy at these cell sizes.
    """
    lat_lo, lat_hi, lon_lo, lon_hi = bounds
    near_lat = min(max(lat, lat_lo), lat_hi)
    near_lon = min(max(lon, lon_lo), lon_hi)
    if near_lat == lat and near_lon == lon:
        return 0.0
    return haversine_miles(lat, lon, near_lat, near_lon)


def cells_covering(
    lat: float, lon: float, radius_mi: float, precision: int
) -> list[str]:
    """Every geohash cell of ``precision`` that the radius actually reaches.

    Two things this does that a naive centre-cell subscription does not.

    The grid is aligned to the globe, not to the point, so the cell containing a
    location is almost never centred on it — a house 6 km from its cell's
    southern edge is blind to everything past that edge. So the search walks
    outward in both axes until it has covered the radius.

    It walks a different distance per axis, because geohash cells are about
    twice as wide as they are tall, and a degree of longitude shrinks with
    latitude. Then it keeps only the cells the circle genuinely intersects: a
    square grid big enough to contain a circle holds a lot of corners the circle
    never touches, and each one is a wasted subscription.

    Cells come back nearest-first, so a caller trimming to a subscription budget
    drops the least useful ones.
    """
    if precision <= 0 or radius_mi <= 0:
        return []
    centre = geohash_encode(lat, lon, precision)
    lat_lo, lat_hi, lon_lo, lon_hi = geohash_bounds(centre)
    lat_step = lat_hi - lat_lo
    lon_step = lon_hi - lon_lo
    if lat_step <= 0 or lon_step <= 0:
        return [centre]

    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    rows = int(math.ceil((radius_mi / 69.0537) / lat_step))
    cols = int(math.ceil((radius_mi / (69.0537 * cos_lat)) / lon_step))

    lat_mid = (lat_lo + lat_hi) / 2
    lon_mid = (lon_lo + lon_hi) / 2

    found: dict[str, float] = {}
    for row in range(-rows, rows + 1):
        for col in range(-cols, cols + 1):
            cell_lat = lat_mid + row * lat_step
            if not -90.0 <= cell_lat <= 90.0:
                continue
            # Wrap rather than skip, so a set spanning the antimeridian stays
            # contiguous instead of losing half of itself.
            cell_lon = ((lon_mid + col * lon_step + 180.0) % 360.0) - 180.0
            cell = geohash_encode(cell_lat, cell_lon, precision)
            if cell in found:
                continue
            distance = _cell_distance_mi(lat, lon, geohash_bounds(cell))
            if distance <= radius_mi:
                found[cell] = distance
    if not found:
        return [centre]
    return sorted(found, key=lambda cell: found[cell])


def plan_coverage(
    lat: float, lon: float, radius_mi: float, budget: int, max_precision: int = 5
) -> tuple[int, list[str]]:
    """Pick the finest geohash precision whose cell set fits ``budget``.

    Finer cells mean less irrelevant traffic but more subscriptions, and the
    broker caps subscriptions per client. Rather than making the user reason
    about that trade-off through a raw precision number, take the radius they
    care about and spend the budget on the tightest fit available.

    Falls back to the coarsest level if even that overruns, so the caller always
    gets something to subscribe to and can trim and log.
    """
    for precision in range(max_precision, 0, -1):
        cells = cells_covering(lat, lon, radius_mi, precision)
        if cells and len(cells) <= budget:
            return precision, cells
    return 1, cells_covering(lat, lon, radius_mi, 1)


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
