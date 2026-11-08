"""The coordinator: subscriptions, alert state, events and storm cell episodes.

No entities here; these tests drive ``_handle_message`` directly and assert on
coordinator state and the Home Assistant event bus.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from pytest_homeassistant_custom_component.common import async_capture_events

from homeassistant.const import CONF_PASSWORD

from custom_components.wxalerts.const import (
    API_INSTANCES_URL,
    DOMAIN,
    OAUTH_TOKEN_URL,
    EVENT_ALERT,
    EVENT_HAZARD,
    EVENT_STORM_CELL,
    MAX_SUBSCRIPTIONS,
)
from custom_components.wxalerts.coordinator import (
    WxAlertsCoordinator,
    parse_places,
    threat_for_rung,
)

from .conftest import feed, make_entry, make_token, start
from .const import (
    ACCESS_TOKEN,
    registration,
    CACHED_PLACES,
    HEAT_ALERT,
    PLACE_BOAT,
    PLACE_HOME,
    PREFIX,
    SAME_AUTAUGA,
    SAME_SANTA_ROSA,
    SEVERE_ALERT,
    TOMBSTONE,
    TOPIC_HEAT,
    TOPIC_PLACES,
    TOPIC_SEVERE,
    TOPIC_TORNADO,
    TORNADO_ALERT,
    USERNAME,
    encode,
    storm,
    storm_topic,
)


def make_coordinator(hass, places=None) -> WxAlertsCoordinator:
    entry = make_entry(places=places)
    entry.add_to_hass(hass)
    coordinator = WxAlertsCoordinator(hass, entry)
    coordinator.connected = True
    return coordinator


async def deliver(hass, coordinator, topic, payload):
    await coordinator._handle_message(topic, payload)
    await hass.async_block_till_done()


# ===========================================================================
# Subscriptions
# ===========================================================================
async def test_subscribes_to_the_account_tree_and_each_county(hass):
    coordinator = make_coordinator(hass)

    assert coordinator._build_subscriptions() == [
        (f"wxalerts/u/{USERNAME}/#", 1),
        (f"wxalerts/nws/v1/same/{SAME_SANTA_ROSA}/#", 1),
        (f"wxalerts/nws/v1/same/{SAME_AUTAUGA}/#", 1),
    ]


async def test_two_places_in_one_county_subscribe_once(hass):
    work = {**CACHED_PLACES[0], "id": "9", "name": "Work"}
    coordinator = make_coordinator(hass, places=[CACHED_PLACES[0], work])
    assert len(coordinator._build_subscriptions()) == 2


async def test_a_place_without_a_county_subscribes_to_nothing_extra(hass):
    coordinator = make_coordinator(hass, places=[{"id": "1", "name": "Boat"}])
    assert coordinator._build_subscriptions() == [(f"wxalerts/u/{USERNAME}/#", 1)]


async def test_the_subscription_cap_keeps_the_account_tree(hass):
    places = [
        {"id": str(n), "name": f"P{n}", "same": f"{n:06d}"} for n in range(1, 30)
    ]
    coordinator = make_coordinator(hass, places=places)
    subs = coordinator._build_subscriptions()
    assert len(subs) == MAX_SUBSCRIPTIONS
    assert subs[0] == (f"wxalerts/u/{USERNAME}/#", 1)


async def test_start_derives_a_unique_client_id_and_uses_the_login(
    hass, mock_feed_client
):
    coordinator = make_coordinator(hass)
    await coordinator.async_start()
    config = mock_feed_client.call_args[0][0]
    assert config.client_id.startswith("wxha-")
    assert config.username == USERNAME
    assert config.password
    await coordinator.async_stop()
    mock_feed_client.return_value.stop.assert_awaited_once()


async def test_stop_without_a_client_does_not_raise(hass):
    await make_coordinator(hass).async_stop()


# ===========================================================================
# Places payload
# ===========================================================================
def test_places_are_parsed_and_normalised():
    places = parse_places(
        [
            {"id": 7, "name": "Home", "lat": "30.5", "lon": -87, "same": 12113},
            {"id": "x", "name": "Boat"},
            {"name": "no id"},
            "garbage",
        ]
    )
    assert list(places) == ["7", "x"]
    assert places["7"].same == "012113"
    assert places["7"].latitude == 30.5
    assert places["x"].same is None


def test_a_non_list_places_payload_is_rejected():
    assert parse_places({"id": 1}) is None
    assert parse_places(None) is None


async def test_a_place_without_a_county_is_looked_up(hass, no_same_lookup):
    coordinator = make_coordinator(hass, places=[])
    await deliver(hass, coordinator, TOPIC_PLACES, encode([PLACE_BOAT]))
    no_same_lookup.assert_awaited_once()
    # Once per run, not on every republish.
    await deliver(hass, coordinator, TOPIC_PLACES, encode([PLACE_BOAT]))
    no_same_lookup.assert_awaited_once()


async def test_the_county_lookup_fills_in_a_missing_same(hass, no_same_lookup, aioclient_mock):
    aioclient_mock.get(
        "https://api.weather.gov/points/29.0,-87.5",
        json={"properties": {"county": "https://api.weather.gov/zones/county/FLC113"}},
    )
    coordinator = make_coordinator(hass, places=[])
    coordinator._client = MagicMock(set_subscriptions=AsyncMock())
    coordinator._apply_places(parse_places([PLACE_BOAT]))
    await no_same_lookup.original(coordinator, coordinator.places["103"])
    await hass.async_block_till_done()

    assert coordinator.places["103"].same == SAME_SANTA_ROSA
    assert (f"wxalerts/nws/v1/same/{SAME_SANTA_ROSA}/#", 1) in (
        coordinator._client.set_subscriptions.await_args[0][0]
    )


async def test_a_failed_county_lookup_is_harmless(hass, no_same_lookup, aioclient_mock):
    aioclient_mock.get("https://api.weather.gov/points/29.0,-87.5", status=500)
    coordinator = make_coordinator(hass, places=[])
    coordinator._apply_places(parse_places([PLACE_BOAT]))
    await no_same_lookup.original(coordinator, coordinator.places["103"])
    assert coordinator.places["103"].same is None


async def test_dropping_a_county_forgets_its_alerts(hass):
    coordinator = make_coordinator(hass)
    await deliver(hass, coordinator, TOPIC_HEAT, encode(HEAT_ALERT))
    assert coordinator.alerts_for(SAME_AUTAUGA)

    await deliver(hass, coordinator, TOPIC_PLACES, encode([PLACE_HOME]))
    assert coordinator.alerts_for(SAME_AUTAUGA) == []


# ===========================================================================
# Alerts
# ===========================================================================
async def test_alert_message_is_stored_under_its_county(hass):
    coordinator = make_coordinator(hass)
    await deliver(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    assert coordinator.alerts_for(SAME_SANTA_ROSA)[0]["vtec"] == TORNADO_ALERT["vtec"]


@pytest.mark.parametrize(
    "topic",
    [
        "wxalerts/nws/v1/same/012113",
        "wxalerts/u/someone-else/places",
        f"{PREFIX}storms/place/101",
        f"{PREFIX}unknown",
        "something/else",
    ],
)
async def test_unrelated_topics_are_ignored(hass, topic):
    coordinator = make_coordinator(hass)
    await deliver(hass, coordinator, topic, encode([PLACE_BOAT]))
    assert coordinator._alerts == {}
    assert coordinator._storms == {}
    assert "103" not in coordinator.places


async def test_undecodable_payload_does_not_raise_or_store(hass):
    coordinator = make_coordinator(hass)
    await deliver(hass, coordinator, TOPIC_TORNADO, b"{not json")
    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []


async def test_tombstone_removes_only_the_hazard_it_names(hass):
    coordinator = make_coordinator(hass)
    await deliver(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await deliver(hass, coordinator, TOPIC_SEVERE, encode(SEVERE_ALERT))
    await deliver(hass, coordinator, TOPIC_TORNADO, TOMBSTONE)
    assert [a["vtec"] for a in coordinator.alerts_for(SAME_SANTA_ROSA)] == [
        SEVERE_ALERT["vtec"]
    ]


@pytest.mark.parametrize("status", ["expired", "cancelled"])
async def test_ended_statuses_are_not_reported_as_active(hass, status):
    coordinator = make_coordinator(hass)
    await deliver(
        hass, coordinator, TOPIC_TORNADO, encode({**TORNADO_ALERT, "status": status})
    )
    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []


async def test_alerts_rank_by_significance_within_a_severity(hass):
    coordinator = make_coordinator(hass)
    watch = {**TORNADO_ALERT, "significance": "A", "severity": "Severe", "event": "Tornado Watch"}
    await deliver(hass, coordinator, f"{TOPIC_TORNADO}9", encode(watch))
    await deliver(hass, coordinator, TOPIC_SEVERE, encode(SEVERE_ALERT))
    assert coordinator.alerts_for(SAME_SANTA_ROSA)[0]["event"] == (
        "Severe Thunderstorm Warning"
    )


async def test_unknown_severity_sorts_last_and_reports_none(hass):
    coordinator = make_coordinator(hass)
    await deliver(
        hass, coordinator, TOPIC_TORNADO, encode({**TORNADO_ALERT, "severity": "Odd"})
    )
    assert coordinator.highest_severity(SAME_SANTA_ROSA) == "None"


async def test_new_hazard_fires_one_alert_event_per_place(hass):
    work = {**CACHED_PLACES[0], "id": "9", "name": "Work"}
    coordinator = make_coordinator(hass, places=[CACHED_PLACES[0], work])
    events = async_capture_events(hass, EVENT_ALERT)

    await deliver(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await deliver(
        hass, coordinator, TOPIC_TORNADO, encode({**TORNADO_ALERT, "action": "CON"})
    )

    assert sorted(e.data["place_name"] for e in events) == ["Home", "Work"]
    assert events[0].data["event"] == "Tornado Warning"
    assert events[0].data["event_type"] == "tornado_warning"
    assert events[0].data["same"] == SAME_SANTA_ROSA


async def test_the_retained_burst_fires_no_events(hass):
    coordinator = make_coordinator(hass)
    coordinator._handle_connection(True)
    alerts = async_capture_events(hass, EVENT_ALERT)
    hazards = async_capture_events(hass, EVENT_HAZARD)

    await deliver(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    assert alerts == [] and hazards == []

    # And it is not announced later as new, either.
    coordinator._priming_until = 0.0
    await deliver(
        hass, coordinator, TOPIC_TORNADO, encode({**TORNADO_ALERT, "action": "CON"})
    )
    assert alerts == []


async def test_a_hazard_reissued_after_ending_fires_again(hass):
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_ALERT)
    await deliver(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await deliver(hass, coordinator, TOPIC_TORNADO, TOMBSTONE)
    await deliver(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    assert len(events) == 2


async def test_hazard_begin_and_end_carry_the_place(hass):
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_HAZARD)

    await deliver(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await deliver(hass, coordinator, TOPIC_TORNADO, TOMBSTONE)

    assert [(e.data["action"], e.data["place_id"]) for e in events] == [
        ("begin", "101"),
        ("end", "101"),
    ]
    assert events[0].data["event_type"] == "tornado_warning"
    assert events[0].data["place_name"] == "Home"


async def test_a_second_warning_of_a_live_type_is_not_a_new_hazard(hass):
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_HAZARD)
    await deliver(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await deliver(hass, coordinator, f"{TOPIC_TORNADO}7", encode(TORNADO_ALERT))
    await deliver(hass, coordinator, TOPIC_TORNADO, TOMBSTONE)
    assert [e.data["action"] for e in events] == ["begin"]
    await deliver(hass, coordinator, f"{TOPIC_TORNADO}7", TOMBSTONE)
    assert [e.data["action"] for e in events] == ["begin", "end"]


async def test_an_expiry_during_priming_is_still_announced(hass):
    coordinator = make_coordinator(hass)
    await deliver(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    coordinator._priming_until = time.monotonic() + 60
    events = async_capture_events(hass, EVENT_HAZARD)
    await deliver(hass, coordinator, TOPIC_TORNADO, TOMBSTONE)
    assert [e.data["action"] for e in events] == ["end"]


# ===========================================================================
# Storm cells
# ===========================================================================
async def test_a_storm_cell_open_fires_the_event(hass):
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_STORM_CELL)

    await deliver(hass, coordinator, storm_topic(101), encode(storm()))

    assert len(events) == 1
    data = events[0].data
    assert data["episode"] == "open"
    assert data["place_id"] == "101"
    assert data["place_name"] == "Home"
    assert data["storm_id"] == "ps-449120"
    assert data["rung"] == 40
    assert data["rung_label"] == "Thunderstorm Risk"
    assert data["threat"] == "thunderstorm_risk"
    assert data["eta_min"] == pytest.approx(18.0, abs=0.1)
    for field in ("prob_severe", "prob_tor", "prob_hail", "prob_wind", "speed_kmh",
                  "bearing_deg", "distance_km", "issued_at", "updated_at"):
        assert field in data


async def test_an_update_does_not_fire(hass):
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_STORM_CELL)
    await deliver(hass, coordinator, storm_topic(101), encode(storm()))
    await deliver(
        hass, coordinator, storm_topic(101), encode(storm(episode="update", eta_min=12))
    )
    assert [e.data["episode"] for e in events] == ["open"]
    assert coordinator.storm_eta(coordinator.primary_storm("101")) == pytest.approx(12, abs=0.1)


async def test_an_escalation_fires_and_a_repeat_does_not(hass):
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_STORM_CELL)
    await deliver(hass, coordinator, storm_topic(101), encode(storm()))
    escalate = storm(episode="escalate", rung=70, rung_label="Tornado Risk")
    await deliver(hass, coordinator, storm_topic(101), encode(escalate))
    await deliver(hass, coordinator, storm_topic(101), encode(escalate))
    await deliver(
        hass,
        coordinator,
        storm_topic(101),
        encode(storm(episode="escalate", rung=80, rung_label="Tornado Detected")),
    )
    assert [(e.data["episode"], e.data["rung"]) for e in events] == [
        ("open", 40),
        ("escalate", 70),
        ("escalate", 80),
    ]


async def test_a_retained_storm_on_reconnect_is_not_announced(hass):
    coordinator = make_coordinator(hass)
    coordinator._handle_connection(True)
    events = async_capture_events(hass, EVENT_STORM_CELL)
    await deliver(hass, coordinator, storm_topic(101), encode(storm()))
    assert events == []
    assert coordinator.primary_storm("101") is not None

    # The replay after the window carries the same episode and rung.
    coordinator._priming_until = 0.0
    await deliver(hass, coordinator, storm_topic(101), encode(storm()))
    assert events == []


async def test_a_cleared_storm_that_returns_fires_again(hass):
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_STORM_CELL)
    await deliver(hass, coordinator, storm_topic(101), encode(storm()))
    await deliver(hass, coordinator, storm_topic(101), TOMBSTONE)
    assert coordinator.primary_storm("101") is None
    await deliver(hass, coordinator, storm_topic(101), encode(storm()))
    assert len(events) == 2


async def test_storms_are_kept_per_place(hass):
    coordinator = make_coordinator(hass)
    await deliver(hass, coordinator, storm_topic(101), encode(storm()))
    await deliver(hass, coordinator, storm_topic(102), encode(storm(place_id=102)))
    await deliver(hass, coordinator, storm_topic(101), TOMBSTONE)
    assert coordinator.primary_storm("101") is None
    assert coordinator.primary_storm("102") is not None


async def test_an_undecodable_storm_is_ignored(hass):
    coordinator = make_coordinator(hass)
    await deliver(hass, coordinator, storm_topic(101), b"[nope")
    await deliver(hass, coordinator, storm_topic(101), encode([1, 2]))
    assert coordinator.primary_storm("101") is None


async def test_removing_a_place_forgets_its_storms(hass):
    coordinator = make_coordinator(hass)
    await deliver(hass, coordinator, storm_topic(102), encode(storm(place_id=102)))
    await deliver(hass, coordinator, TOPIC_PLACES, encode([PLACE_HOME]))
    assert coordinator.storms_for("102") == []


@pytest.mark.parametrize(
    ("rung", "expected"),
    [
        (None, "none"),
        (5, "none"),
        (40, "thunderstorm_risk"),
        (50, "thunderstorm_detected"),
        (70, "tornado_risk"),
        (75, "tornado_risk_high"),
        (85, "tornado_confirmed"),
        (99, "tornado_confirmed"),
        ("70", "tornado_risk"),
    ],
)
def test_threat_for_rung(rung, expected):
    assert threat_for_rung(rung) == expected


# ===========================================================================
# Connection and authentication
# ===========================================================================
def _flows(hass):
    return [
        flow["context"]["source"]
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    ]


async def _reject_login(hass, coordinator):
    coordinator._handle_auth_failed()
    await hass.async_block_till_done()


async def test_a_rejected_login_is_renewed_without_asking(
    hass, aioclient_mock, mock_feed_client, enable_custom_integrations
):
    """A password rotated on the website: register again with the stored
    token, reconnect with the new login, and never bother the person."""
    entry = make_entry()
    coordinator = await start(hass, entry)
    aioclient_mock.post(API_INSTANCES_URL, json=registration(password="renewed"))

    await _reject_login(hass, coordinator)

    assert entry.data[CONF_PASSWORD] == "renewed"
    assert _flows(hass) == []
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    # Reloaded onto the new login.
    assert entry.runtime_data is not coordinator
    assert mock_feed_client.call_args[0][0].password == "renewed"


async def test_an_expired_token_is_refreshed_before_registering(
    hass, aioclient_mock, mock_feed_client, enable_custom_integrations
):
    entry = make_entry(token=make_token(expires_in=-60))
    coordinator = await start(hass, entry)
    aioclient_mock.post(
        OAUTH_TOKEN_URL,
        json={"access_token": "refreshed", "refresh_token": "r2", "expires_in": 3600},
    )
    aioclient_mock.post(API_INSTANCES_URL, json=registration(password="renewed"))

    await _reject_login(hass, coordinator)

    assert entry.data["token"]["access_token"] == "refreshed"
    assert aioclient_mock.mock_calls[1][3]["Authorization"] == "Bearer refreshed"
    assert entry.data[CONF_PASSWORD] == "renewed"


async def test_a_refused_token_gets_one_forced_refresh(
    hass, aioclient_mock, mock_feed_client, enable_custom_integrations
):
    entry = make_entry()
    coordinator = await start(hass, entry)
    aioclient_mock.post(
        OAUTH_TOKEN_URL,
        json={"access_token": "refreshed", "refresh_token": "r2", "expires_in": 3600},
    )

    async def _register(method, url, data):
        from pytest_homeassistant_custom_component.test_util.aiohttp import (
            AiohttpClientMockResponse,
        )

        calls = [c for c in aioclient_mock.mock_calls if str(c[1]) == API_INSTANCES_URL]
        if len(calls) == 1:
            return AiohttpClientMockResponse(method, url, status=401, json={})
        return AiohttpClientMockResponse(method, url, json=registration(password="renewed"))

    aioclient_mock.post(API_INSTANCES_URL, side_effect=_register)

    await _reject_login(hass, coordinator)

    assert entry.data[CONF_PASSWORD] == "renewed"
    assert entry.data["token"]["access_token"] == "refreshed"
    assert _flows(hass) == []


@pytest.mark.parametrize(
    ("token_status", "api_status", "api_body"),
    [
        (400, None, None),  # refresh token revoked
        (None, 403, {"detail": {"code": "supporter_required"}}),
        (None, 409, {"detail": {"code": "instance_limit", "limit": 1, "used": 1}}),
    ],
)
async def test_when_renewal_fails_it_asks_to_sign_in(
    hass, aioclient_mock, mock_feed_client, enable_custom_integrations,
    token_status, api_status, api_body,
):
    expires = -60 if token_status else 3600
    entry = make_entry(token=make_token(expires_in=expires))
    coordinator = await start(hass, entry)
    if token_status:
        aioclient_mock.post(OAUTH_TOKEN_URL, status=token_status, json={"error": "invalid_grant"})
    else:
        aioclient_mock.post(API_INSTANCES_URL, status=api_status, json=api_body)

    await _reject_login(hass, coordinator)

    assert _flows(hass) == [SOURCE_REAUTH]


async def test_an_unreachable_api_retries_later_instead_of_asking(
    hass, freezer, aioclient_mock, mock_feed_client, enable_custom_integrations
):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = make_entry()
    coordinator = await start(hass, entry)
    aioclient_mock.post(API_INSTANCES_URL, status=503)

    await _reject_login(hass, coordinator)
    assert _flows(hass) == []
    assert aioclient_mock.call_count == 1

    aioclient_mock.clear_requests()
    aioclient_mock.post(API_INSTANCES_URL, json=registration(password="renewed"))
    freezer.tick(timedelta(seconds=301))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert entry.data[CONF_PASSWORD] == "renewed"


async def test_a_fresh_login_rejected_again_asks_instead_of_looping(
    hass, aioclient_mock, mock_feed_client, enable_custom_integrations
):
    entry = make_entry()
    await start(hass, entry)
    aioclient_mock.post(API_INSTANCES_URL, json=registration(password="renewed"))
    await _reject_login(hass, entry.runtime_data)
    assert _flows(hass) == []

    await _reject_login(hass, entry.runtime_data)

    assert _flows(hass) == [SOURCE_REAUTH]
    assert aioclient_mock.call_count == 1


async def test_connection_state_reaches_the_entities(hass, setup_integration):
    setup_integration._handle_connection(False)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.wxalerts_home_active_alert").state == (
        "unavailable"
    )
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))
