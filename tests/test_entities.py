"""Entities end to end: a message goes in at the MQTT layer and the test
asserts on what a user would see in Home Assistant.

Messages are handed to ``_handle_message`` because that is exactly what the
real ``FeedClient`` calls with the topic and raw payload.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wxalerts.const import CONF_PLACES, DOMAIN

from .conftest import feed, make_entry, start
from .const import (
    CACHED_PLACES,
    HEAT_ALERT,
    PLACE_BOAT,
    PLACE_CABIN,
    PLACE_HOME,
    PLACES,
    SEVERE_ALERT,
    TOMBSTONE,
    TOPIC_HEAT,
    TOPIC_PLACES,
    TOPIC_SEVERE,
    TOPIC_STATUS,
    TOPIC_TORNADO,
    TORNADO_ALERT,
    encode,
    status,
    storm,
    storm_topic,
)

ACTIVE_ALERT = "binary_sensor.wxalerts_home_active_alert"
TORNADO_SENSOR = "binary_sensor.wxalerts_home_tornado_warning"
TORNADO_WATCH = "binary_sensor.wxalerts_home_tornado_watch"
SEVERE_SENSOR = "binary_sensor.wxalerts_home_severe_thunderstorm_warning"
SEVERITY = "sensor.wxalerts_home_highest_severity"
COUNT = "sensor.wxalerts_home_active_alerts"
TOP = "sensor.wxalerts_home_top_alert"
STORM = "binary_sensor.wxalerts_home_storm_cell_approaching"
ETA = "sensor.wxalerts_home_storm_cell_eta"
THREAT = "sensor.wxalerts_home_storm_cell_threat"
FEED_AGE = "sensor.wxalerts_feed_age"
CONNECTED = "binary_sensor.wxalerts_connected"


# ===========================================================================
# Entity creation
# ===========================================================================
async def test_every_place_gets_its_entity_set(hass, setup_integration):
    for entity_id in (
        ACTIVE_ALERT,
        SEVERITY,
        COUNT,
        TOP,
        TORNADO_SENSOR,
        SEVERE_SENSOR,
        STORM,
        ETA,
        THREAT,
    ):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get("binary_sensor.wxalerts_cabin_active_alert") is not None
    assert hass.states.get("binary_sensor.wxalerts_cabin_storm_cell_approaching")
    assert hass.states.get(FEED_AGE) is not None
    assert hass.states.get(CONNECTED).state == STATE_ON


async def test_one_device_per_place(hass, config_entry, setup_integration):
    devices = dr.async_entries_for_config_entry(
        dr.async_get(hass), config_entry.entry_id
    )
    names = sorted(device.name for device in devices)
    assert names == ["WxAlerts", "WxAlerts Cabin", "WxAlerts Home"]


async def test_two_places_in_one_county_each_get_entities(
    hass, mock_feed_client, enable_custom_integrations
):
    """Places, not counties, are the unit now: Home and Work in one county are
    two devices, each with its own alert entities, and both light up."""
    work = {**CACHED_PLACES[0], "id": "104", "name": "Work"}
    coordinator = await start(hass, make_entry(places=[CACHED_PLACES[0], work]))

    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))

    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get("binary_sensor.wxalerts_work_active_alert").state == STATE_ON


async def test_life_threatening_warnings_are_the_default_selection(
    hass, setup_integration
):
    for entity_id in (TORNADO_SENSOR, SEVERE_SENSOR):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get(TORNADO_WATCH) is None
    assert hass.states.get("binary_sensor.wxalerts_home_heat_advisory") is None


async def test_selecting_event_types_creates_exactly_those_sensors(
    hass, mock_feed_client, enable_custom_integrations
):
    await start(
        hass,
        make_entry(options={"event_sensors": ["tornado_watch", "blizzard_warning"]}),
    )

    assert hass.states.get(TORNADO_WATCH) is not None
    assert hass.states.get("binary_sensor.wxalerts_home_blizzard_warning") is not None
    assert hass.states.get(TORNADO_SENSOR) is None


async def test_selecting_nothing_creates_no_event_sensors(
    hass, mock_feed_client, enable_custom_integrations
):
    await start(hass, make_entry(options={"event_sensors": []}))

    assert hass.states.get(ACTIVE_ALERT) is not None
    assert hass.states.get(TORNADO_SENSOR) is None


async def test_entities_are_unavailable_until_the_feed_connects(
    hass, config_entry, mock_feed_client, enable_custom_integrations
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT).state == STATE_UNAVAILABLE
    assert hass.states.get(STORM).state == STATE_UNAVAILABLE

    config_entry.runtime_data._handle_connection(True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert hass.states.get(STORM).state == STATE_OFF


async def test_losing_the_connection_marks_entities_unavailable(
    hass, setup_integration
):
    setup_integration._handle_connection(False)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT).state == STATE_UNAVAILABLE
    assert hass.states.get(STORM).state == STATE_UNAVAILABLE
    assert hass.states.get(CONNECTED).state == STATE_OFF


# ===========================================================================
# Places discovery
# ===========================================================================
async def test_places_arrive_on_a_fresh_entry(
    hass, mock_feed_client, enable_custom_integrations
):
    """A new entry may not have seen the retained list yet. Devices appear
    when it lands, with no restart."""
    entry = make_entry(places=[])
    coordinator = await start(hass, entry)
    assert hass.states.get(ACTIVE_ALERT) is None

    await feed(hass, coordinator, TOPIC_PLACES, encode(PLACES))

    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert hass.states.get(STORM).state == STATE_OFF
    assert hass.states.get("binary_sensor.wxalerts_cabin_active_alert") is not None
    # Cached, so the next start has entities before the broker replays it.
    assert entry.data[CONF_PLACES] == CACHED_PLACES
    # And the new counties are subscribed live, without reconnecting.
    topics = [
        topic
        for topic, _qos in mock_feed_client.return_value.set_subscriptions.await_args[
            0
        ][0]
    ]
    assert "wxalerts/nws/v1/same/012113/#" in topics
    assert "wxalerts/nws/v1/same/001001/#" in topics


async def test_caching_places_does_not_reload_the_entry(
    hass, mock_feed_client, enable_custom_integrations
):
    entry = make_entry(places=[])
    coordinator = await start(hass, entry)

    await feed(hass, coordinator, TOPIC_PLACES, encode(PLACES))

    assert entry.runtime_data is coordinator
    assert mock_feed_client.call_count == 1


async def test_a_new_place_is_added_live(hass, setup_integration):
    await feed(
        hass, setup_integration, TOPIC_PLACES, encode([*PLACES, PLACE_BOAT])
    )

    assert hass.states.get("binary_sensor.wxalerts_boat_storm_cell_approaching")
    # No county: alert entities exist but say so honestly.
    assert (
        hass.states.get("binary_sensor.wxalerts_boat_active_alert").state
        == STATE_UNAVAILABLE
    )


async def test_a_removed_place_takes_its_device_with_it(
    hass, config_entry, setup_integration
):
    await feed(hass, setup_integration, TOPIC_PLACES, encode([PLACE_HOME]))

    device_registry = dr.async_get(hass)
    names = sorted(
        device.name
        for device in dr.async_entries_for_config_entry(
            device_registry, config_entry.entry_id
        )
    )
    assert names == ["WxAlerts", "WxAlerts Home"]
    assert er.async_get(hass).async_get("binary_sensor.wxalerts_cabin_active_alert") is None
    assert hass.states.get("binary_sensor.wxalerts_cabin_active_alert") is None


async def test_a_renamed_place_renames_its_device(hass, config_entry, setup_integration):
    await feed(
        hass,
        setup_integration,
        TOPIC_PLACES,
        encode([{**PLACE_HOME, "name": "House"}, PLACE_CABIN]),
    )

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{config_entry.entry_id}_place_101")}
    )
    assert device.name == "WxAlerts House"
    # Entity ids are not rewritten under the user.
    assert hass.states.get(ACTIVE_ALERT) is not None


async def test_a_place_that_moves_county_follows_its_new_county(
    hass, setup_integration
):
    coordinator = setup_integration
    await feed(
        hass,
        coordinator,
        TOPIC_PLACES,
        encode([{**PLACE_HOME, "same": "001001"}, PLACE_CABIN]),
    )

    await feed(hass, coordinator, TOPIC_HEAT, encode(HEAT_ALERT))

    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get(SEVERITY).state == "Moderate"


async def test_an_empty_places_list_keeps_every_device_and_checks_standing(
    hass, config_entry, no_heartbeat, setup_integration
):
    """The server sends [] on revoke. Deleting everything on that alone left
    the owner with no devices and no explanation."""
    calls = no_heartbeat.await_count
    await feed(hass, setup_integration, TOPIC_PLACES, encode([]))

    assert no_heartbeat.await_count == calls + 1
    names = sorted(
        device.name
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), config_entry.entry_id
        )
    )
    assert names == ["WxAlerts", "WxAlerts Cabin", "WxAlerts Home"]
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF


async def test_a_revoke_announced_by_empty_places_keeps_devices_unavailable(
    hass, config_entry, aioclient_mock, no_heartbeat, setup_integration
):
    from homeassistant.helpers import issue_registry as ir

    from .test_coordinator import mock_heartbeat

    mock_heartbeat(aioclient_mock, active=False, reason="revoked")
    async def _real(*_args):
        await no_heartbeat.original(setup_integration)

    no_heartbeat.side_effect = _real
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))

    await feed(hass, setup_integration, TOPIC_PLACES, encode([]))
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, "instance_revoked")
    assert len(
        dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)
    ) == 3
    for entity_id in (
        ACTIVE_ALERT,
        STORM,
        ETA,
        "geo_location.wxalerts_home_tornado_warning",
    ):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id


async def test_an_empty_list_from_an_active_account_keeps_devices_until_replaced(
    hass, config_entry, aioclient_mock, no_heartbeat, setup_integration
):
    """Really no places: entities go unavailable, devices stay. A later
    non-empty list removes the ones it does not name."""
    from .test_coordinator import mock_heartbeat

    mock_heartbeat(aioclient_mock)
    async def _real(*_args):
        await no_heartbeat.original(setup_integration)

    no_heartbeat.side_effect = _real

    await feed(hass, setup_integration, TOPIC_PLACES, encode([]))
    await hass.async_block_till_done()
    assert hass.states.get(ACTIVE_ALERT).state == STATE_UNAVAILABLE
    device_registry = dr.async_get(hass)
    assert len(dr.async_entries_for_config_entry(device_registry, config_entry.entry_id)) == 3

    await feed(hass, setup_integration, TOPIC_PLACES, encode([PLACE_HOME]))

    names = sorted(
        device.name
        for device in dr.async_entries_for_config_entry(
            device_registry, config_entry.entry_id
        )
    )
    assert names == ["WxAlerts", "WxAlerts Home"]
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF


async def test_a_status_with_a_reason_checks_standing_at_once(
    hass, no_heartbeat, setup_integration
):
    calls = no_heartbeat.await_count
    await feed(
        hass,
        setup_integration,
        TOPIC_STATUS,
        encode({**status(online=False), "reason": "revoked"}),
    )
    assert no_heartbeat.await_count == calls + 1
    # A plain offline status, with no reason, is an outage, not a revoke.
    await feed(hass, setup_integration, TOPIC_STATUS, encode(status(online=False)))
    assert no_heartbeat.await_count == calls + 1


async def test_a_malformed_places_payload_deletes_nothing(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_PLACES, encode({"oops": True}))
    await feed(hass, setup_integration, TOPIC_PLACES, b"not json")
    await feed(hass, setup_integration, TOPIC_PLACES, TOMBSTONE)

    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert hass.states.get("binary_sensor.wxalerts_cabin_active_alert") is not None


# ===========================================================================
# Alert entities
# ===========================================================================
async def test_quiet_county_reads_as_clear(hass, setup_integration):
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert hass.states.get(SEVERITY).state == "None"
    assert hass.states.get(COUNT).state == "0"
    assert hass.states.get(TOP).state == "None"
    assert hass.states.get(TORNADO_SENSOR).state == STATE_OFF


async def test_a_tornado_warning_lights_everything_up(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))

    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get(SEVERITY).state == "Extreme"
    assert hass.states.get(COUNT).state == "1"
    assert hass.states.get(TORNADO_SENSOR).state == STATE_ON
    assert hass.states.get(SEVERE_SENSOR).state == STATE_OFF
    assert hass.states.get(TOP).state == "Tornado Warning"


async def test_the_count_sensor_carries_the_alert_list(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))

    attributes = hass.states.get(COUNT).attributes
    assert attributes["same"] == "012113"
    assert attributes["place_name"] == "Home"
    assert attributes["place_id"] == "101"
    alert = attributes["alerts"][0]
    assert alert["event"] == "Tornado Warning"
    assert alert["headline"] == TORNADO_ALERT["headline"]
    assert alert["instruction"] == TORNADO_ALERT["instruction"]
    assert alert["event_type"] == "tornado_warning"
    assert attributes["events"] == ["Tornado Warning"]


async def test_a_watch_does_not_light_the_warning_sensor(
    hass, mock_feed_client, enable_custom_integrations
):
    coordinator = await start(
        hass, make_entry(options={"event_sensors": ["tornado_warning", "tornado_watch"]})
    )
    watch = {
        **TORNADO_ALERT,
        "vtec": "KMOB.TO.A.0099.2026",
        "significance": "A",
        "event": "Tornado Watch",
        "severity": "Severe",
    }
    await feed(hass, coordinator, f"{TOPIC_TORNADO}9", encode(watch))
    assert hass.states.get(TORNADO_WATCH).state == STATE_ON
    assert hass.states.get(TORNADO_SENSOR).state == STATE_OFF

    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    assert hass.states.get(TORNADO_SENSOR).state == STATE_ON
    assert hass.states.get(TOP).state == "Tornado Warning"


async def test_two_hazards_count_and_rank(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_SEVERE, encode(SEVERE_ALERT))
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))

    assert hass.states.get(COUNT).state == "2"
    assert hass.states.get(ACTIVE_ALERT).attributes["events"] == [
        "Tornado Warning",
        "Severe Thunderstorm Warning",
    ]


async def test_a_tombstone_clears_the_entities(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await feed(hass, setup_integration, TOPIC_TORNADO, TOMBSTONE)

    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert hass.states.get(SEVERITY).state == "None"
    assert hass.states.get(COUNT).state == "0"
    assert hass.states.get(TORNADO_SENSOR).state == STATE_OFF


async def test_ending_one_hazard_leaves_the_other_on(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await feed(hass, setup_integration, TOPIC_SEVERE, encode(SEVERE_ALERT))
    await feed(hass, setup_integration, TOPIC_TORNADO, TOMBSTONE)

    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get(TORNADO_SENSOR).state == STATE_OFF
    assert hass.states.get(SEVERE_SENSOR).state == STATE_ON


async def test_a_second_place_is_not_disturbed(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_HEAT, encode(HEAT_ALERT))

    assert hass.states.get("binary_sensor.wxalerts_cabin_active_alert").state == STATE_ON
    assert hass.states.get("sensor.wxalerts_cabin_highest_severity").state == "Moderate"
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF


async def test_severity_sensor_options_cover_every_state_it_reports(
    hass, setup_integration
):
    options = hass.states.get(SEVERITY).attributes["options"]
    for severity in ("Extreme", "Severe", "Moderate", "Minor", "None"):
        assert severity in options


# ===========================================================================
# Storm cells
# ===========================================================================
async def test_a_storm_cell_opens(hass, setup_integration):
    await feed(hass, setup_integration, storm_topic(101), encode(storm()))

    state = hass.states.get(STORM)
    assert state.state == STATE_ON
    assert state.attributes["device_class"] == "safety"
    assert state.attributes["storm_id"] == "ps-449120"
    assert state.attributes["rung"] == 40
    assert state.attributes["rung_label"] == "Thunderstorm Risk"
    assert state.attributes["prob_severe"] == 46
    assert state.attributes["prob_tor"] == 3
    assert state.attributes["prob_hail"] == 22
    assert state.attributes["prob_wind"] == 41
    assert state.attributes["speed_kmh"] == 52.0
    assert state.attributes["bearing_deg"] == 245.0
    assert state.attributes["distance_km"] == 15.6
    assert state.attributes["eta_min"] == pytest.approx(18.0, abs=0.1)
    assert hass.states.get(THREAT).state == "thunderstorm_risk"
    assert float(hass.states.get(ETA).state) == pytest.approx(18.0, abs=0.1)
    assert hass.states.get(ETA).attributes["device_class"] == "duration"
    # The other place is untouched.
    assert (
        hass.states.get("binary_sensor.wxalerts_cabin_storm_cell_approaching").state
        == STATE_OFF
    )


async def test_a_storm_cell_escalates(hass, setup_integration):
    await feed(hass, setup_integration, storm_topic(101), encode(storm()))
    await feed(
        hass,
        setup_integration,
        storm_topic(101),
        encode(storm(episode="escalate", rung=70, rung_label="Tornado Risk", eta_min=9)),
    )

    assert hass.states.get(THREAT).state == "tornado_risk"
    assert float(hass.states.get(ETA).state) == pytest.approx(9.0, abs=0.1)
    assert hass.states.get(STORM).attributes["rung"] == 70


async def test_a_storm_cell_clears(hass, setup_integration):
    await feed(hass, setup_integration, storm_topic(101), encode(storm()))
    await feed(hass, setup_integration, storm_topic(101), TOMBSTONE)

    assert hass.states.get(STORM).state == STATE_OFF
    assert hass.states.get(ETA).state == STATE_UNKNOWN
    assert hass.states.get(THREAT).state == "none"


async def test_the_most_threatening_cell_drives_the_entities(hass, setup_integration):
    await feed(hass, setup_integration, storm_topic(101, "a"), encode(storm(storm_id="a", eta_min=5)))
    await feed(
        hass,
        setup_integration,
        storm_topic(101, "b"),
        encode(storm(storm_id="b", rung=80, rung_label="Tornado Detected", eta_min=20)),
    )

    assert hass.states.get(THREAT).state == "tornado_detected"
    assert hass.states.get(STORM).attributes["storm_count"] == 2
    await feed(hass, setup_integration, storm_topic(101, "b"), TOMBSTONE)
    assert hass.states.get(THREAT).state == "thunderstorm_risk"
    assert hass.states.get(STORM).state == STATE_ON


async def test_an_unknown_rung_maps_to_its_nearest_lower_neighbour(
    hass, setup_integration
):
    await feed(hass, setup_integration, storm_topic(101), encode(storm(rung=77)))
    assert hass.states.get(THREAT).state == "tornado_risk_high"
    options = hass.states.get(THREAT).attributes["options"]
    assert "none" in options and "tornado_confirmed" in options


async def test_the_eta_counts_down_with_no_messages(hass, freezer, setup_integration):
    await feed(hass, setup_integration, storm_topic(101), encode(storm(eta_min=18)))

    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert float(hass.states.get(ETA).state) == pytest.approx(13.0, abs=0.2)


async def test_a_storm_nobody_cleared_expires(hass, freezer, setup_integration):
    await feed(hass, setup_integration, storm_topic(101), encode(storm()))

    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert hass.states.get(STORM).state == STATE_OFF


async def test_a_stale_retained_storm_is_ignored(hass, setup_integration):
    await feed(
        hass, setup_integration, storm_topic(101), encode(storm(age_seconds=7200))
    )
    assert hass.states.get(STORM).state == STATE_OFF


# ===========================================================================
# Feed age
# ===========================================================================
async def test_feed_age_keeps_rising_with_no_messages(hass, freezer, setup_integration):
    await feed(hass, setup_integration, TOPIC_STATUS, encode(status()))
    assert int(hass.states.get(FEED_AGE).state) < 5

    freezer.tick(timedelta(seconds=120))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert int(hass.states.get(FEED_AGE).state) == pytest.approx(120, abs=5)


async def test_a_stale_feed_makes_storm_entities_unavailable(
    hass, freezer, setup_integration
):
    """A calm "off" from a feed that stopped looks exactly like no storm."""
    await feed(hass, setup_integration, TOPIC_STATUS, encode(status()))
    assert hass.states.get(STORM).state == STATE_OFF

    freezer.tick(timedelta(minutes=16))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert hass.states.get(STORM).state == STATE_UNAVAILABLE
    assert hass.states.get(ETA).state == STATE_UNAVAILABLE
    # Alerts ride the public feed and do not depend on the heartbeat.
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF

    await feed(hass, setup_integration, TOPIC_STATUS, encode(status()))
    assert hass.states.get(STORM).state == STATE_OFF


async def test_an_offline_status_makes_storm_entities_unavailable(
    hass, setup_integration
):
    await feed(hass, setup_integration, TOPIC_STATUS, encode(status(online=False)))
    assert hass.states.get(STORM).state == STATE_UNAVAILABLE
    assert hass.states.get(FEED_AGE).attributes["online"] is False


async def test_feed_age_is_on_the_storm_sensor(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_STATUS, encode(status(age_seconds=60)))
    await feed(hass, setup_integration, storm_topic(101), encode(storm()))
    assert hass.states.get(STORM).attributes["feed_age_seconds"] == pytest.approx(
        60, abs=5
    )


# ===========================================================================
# Map markers
# ===========================================================================
def geo_entities(hass) -> list:
    return [
        state
        for state in hass.states.async_all("geo_location")
        if state.attributes.get("source") == DOMAIN
    ]


async def test_a_marker_appears_for_a_hazard_with_geometry(hass, setup_integration):
    assert geo_entities(hass) == []
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))

    markers = geo_entities(hass)
    assert len(markers) == 1
    marker = markers[0]
    assert marker.attributes["friendly_name"] == "WxAlerts Home Tornado Warning"
    assert marker.attributes["latitude"] == pytest.approx(30.64, abs=0.01)
    assert marker.entity_id == "geo_location.wxalerts_home_tornado_warning"
    assert marker.attributes["place_name"] == "Home"
    assert marker.attributes["geometry"] == TORNADO_ALERT["geometry"]


async def test_markers_belong_to_their_place_device(
    hass, config_entry, setup_integration
):
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))

    registry_entry = er.async_get(hass).async_get(
        "geo_location.wxalerts_home_tornado_warning"
    )
    assert registry_entry.unique_id == (
        f"{config_entry.entry_id}_place_101_geo_{TOPIC_TORNADO}"
    )
    device = dr.async_get(hass).async_get(registry_entry.device_id)
    assert (DOMAIN, f"{config_entry.entry_id}_place_101") in device.identifiers


async def test_each_place_in_a_county_gets_its_own_marker(
    hass, mock_feed_client, enable_custom_integrations
):
    work = {**CACHED_PLACES[0], "id": "104", "name": "Work"}
    coordinator = await start(hass, make_entry(places=[CACHED_PLACES[0], work]))
    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))

    ids = sorted(state.entity_id for state in geo_entities(hass))
    assert ids == [
        "geo_location.wxalerts_home_tornado_warning",
        "geo_location.wxalerts_work_tornado_warning",
    ]


async def test_markers_go_with_their_place(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await feed(hass, setup_integration, TOPIC_PLACES, encode([PLACE_CABIN]))

    assert geo_entities(hass) == []
    assert er.async_get(hass).async_get("geo_location.wxalerts_home_tornado_warning") is None


async def test_a_place_added_later_gets_markers_for_live_hazards(
    hass, mock_feed_client, enable_custom_integrations
):
    coordinator = await start(hass, make_entry(places=[CACHED_PLACES[1]]))
    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    assert geo_entities(hass) == []

    await feed(hass, coordinator, TOPIC_PLACES, encode(PLACES))

    assert hass.states.get("geo_location.wxalerts_home_tornado_warning")


async def test_marker_geometry_is_kept_out_of_the_recorder(hass, setup_integration):
    from custom_components.wxalerts.geo_location import AlertMarker

    assert "geometry" in AlertMarker._unrecorded_attributes


async def test_the_marker_vanishes_on_the_tombstone(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await feed(hass, setup_integration, TOPIC_TORNADO, TOMBSTONE)
    assert geo_entities(hass) == []


async def test_one_marker_per_hazard(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await feed(hass, setup_integration, TOPIC_SEVERE, encode(SEVERE_ALERT))
    for _ in range(3):
        await feed(
            hass,
            setup_integration,
            TOPIC_TORNADO,
            encode({**TORNADO_ALERT, "action": "CON"}),
        )
    assert len(geo_entities(hass)) == 2


async def test_a_hazard_with_no_geometry_gets_no_marker(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_HEAT, encode(HEAT_ALERT))
    assert geo_entities(hass) == []


# ===========================================================================
# Unload
# ===========================================================================
async def test_unload_stops_the_feed(hass, config_entry, setup_integration, mock_feed_client):
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_feed_client.return_value.stop.assert_awaited_once()
    assert hass.states.get(ACTIVE_ALERT).state == STATE_UNAVAILABLE


async def test_reload_restores_the_entities(
    hass, config_entry, setup_integration, mock_feed_client
):
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    config_entry.runtime_data._handle_connection(True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF


async def test_changing_options_reloads_the_entry(
    hass, config_entry, setup_integration
):
    hass.config_entries.async_update_entry(
        config_entry, options={"event_sensors": ["tornado_watch"]}
    )
    await hass.async_block_till_done()

    assert config_entry.runtime_data is not setup_integration
    assert hass.states.get(TORNADO_WATCH) is not None
