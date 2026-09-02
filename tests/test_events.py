"""The hazard catalogue: resolution, coverage and internal consistency.

The catalogue is a lookup table, so the tests worth writing are the ones a
table gets wrong — a hazard resolving to the wrong entry, a name the two feed
paths spell differently landing in two places, and the table drifting out of
step with what NWS actually publishes.
"""

from __future__ import annotations

import json
import urllib.request

import pytest
import pytest_socket

from custom_components.wxalerts.events import (
    BY_KEY,
    BY_VTEC,
    DEFAULT_EVENT_KEYS,
    EVENT_TYPES,
    SELECTABLE_EVENTS,
    event_key_for,
    event_name_for,
    event_type_for,
)

from .const import HEAT_ALERT, SEVERE_ALERT, TORNADO_ALERT

# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_the_captured_payloads_resolve():
    assert event_key_for(TORNADO_ALERT) == "tornado_warning"
    assert event_key_for(SEVERE_ALERT) == "severe_thunderstorm_warning"
    assert event_key_for(HEAT_ALERT) == "heat_advisory"


def test_significance_separates_the_watch_from_the_warning():
    """The entire point of the rework: TO.A and TO.W are not one hazard."""
    watch = {**TORNADO_ALERT, "significance": "A", "event": "Tornado Watch"}
    assert event_key_for(TORNADO_ALERT) != event_key_for(watch)
    assert event_key_for(watch) == "tornado_watch"


def test_the_vtec_pair_beats_a_disagreeing_name():
    """The two producers word hazards differently, so the pair has to win.

    The ingest builds its name mechanically from the VTEC table and calls FW.W
    a "Fire Weather Warning"; NWS calls the same product a Red Flag Warning.
    Matching the name first would put one hazard in two places.
    """
    from_vtec = {
        "phenomena": "FW",
        "significance": "W",
        "event": "Fire Weather Warning",
    }
    from_cap = {"phenomena": None, "significance": None, "event": "Red Flag Warning"}
    assert event_key_for(from_vtec) == event_key_for(from_cap) == "red_flag_warning"
    assert event_name_for(from_vtec) == "Red Flag Warning"


def test_a_cap_only_alert_resolves_by_name():
    """No VTEC at all — the ingest publishes both fields as null for these."""
    alert = {
        "phenomena": None,
        "significance": None,
        "event": "Special Weather Statement",
    }
    assert event_key_for(alert) == "special_weather_statement"


@pytest.mark.parametrize(
    ("phenomena", "significance", "expected"),
    [
        # Retired codes fold into their replacements, so an office still
        # issuing the old one is not invisible to an automation.
        ("WC", "Y", "cold_weather_advisory"),
        ("WC", "W", "extreme_cold_warning"),
        ("EC", "W", "extreme_cold_warning"),
        ("EH", "W", "extreme_heat_warning"),
        # Areal and forecast-point flooding are one hazard to a homeowner.
        ("FA", "W", "flood_warning"),
        ("FL", "W", "flood_warning"),
        # All four small-craft codes publish under one NWS name.
        ("SC", "Y", "small_craft_advisory"),
        ("RB", "Y", "small_craft_advisory"),
    ],
)
def test_codes_that_share_an_event_type(phenomena, significance, expected):
    assert event_key_for(
        {"phenomena": phenomena, "significance": significance, "event": "whatever"}
    ) == expected


def test_an_uncatalogued_hazard_still_gets_a_key():
    """A product this table has never heard of has to stay reportable — it
    just cannot be selected in the options flow."""
    alert = {"phenomena": "XX", "significance": "Q", "event": "Sharknado Warning"}
    assert event_type_for(alert) is None
    assert event_key_for(alert) == "sharknado_warning"
    assert event_name_for(alert) == "Sharknado Warning"


def test_an_alert_with_nothing_to_go_on():
    assert event_key_for({}) == "unknown"
    assert event_name_for({}) == "Unknown"


# ---------------------------------------------------------------------------
# Table consistency
# ---------------------------------------------------------------------------


def test_keys_are_unique():
    keys = [event.key for event in EVENT_TYPES]
    assert len(keys) == len(set(keys))


def test_every_selectable_event_is_in_the_key_index():
    assert {event.key for event in SELECTABLE_EVENTS} == set(BY_KEY)


def test_the_defaults_are_all_warnings():
    """The default set exists to cover hazards that can kill someone asleep.
    A watch or an advisory in it would be a mistake."""
    for key in DEFAULT_EVENT_KEYS:
        assert BY_KEY[key].name.endswith("Warning"), key


def test_every_phenomenon_the_feed_can_publish_is_mapped():
    """Guards the table against the ingest's VTEC list gaining a code.

    The list is duplicated here rather than imported: nws-ingest is a separate
    repo that is not a dependency, and a copy that drifts is exactly what this
    test is meant to catch — it fails when the ingest adds a phenomenon, which
    is when someone has to decide what event type it belongs to.
    """
    published = {
        "AF", "AS", "BH", "BW", "BZ", "CF", "DS", "DU", "EC", "EH", "EW",
        "FA", "FF", "FG", "FL", "FR", "FW", "FZ", "GL", "HF", "HT", "HU",
        "HW", "HY", "HZ", "IS", "LE", "LO", "LS", "LW", "MA", "MF", "MH",
        "MS", "RB", "RP", "SC", "SE", "SI", "SM", "SQ", "SR", "SS", "SU",
        "SV", "SW", "TO", "TR", "TS", "TY", "UP", "WC", "WI", "WS", "WW",
        "ZF", "ZR", "ZY",
    }
    assert published - {phenomena for phenomena, _ in BY_VTEC} == set()


@pytest.fixture
def allow_the_internet():
    """The HA harness pins every socket to 127.0.0.1, and the ``socket_enabled``
    fixture does not lift the host restriction — only this does."""
    pytest_socket._remove_restrictions()
    yield


@pytest.mark.live
def test_the_catalogue_covers_every_type_nws_publishes(allow_the_internet):
    """Against api.weather.gov, so the table cannot silently fall behind.

    Marked live because it is a network call: run with ``pytest -m live``.
    A name NWS adds and this misses is not broken — it resolves to a slug and
    still reports — but it cannot be picked in the options flow, which is the
    whole feature.
    """
    request = urllib.request.Request(
        "https://api.weather.gov/alerts/types",
        headers={"User-Agent": "WxAlerts-HA (github.com/wxalerts/WxAlerts-HA)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        published = set(json.load(response)["eventTypes"])

    catalogued = {event.name for event in EVENT_TYPES}
    assert published - catalogued == set()


def test_the_test_product_resolves_under_either_spelling():
    """NWS's type list says "Test"; the products on the feed say "Test
    Message". Caught by resolving every live hazard against the catalogue —
    the type list alone would have missed it."""
    assert event_key_for({"event": "Test"}) == "test"
    assert event_key_for({"event": "Test Message"}) == "test"
