"""MQTT V2: the topic set the API advertises, the V2 alert message mapped onto
the entities, and the alerts in force read from the API because V2 does not
retain them. No test opens a socket: the feed and the public API are patched.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.wxalerts.alerts_v2 import (
    alert_key,
    from_api,
    merge_detail,
    parse_message,
    place_scope,
)
from custom_components.wxalerts.api import ApiUnavailable, broker_data, topics_data
from custom_components.wxalerts.const import (
    CONF_TOPICS_VERSION,
    CONF_V2_INSTANCE,
    CONF_V2_USER_ID,
    EVENT_ALERT,
    EVENT_HAZARD,
)

from .conftest import make_entry, start
from .const import (
    CACHED_PLACES,
    PLACE_BOAT,
    PLACE_CABIN,
    PLACE_HOME,
    PREFIX,
    TOPIC_PLACES,
    V2_INSTANCE,
    V2_PREFIX,
    V2_SEVERE_ID,
    V2_TOPIC_PLACES,
    V2_TOPIC_SEVERE,
    V2_TOPIC_STATUS,
    V2_TOPIC_TORNADO,
    V2_TORNADO_ID,
    V2_USER_ID,
    api_alert,
    encode,
    v2_alert,
)

V2_DATA = {
    CONF_TOPICS_VERSION: 2,
    CONF_V2_USER_ID: V2_USER_ID,
    CONF_V2_INSTANCE: V2_INSTANCE,
}
ACTIVE_ALERT = "binary_sensor.wxalerts_home_active_alert"
TORNADO_SENSOR = "binary_sensor.wxalerts_home_tornado_warning"
CABIN_ACTIVE = "binary_sensor.wxalerts_cabin_active_alert"
TOP = "sensor.wxalerts_home_top_alert"
COUNT = "sensor.wxalerts_home_active_alerts"


@pytest.fixture(autouse=True)
def _custom_integrations(enable_custom_integrations):
    """Entries here are set up through Home Assistant's loader."""


@pytest.fixture
def api():
    """The public API, stubbed: nothing in force, no detail."""
    with (
        patch("custom_components.wxalerts.api.async_alerts_at", new_callable=AsyncMock) as at,
        patch("custom_components.wxalerts.api.async_alert_page", new_callable=AsyncMock) as page,
    ):
        at.return_value = []
        page.return_value = None
        yield MagicMock(at=at, page=page)


async def settle(hass):
    await hass.async_block_till_done(wait_background_tasks=True)


async def deliver(hass, coordinator, topic, payload):
    await coordinator._handle_message(topic, payload)
    await settle(hass)


async def start_v2(hass, places=None):
    entry = make_entry(places=places, **V2_DATA)
    return await start(hass, entry)


def held(coordinator) -> dict:
    """Every alert the coordinator holds, by scope, leaving out empty scopes."""
    return {scope: county for scope, county in coordinator._alerts.items() if county}


def events(hass, name):
    seen: list[dict] = []
    hass.bus.async_listen(name, lambda event: seen.append(dict(event.data)))
    return seen


# ===========================================================================
# What the API advertises
# ===========================================================================
def test_v2_only_when_the_api_says_so_completely():
    good = {"topics_version": 2, "v2_user_id": 42, "v2_instance": "k7x2q9mfw3"}
    assert topics_data(good) == V2_DATA
    v1 = {CONF_TOPICS_VERSION: 1, CONF_V2_USER_ID: None, CONF_V2_INSTANCE: None}
    assert topics_data({}) == v1  # an API that has never heard of V2
    assert topics_data({"topics_version": 1}) == v1
    assert topics_data({**good, "v2_user_id": None}) == v1
    assert topics_data({**good, "v2_user_id": True}) == v1
    assert topics_data({**good, "v2_user_id": "42"}) == v1
    assert topics_data({**good, "v2_instance": None}) == v1


@pytest.mark.parametrize("instance", ["a/b", "a+", "#", "a#", "", "UPPER", "x" * 41])
def test_an_instance_key_that_could_widen_a_subscription_is_refused(instance):
    out = topics_data({"topics_version": 2, "v2_user_id": 42, "v2_instance": instance})
    assert out[CONF_TOPICS_VERSION] == 1


def test_the_registration_carries_the_topic_set_into_the_entry():
    out = broker_data(
        {"username": "ha-x", "password": "p", "topics_version": 2, "v2_user_id": 42,
         "v2_instance": "k7x2q9mfw3"},
        "iid",
    )
    assert out[CONF_TOPICS_VERSION] == 2 and out[CONF_V2_USER_ID] == 42
    # An old API's registration puts the entry back on V1.
    assert broker_data({"username": "ha-x", "password": "p"}, "iid")[CONF_TOPICS_VERSION] == 1


async def test_an_entry_with_no_topic_set_is_v1(hass, api, mock_feed_client):
    coordinator = await start(hass, make_entry())
    assert not coordinator.v2
    assert coordinator.topic_prefix == PREFIX
    assert coordinator.subscriptions[0][0] == f"{PREFIX}#"
    api.at.assert_not_called()


async def test_v2_subscribes_to_two_topics_whatever_the_places(hass, api, mock_feed_client):
    coordinator = await start_v2(hass, places=[*CACHED_PLACES, {"id": "103", "name": "Boat"}])
    assert coordinator.v2
    assert coordinator.topic_prefix == V2_PREFIX
    assert coordinator.subscriptions == [
        (f"V2/{V2_USER_ID}/NWS/#", 1),
        (f"V2/{V2_USER_ID}/HA/{V2_INSTANCE}/#", 1),
    ]


async def test_the_heartbeat_moves_an_entry_onto_v2_and_back(hass, api, mock_feed_client):
    coordinator = await start(hass, make_entry())
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        assert coordinator._follow_topics_version({"topics_version": 1}) is False
        assert coordinator._follow_topics_version({}) is False
        reload.assert_not_called()
        assert coordinator._follow_topics_version(
            {"topics_version": 2, "v2_user_id": 42, "v2_instance": V2_INSTANCE}
        ) is True
        reload.assert_called_once_with(coordinator.entry.entry_id)
    assert coordinator.entry.data[CONF_TOPICS_VERSION] == 2
    assert coordinator.entry.data[CONF_V2_USER_ID] == 42

    v2 = await start_v2(hass)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        assert v2._follow_topics_version(
            {"topics_version": 2, "v2_user_id": 42, "v2_instance": V2_INSTANCE}
        ) is False
        # The API rolled back: no field at all reads as V1.
        assert v2._follow_topics_version({"instance_id": "x", "active": True}) is True
        reload.assert_called_once()
    assert v2.entry.data[CONF_TOPICS_VERSION] == 1


# ===========================================================================
# The message
# ===========================================================================
def test_a_message_maps_onto_the_fields_the_entities_read():
    alert = parse_message(v2_alert())
    assert alert["short_id"] == V2_TORNADO_ID
    assert alert["id"] is None  # the numeric id is the API's
    for key in ("vtec", "event", "office", "phenomena", "significance", "severity",
                "urgency", "certainty", "headline", "onset", "ends", "action", "status",
                "geometry", "geometry_source"):
        assert alert[key] == v2_alert()[key]
    assert alert["instruction"] is None and alert["description"] is None
    assert alert["places"] == ["Home"]


@pytest.mark.parametrize(
    "raw",
    [None, [], {}, {"id": "short"}, {"id": 5, "status": "active"},
     {"id": "52HcPf5Z", "status": "bogus"}, {"id": "52Hc/f5Z", "status": "active"}],
)
def test_a_malformed_message_is_none(raw):
    assert parse_message(raw) is None


def test_the_api_supplies_the_numeric_id_and_instruction():
    merged = merge_detail(parse_message(v2_alert()), api_alert(numeric_id=777))
    assert merged["id"] == 777
    assert merged["instruction"].startswith("TAKE COVER")
    assert merged["description"].startswith("At 315 PM")
    # The message's own fields win.
    assert merged["headline"] == v2_alert()["headline"]
    assert merge_detail(parse_message(v2_alert()), None)["instruction"] is None


def test_geometry_comes_from_the_api_only_when_the_message_has_none():
    bare = parse_message(v2_alert(geometry=None, geometry_source=None))
    page = api_alert(geometry={"type": "Polygon", "coordinates": []})
    assert merge_detail(bare, page)["geometry"] == page["geometry"]
    kept = merge_detail(parse_message(v2_alert()), page)
    assert kept["geometry"] == v2_alert()["geometry"]


def test_an_alert_read_from_the_api_has_the_same_shape():
    alert = from_api(api_alert(), {"geometry": {"type": "Polygon", "coordinates": []}})
    assert alert["id"] == 1234 and alert["vtec"] == "KMOB.TO.W.0012.2026"
    assert alert["instruction"].startswith("TAKE COVER")
    assert alert["geometry_source"] == "polygon" and alert["action"] is None
    assert alert_key(42, alert) == V2_TOPIC_TORNADO
    assert from_api({"id": 1}) is None


# ===========================================================================
# Live messages
# ===========================================================================
async def test_an_alert_lands_on_the_places_it_names_with_the_api_detail(
    hass, api, mock_feed_client
):
    api.page.return_value = api_alert(numeric_id=1234)
    coordinator = await start_v2(hass)
    fired = events(hass, EVENT_ALERT)

    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))

    [alert] = coordinator.alerts_for(place_scope("101"))
    assert alert["id"] == 1234 and alert["instruction"].startswith("TAKE COVER")
    assert alert["action"] == "NEW" and alert["short_id"] == V2_TORNADO_ID
    # Home is named; Cabin is not, though it may share the county.
    assert coordinator.alerts_for(place_scope("102")) == []
    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get(TORNADO_SENSOR).state == STATE_ON
    assert hass.states.get(CABIN_ACTIVE).state == STATE_OFF
    [event] = fired
    assert event["place_id"] == "101" and event["id"] == 1234
    assert event["instruction"].startswith("TAKE COVER")
    assert event["topic"] == V2_TOPIC_TORNADO and event["action"] == "NEW"
    assert event["same"] == PLACE_HOME["same"]


async def test_the_sensor_attributes_carry_the_fields_users_see(hass, api, mock_feed_client):
    api.page.return_value = api_alert(numeric_id=1234)
    coordinator = await start_v2(hass)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    top = hass.states.get(TOP).attributes
    assert top["severity"] == "Extreme"
    assert top["instruction"].startswith("TAKE COVER")
    assert top["headline"] == v2_alert()["headline"]
    assert top["ends"] == v2_alert()["ends"]
    listed = hass.states.get(COUNT).attributes["alerts"][0]
    assert listed["id"] == 1234 and listed["vtec"] == "KMOB.TO.W.0012.2026"
    assert listed["action"] == "NEW" and listed["geometry_source"] == "polygon"
    assert hass.states.get(COUNT).state == "1"


async def test_a_failed_detail_fetch_shows_the_alert_without_the_extras(
    hass, api, mock_feed_client
):
    api.page.side_effect = ApiUnavailable("down")
    coordinator = await start_v2(hass)
    fired = events(hass, EVENT_ALERT)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    [alert] = coordinator.alerts_for(place_scope("101"))
    assert alert["id"] is None and alert["instruction"] is None
    assert alert["severity"] == "Extreme"
    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert len(fired) == 1


async def test_a_place_with_no_county_gets_alerts_on_v2(hass, api, mock_feed_client):
    coordinator = await start_v2(hass, places=[PLACE_BOAT])
    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(places=["Boat"]))
    )
    assert len(coordinator.alerts_for(place_scope("103"))) == 1
    assert hass.states.get("binary_sensor.wxalerts_boat_active_alert").state == STATE_ON


async def test_an_update_is_not_a_new_alert(hass, api, mock_feed_client):
    coordinator = await start_v2(hass)
    fired = events(hass, EVENT_ALERT)
    hazards = events(hass, EVENT_HAZARD)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(action="CON")))
    assert len(fired) == 1
    assert [h["action"] for h in hazards] == ["begin"]
    assert coordinator.alerts_for(place_scope("101"))[0]["action"] == "CON"


async def test_a_message_naming_a_label_two_places_share_reaches_both(
    hass, api, mock_feed_client
):
    twin = {**PLACE_CABIN, "name": "Home"}
    coordinator = await start_v2(
        hass, places=[{**CACHED_PLACES[0]}, {**CACHED_PLACES[1], "name": "Home"}]
    )
    del twin
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    assert len(coordinator.alerts_for(place_scope("101"))) == 1
    assert len(coordinator.alerts_for(place_scope("102"))) == 1


async def test_a_label_with_no_place_is_ignored(hass, api, mock_feed_client):
    coordinator = await start_v2(hass)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(places=["Nowhere"])))
    assert held(coordinator) == {}


async def test_an_empty_or_undecodable_message_is_harmless(hass, api, mock_feed_client):
    coordinator = await start_v2(hass)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, b"")
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, b"{nope")
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(["a list"]))
    assert held(coordinator) == {}


@pytest.mark.parametrize("status", ["cancelled", "expired"])
async def test_a_cancel_or_expiry_ends_the_alert(hass, api, mock_feed_client, status):
    coordinator = await start_v2(hass)
    hazards = events(hass, EVENT_HAZARD)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO,
        encode(v2_alert(status=status, action="CAN" if status == "cancelled" else "EXP")),
    )
    assert coordinator.alerts_for(place_scope("101")) == []
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert [h["action"] for h in hazards] == ["begin", "end"]


async def test_a_partial_cancel_releases_only_the_places_it_names(
    hass, api, mock_feed_client
):
    coordinator = await start_v2(hass)
    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(places=["Home", "Cabin"]))
    )
    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get(CABIN_ACTIVE).state == STATE_ON

    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO,
        encode(v2_alert(status="cancelled", action="CAN", places=["Cabin"])),
    )
    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get(CABIN_ACTIVE).state == STATE_OFF


async def test_a_cancel_naming_no_place_ends_it_everywhere(hass, api, mock_feed_client):
    coordinator = await start_v2(hass)
    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(places=["Home", "Cabin"]))
    )
    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO,
        encode(v2_alert(status="cancelled", places=[])),
    )
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert hass.states.get(CABIN_ACTIVE).state == STATE_OFF


async def test_an_active_update_that_shrinks_the_places_removes_the_rest(
    hass, api, mock_feed_client
):
    coordinator = await start_v2(hass)
    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(places=["Home", "Cabin"]))
    )
    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(action="CON", places=["Home"]))
    )
    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get(CABIN_ACTIVE).state == STATE_OFF


async def test_two_alerts_for_one_place_are_kept_apart(hass, api, mock_feed_client):
    coordinator = await start_v2(hass)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    await deliver(
        hass, coordinator, V2_TOPIC_SEVERE,
        encode(v2_alert(
            short_id=V2_SEVERE_ID, event="Severe Thunderstorm Warning", phenomena="SV",
            vtec="KMOB.SV.W.0044.2026", severity="Severe",
        )),
    )
    assert hass.states.get(COUNT).state == "2"
    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(status="cancelled"))
    )
    assert hass.states.get(COUNT).state == "1"


async def test_the_end_that_arrives_during_the_fetch_wins(hass, api, mock_feed_client):
    import asyncio

    gate = asyncio.Event()

    async def slow_page(_hass, _short_id):
        await gate.wait()
        return api_alert()

    api.page.side_effect = slow_page
    coordinator = await start_v2(hass)
    await coordinator._handle_message(V2_TOPIC_TORNADO, encode(v2_alert()))
    await coordinator._handle_message(
        V2_TOPIC_TORNADO, encode(v2_alert(status="cancelled"))
    )
    gate.set()
    await settle(hass)
    assert coordinator.alerts_for(place_scope("101")) == []


async def test_an_alert_long_past_its_end_is_dropped(hass, api, mock_feed_client):
    coordinator = await start_v2(hass)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    # Its end (2026-08-11T20:45Z) is far in the past relative to the real
    # clock, so the sweep takes it.
    coordinator._expire_v2_alerts()
    await settle(hass)
    assert coordinator.alerts_for(place_scope("101")) == []
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF


async def test_an_alert_still_running_is_not_swept(hass, api, mock_feed_client):
    coordinator = await start_v2(hass)
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    await deliver(
        hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(ends=soon, expires=soon))
    )
    coordinator._expire_v2_alerts()
    assert len(coordinator.alerts_for(place_scope("101"))) == 1


async def test_a_removed_place_takes_its_alerts_with_it(hass, api, mock_feed_client):
    coordinator = await start_v2(hass)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    await deliver(
        hass, coordinator, V2_TOPIC_PLACES, encode([PLACE_CABIN])
    )
    assert coordinator.alerts_for(place_scope("101")) == []
    assert place_scope("101") not in coordinator._alerts


# ===========================================================================
# The per instance topics keep their names under the V2 prefix
# ===========================================================================
async def test_places_and_status_are_read_from_the_v2_prefix(hass, api, mock_feed_client):
    coordinator = await start_v2(hass, places=[])
    await deliver(hass, coordinator, V2_TOPIC_PLACES, encode([PLACE_HOME, PLACE_CABIN]))
    assert set(coordinator.places) == {"101", "102"}
    await deliver(hass, coordinator, V2_TOPIC_STATUS, encode({"online": True}))
    assert coordinator.feed_online is True


async def test_the_old_prefix_is_ignored_on_v2(hass, api, mock_feed_client):
    coordinator = await start_v2(hass, places=[])
    await deliver(hass, coordinator, TOPIC_PLACES, encode([PLACE_HOME]))
    assert coordinator.places == {}


async def test_v1_topics_are_ignored_on_v2_and_v2_alerts_on_v1(hass, api, mock_feed_client):
    v2 = await start_v2(hass)
    from .const import TOPIC_TORNADO, TORNADO_ALERT

    await deliver(hass, v2, TOPIC_TORNADO, encode(TORNADO_ALERT))
    assert held(v2) == {}
    v1 = await start(hass, make_entry())
    await deliver(hass, v1, V2_TOPIC_TORNADO, encode(v2_alert()))
    assert held(v1) == {}


# ===========================================================================
# The alerts in force, read from the API
# ===========================================================================
async def test_connecting_loads_the_alerts_in_force_without_firing_events(
    hass, api, mock_feed_client
):
    api.at.side_effect = lambda _hass, lat, lon: (
        [api_alert()] if round(lat, 2) == 30.64 else []
    )
    api.page.return_value = api_alert(geometry={"type": "Polygon", "coordinates": []})
    fired, hazards = events(hass, EVENT_ALERT), events(hass, EVENT_HAZARD)
    coordinator = await start_v2(hass)
    await settle(hass)

    [alert] = coordinator.alerts_for(place_scope("101"))
    assert alert["id"] == 1234 and alert["vtec"] == "KMOB.TO.W.0012.2026"
    assert alert["instruction"].startswith("TAKE COVER")
    assert coordinator.alerts_for(place_scope("102")) == []
    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert fired == [] and hazards == []
    # Keyed as the feed keys it, so the feed's next message replaces it.
    assert list(coordinator._alerts[place_scope("101")]) == [V2_TOPIC_TORNADO]
    # One call per place.
    assert api.at.await_count == 2

    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(action="CON")))
    assert len(coordinator.alerts_for(place_scope("101"))) == 1
    assert fired == []  # an update of an alert already known


async def test_a_reconnect_reloads_and_drops_what_ended_meanwhile(hass, api, mock_feed_client):
    api.at.side_effect = lambda _hass, lat, lon: (
        [api_alert()] if round(lat, 2) == 30.64 else []
    )
    coordinator = await start_v2(hass)
    await settle(hass)
    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    hazards = events(hass, EVENT_HAZARD)

    api.at.side_effect = None
    api.at.return_value = []
    coordinator._handle_connection(False)
    coordinator._handle_connection(True)
    await settle(hass)
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert [h["action"] for h in hazards] == ["end"]


async def test_an_end_heard_during_the_load_is_not_undone_by_the_older_answer(
    hass, api, mock_feed_client
):
    import asyncio

    release = asyncio.Event()

    async def slow_at(_hass, lat, lon):
        await release.wait()
        return [api_alert()] if round(lat, 2) == 30.64 else []

    api.at.side_effect = slow_at
    entry = make_entry(**V2_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data
    coordinator._handle_connection(True)
    coordinator._priming_until = 0.0
    await asyncio.sleep(0)
    await coordinator._handle_message(
        V2_TOPIC_TORNADO, encode(v2_alert(status="cancelled"))
    )
    release.set()
    await settle(hass)
    assert coordinator.alerts_for(place_scope("101")) == []


async def test_an_alert_the_feed_announced_during_the_load_is_kept(
    hass, api, mock_feed_client
):
    import asyncio

    release = asyncio.Event()

    async def slow_at(_hass, lat, lon):
        await release.wait()
        return []

    api.at.side_effect = slow_at
    entry = make_entry(**V2_DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    coordinator = entry.runtime_data
    coordinator._handle_connection(True)
    coordinator._priming_until = 0.0
    await asyncio.sleep(0)
    await coordinator._handle_message(V2_TOPIC_TORNADO, encode(v2_alert()))
    await asyncio.sleep(0.05)
    release.set()
    await settle(hass)
    assert len(coordinator.alerts_for(place_scope("101"))) == 1


async def test_an_api_failure_keeps_the_state_and_retries(hass, api, mock_feed_client):
    api.at.side_effect = ApiUnavailable("down")
    with patch(
        "custom_components.wxalerts.coordinator.async_call_later"
    ) as later:
        coordinator = await start_v2(hass)
        await settle(hass)
    assert coordinator._alerts == {} or all(not c for c in coordinator._alerts.values())
    assert later.call_count >= 1
    assert later.call_args.args[1] == 30


async def test_a_new_place_loads_its_alerts(hass, api, mock_feed_client):
    coordinator = await start_v2(hass, places=[PLACE_HOME])
    await settle(hass)
    api.at.reset_mock()
    api.at.side_effect = lambda _hass, lat, lon: (
        [api_alert(numeric_id=9, short_id=V2_SEVERE_ID)] if round(lat, 2) == 32.54 else []
    )
    await deliver(hass, coordinator, V2_TOPIC_PLACES, encode([PLACE_HOME, PLACE_CABIN]))
    await settle(hass)
    assert api.at.await_count == 1
    [alert] = coordinator.alerts_for(place_scope("102"))
    assert alert["id"] == 9


async def test_v1_never_calls_the_api_for_alerts(hass, api, mock_feed_client):
    await start(hass, make_entry())
    api.at.assert_not_called()
    api.page.assert_not_called()


# ===========================================================================
# Map markers, triggers, diagnostics
# ===========================================================================
def _markers(hass):
    return [s for s in hass.states.async_all("geo_location") if s.attributes.get("source")]


async def test_a_marker_appears_for_the_place_inside_the_alert_only(
    hass, api, mock_feed_client
):
    # Home and Work share a county; only Home is inside the alert.
    work = {**CACHED_PLACES[0], "id": "104", "name": "Work"}
    coordinator = await start_v2(hass, places=[CACHED_PLACES[0], work])
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    [marker] = _markers(hass)
    assert marker.entity_id == "geo_location.wxalerts_home_tornado_warning"
    assert marker.attributes["place_id"] == "101"
    assert marker.attributes["same"] == PLACE_HOME["same"]
    assert marker.attributes["geometry"] == v2_alert()["geometry"]
    assert marker.attributes["vtec"] == "KMOB.TO.W.0012.2026"


async def test_the_marker_goes_when_the_alert_ends(hass, api, mock_feed_client):
    coordinator = await start_v2(hass)
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert()))
    assert len(_markers(hass)) == 1
    await deliver(hass, coordinator, V2_TOPIC_TORNADO, encode(v2_alert(status="expired")))
    await settle(hass)
    assert _markers(hass) == []


async def test_a_marker_follows_a_loaded_alert(hass, api, mock_feed_client):
    api.at.side_effect = lambda _hass, lat, lon: (
        [api_alert()] if round(lat, 2) == 30.64 else []
    )
    api.page.return_value = api_alert(geometry=v2_alert()["geometry"])
    await start_v2(hass)
    await settle(hass)
    assert len(_markers(hass)) == 1


async def test_hazard_triggers_are_offered_on_a_place_with_no_county_on_v2(
    hass, api, mock_feed_client
):
    from homeassistant.components.device_automation import DeviceAutomationType
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import (
        async_get_device_automations,
    )

    from custom_components.wxalerts.const import DOMAIN
    from custom_components.wxalerts.coordinator import entry_device

    await async_setup_component(hass, "homeassistant", {})
    entry = make_entry(places=[PLACE_BOAT | {"id": "103", "same": None}], **V2_DATA)
    await start(hass, entry)
    device = entry_device(hass, entry.entry_id, (DOMAIN, f"{entry.entry_id}_place_103"))
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device.id
    )
    types = {t["type"] for t in triggers if t["domain"] == DOMAIN}
    assert "tornado_warning_begin" in types and "storm_cell_open" in types


async def test_diagnostics_name_the_topic_set_and_hide_the_ids(hass, api, mock_feed_client):
    from custom_components.wxalerts.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    coordinator = await start_v2(hass)
    result = await async_get_config_entry_diagnostics(hass, coordinator.entry)
    assert result["state"]["topics_version"] == 2
    text = str(result)
    assert V2_INSTANCE not in text
    assert f"V2/{V2_USER_ID}/" not in text
    assert result["entry"]["data"][CONF_V2_INSTANCE] != V2_INSTANCE
    assert result["subscriptions"][0]["topic"].startswith("V2/**REDACTED**/NWS/")
