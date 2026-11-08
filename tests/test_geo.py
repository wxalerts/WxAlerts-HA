"""Geo helpers: UGC to SAME, SAME normalisation, GeoJSON centroids.

These have no Home Assistant in them and decide which county trees get
subscribed, so they are tested against known-good values.
"""

from __future__ import annotations

import pytest

from custom_components.wxalerts.geo import (
    geometry_centroid,
    normalize_same,
    same_from_county_url,
    ugc_county_to_same,
)


# normalize_same
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("012113", "012113"),
        (12113, "012113"),  # a number loses the leading zero every code has
        (" 001001 ", "001001"),
        (None, None),
        (True, None),
        ("", None),
        ("FLC113", None),
        ("1234567", None),
    ],
)
def test_normalize_same(raw, expected):
    assert normalize_same(raw) == expected


# ugc_county_to_same
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
        "FLZ206",  # a forecast zone, not a county, no SAME code
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


# same_from_county_url
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


# geometry_centroid
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
