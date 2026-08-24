"""Geo helpers: geohash, UGC -> SAME, GeoJSON centroids.

These are the pieces with no Home Assistant in them and the ones that decide
which topics get subscribed, so they are tested against known-good values
rather than against themselves.
"""

from __future__ import annotations

import pytest

from custom_components.wxalerts.geo import (
    cells_covering,
    geohash_bounds,
    geohash_encode,
    haversine_miles,
    plan_coverage,
    geometry_centroid,
    glm_topic_for_geohash,
    same_from_county_url,
    ugc_county_to_same,
)


# --- geohash_encode -------------------------------------------------------


@pytest.mark.parametrize(
    ("lat", "lon", "precision", "expected"),
    [
        # Santa Rosa County FL — the worked example in the feed docs.
        (30.6435, -87.0545, 5, "dj6n7"),
        # The canonical geohash test point (Jutland).
        (57.64911, 10.40744, 5, "u4pru"),
        (57.64911, 10.40744, 11, "u4pruydqqvj"),
        # Null Island.
        (0.0, 0.0, 5, "s0000"),
        # Southern and western hemispheres.
        (-33.8688, 151.2093, 5, "r3gx2"),
        (51.5074, -0.1278, 5, "gcpvj"),
    ],
)
def test_geohash_encode_known_points(lat, lon, precision, expected):
    assert geohash_encode(lat, lon, precision) == expected


def test_geohash_prefix_is_a_valid_coarser_box():
    """Every prefix must itself be the geohash at that precision.

    The integration subscribes with ``geohash[:precision]``, so if this were
    not true the lightning box would be somewhere else entirely.
    """
    full = geohash_encode(30.6435, -87.0545, 9)
    for precision in range(1, 10):
        assert full[:precision] == geohash_encode(30.6435, -87.0545, precision)


def test_geohash_encode_precision_length():
    assert len(geohash_encode(30.6435, -87.0545, 1)) == 1
    assert len(geohash_encode(30.6435, -87.0545, 12)) == 12


# --- glm_topic_for_geohash ------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("d", "wxalerts/glm/v1/d/#"),
        ("dj", "wxalerts/glm/v1/d/j/#"),
        ("dj6", "wxalerts/glm/v1/d/j/6/#"),
        ("dj6n", "wxalerts/glm/v1/d/j/6/n/#"),
        # A full leaf is an exact topic: the tree is exactly five deep.
        ("dj6n7", "wxalerts/glm/v1/d/j/6/n/7"),
        # Anything longer is truncated to the leaf, not turned into a
        # six-level topic that would match nothing.
        ("dj6n7xyz", "wxalerts/glm/v1/d/j/6/n/7"),
    ],
)
def test_glm_topic_for_geohash(prefix, expected):
    assert glm_topic_for_geohash(prefix) == expected


# --- ugc_county_to_same ---------------------------------------------------


@pytest.mark.parametrize(
    ("ugc", "expected"),
    [
        ("FLC113", "012113"),  # Santa Rosa County FL
        ("ALC003", "001003"),  # Baldwin County AL
        ("CAC037", "006037"),  # Los Angeles County CA
        ("PRC127", "072127"),  # a territory, which has a FIPS code too
        ("flc113", "012113"),  # case-insensitive
        (" FLC113 ", "012113"),  # tolerant of whitespace
    ],
)
def test_ugc_county_to_same(ugc, expected):
    assert ugc_county_to_same(ugc) == expected


@pytest.mark.parametrize(
    "ugc",
    [
        "FLZ206",  # a forecast zone, not a county — no SAME code
        "AMZ650",  # marine zone
        "ANZ450",  # offshore zone
        "ZZC001",  # state that does not exist
        "FLC11",  # too few digits
        "FLC1130",  # too many digits
        "",
        "garbage",
    ],
)
def test_ugc_county_to_same_rejects_non_counties(ugc):
    assert ugc_county_to_same(ugc) is None


# --- same_from_county_url -------------------------------------------------


def test_same_from_county_url():
    url = "https://api.weather.gov/zones/county/FLC113"
    assert same_from_county_url(url) == "012113"


def test_same_from_county_url_tolerates_trailing_slash():
    url = "https://api.weather.gov/zones/county/FLC113/"
    assert same_from_county_url(url) == "012113"


def test_same_from_forecast_zone_url_is_none():
    """A zone URL is what a marine or offshore point resolves to."""
    url = "https://api.weather.gov/zones/forecast/FLZ206"
    assert same_from_county_url(url) is None


# --- geometry_centroid ----------------------------------------------------


def test_centroid_of_polygon():
    """A closed ring repeats its first vertex; the average is still centred."""
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [[-87.0, 30.0], [-86.0, 30.0], [-86.0, 31.0], [-87.0, 31.0], [-87.0, 30.0]]
        ],
    }
    lat, lon = geometry_centroid(geometry)
    assert lat == pytest.approx(30.4, abs=0.001)
    assert lon == pytest.approx(-86.6, abs=0.001)


def test_centroid_of_multipolygon_uses_every_exterior_ring():
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]]],
            [[[10.0, 10.0], [12.0, 10.0], [12.0, 12.0], [10.0, 12.0]]],
        ],
    }
    lat, lon = geometry_centroid(geometry)
    assert lat == pytest.approx(6.0)
    assert lon == pytest.approx(6.0)


def test_centroid_of_point_is_the_point():
    """GeoJSON is lon/lat; the centroid is returned lat/lon."""
    assert geometry_centroid({"type": "Point", "coordinates": [-87.05, 30.64]}) == (
        30.64,
        -87.05,
    )


def test_centroid_ignores_interior_rings():
    """A hole must not drag the marker off the polygon it belongs to."""
    solid = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0], [0.0, 0.0]]],
    }
    with_hole = {
        "type": "Polygon",
        "coordinates": [
            solid["coordinates"][0],
            [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0], [1.0, 1.0]],
        ],
    }
    assert geometry_centroid(with_hole) == geometry_centroid(solid)


@pytest.mark.parametrize(
    "geometry",
    [
        None,
        {},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon"},
        {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]},
        {"type": "GeometryCollection", "coordinates": [[0.0, 0.0]]},
    ],
)
def test_centroid_of_unusable_geometry_is_none(geometry):
    """``geometry_source: none`` alerts arrive with no geometry at all."""
    assert geometry_centroid(geometry) is None


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------


def test_a_degree_of_latitude_is_69_miles():
    assert haversine_miles(30.0, -87.0, 31.0, -87.0) == pytest.approx(69.05, abs=0.1)


def test_a_degree_of_longitude_shrinks_with_latitude():
    """The reason a flat lat/lon difference cannot be used: at 30 degrees north
    a degree of longitude is 14% shorter than a degree of latitude."""
    at_equator = haversine_miles(0.0, 0.0, 0.0, 1.0)
    at_florida = haversine_miles(30.64, -87.0, 30.64, -86.0)

    assert at_florida == pytest.approx(at_equator * 0.86, rel=0.01)


def test_distance_to_the_same_point_is_zero():
    assert haversine_miles(30.64, -87.05, 30.64, -87.05) == 0.0


# ---------------------------------------------------------------------------
# Cell geometry
# ---------------------------------------------------------------------------


def test_geohash_bounds_contain_the_point_that_produced_them():
    lat, lon = 30.643444, -87.054500
    lat_lo, lat_hi, lon_lo, lon_hi = geohash_bounds(geohash_encode(lat, lon, 5))

    assert lat_lo <= lat <= lat_hi
    assert lon_lo <= lon <= lon_hi


def test_a_cell_is_about_twice_as_wide_as_it_is_tall():
    """Which is why the covering search walks a different number of steps per
    axis instead of using a square ring of neighbours."""
    lat_lo, lat_hi, lon_lo, lon_hi = geohash_bounds("dj6n")

    assert (lon_hi - lon_lo) == pytest.approx(2 * (lat_hi - lat_lo))


def test_the_containing_cell_is_not_centred_on_the_point():
    """The bug this whole module exists to fix. The grid is aligned to the
    globe, so a location can sit hard against one edge of its own cell."""
    lat, lon = 30.643444, -87.054500
    lat_lo, lat_hi, _lon_lo, _lon_hi = geohash_bounds(geohash_encode(lat, lon, 4))

    to_south = haversine_miles(lat, lon, lat_lo, lon)
    to_north = haversine_miles(lat, lon, lat_hi, lon)

    assert to_south < 5, "this station really is that close to its cell's edge"
    assert to_north > 2 * to_south, "and nowhere near the middle"


def test_covering_a_radius_takes_more_than_the_containing_cell():
    cells = cells_covering(30.643444, -87.054500, 10, precision=4)

    assert geohash_encode(30.643444, -87.054500, 4) in cells
    assert len(cells) > 1


def test_covering_cells_come_back_nearest_first():
    """So trimming to a subscription budget drops the least useful ones."""
    lat, lon = 30.643444, -87.054500
    cells = cells_covering(lat, lon, 30, precision=4)

    assert cells[0] == geohash_encode(lat, lon, 4)


def test_the_covering_set_leaves_no_gap_inside_the_radius():
    """Property check: every point inside the circle must fall in a subscribed
    cell, or the sensor is silently blind in that direction."""
    import math

    lat, lon = 30.643444, -87.054500
    radius = 30.0
    precision, cells = plan_coverage(lat, lon, radius, budget=12)
    covered = set(cells)

    for bearing in range(0, 360, 3):
        for fraction in (0.25, 0.5, 0.75, 1.0):
            angular = (radius * fraction) / 3958.7613
            brg = math.radians(bearing)
            phi = math.asin(
                math.sin(math.radians(lat)) * math.cos(angular)
                + math.cos(math.radians(lat)) * math.sin(angular) * math.cos(brg)
            )
            lam = math.radians(lon) + math.atan2(
                math.sin(brg) * math.sin(angular) * math.cos(math.radians(lat)),
                math.cos(angular) - math.sin(math.radians(lat)) * math.sin(phi),
            )
            cell = geohash_encode(math.degrees(phi), math.degrees(lam), precision)
            assert cell in covered, f"blind at bearing {bearing}"


def test_corners_the_circle_never_reaches_are_not_subscribed():
    """A square grid big enough to hold a circle has corners outside it, and
    each one would be a wasted subscription."""
    lat, lon = 30.643444, -87.054500
    cells = cells_covering(lat, lon, 30, precision=3)

    # A 3x3 grid would be nine; only the cells the circle actually touches
    # should survive.
    assert len(cells) < 9


def test_plan_coverage_prefers_finer_cells_when_they_fit():
    """Finer cells mean less irrelevant traffic, so spend the budget on them."""
    tight, _ = plan_coverage(30.643444, -87.054500, 5, budget=12)
    wide, _ = plan_coverage(30.643444, -87.054500, 50, budget=12)

    assert tight > wide


def test_plan_coverage_respects_the_budget():
    for budget in (1, 2, 4, 8, 16):
        _precision, cells = plan_coverage(30.643444, -87.054500, 30, budget=budget)
        assert len(cells) <= budget


def test_a_radius_of_zero_covers_nothing():
    assert cells_covering(30.6, -87.0, 0, precision=4) == []
