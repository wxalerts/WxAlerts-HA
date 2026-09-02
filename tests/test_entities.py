"""Entities end to end: a message goes in at the MQTT layer and the test
asserts on what a user would see in Home Assistant.

Messages are handed to ``_handle_message`` because that is exactly what the
real ``FeedClient`` calls with the topic and raw payload — everything below
that line is the MQTT library's job, not this integration's.
"""

from __future__ import annotations

import pytest
from homeassistant.const import (
    UnitOfLength,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_conversion import DistanceConverter

from custom_components.wxalerts.const import (
    TOPIC_GLM_STATUS,
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_GLM_ALERT,
    CONF_GLM_RADIUS,
    CONF_EVENT_SENSORS,
    CONF_LOCATIONS,
    CONF_ZONES,
    DOMAIN,
)

from .const import (
    GLM_BURST,
    HEAT_ALERT,
    LOCATION_SANTA_ROSA,
    SEVERE_ALERT,
    TOMBSTONE,
    TOPIC_GLM_LEAF,
    TOPIC_HEAT,
    TOPIC_SEVERE,
    TOPIC_TORNADO,
    TORNADO_ALERT,
    encode,
    fresh_burst,
)

ACTIVE_ALERT = "binary_sensor.wxalerts_home_active_alert"
# One entity per NWS event type, not per phenomenon: the warning and the watch
# are separate sensors because they mean separate things.
TORNADO_SENSOR = "binary_sensor.wxalerts_home_tornado_warning"
TORNADO_WATCH = "binary_sensor.wxalerts_home_tornado_watch"
SEVERE_SENSOR = "binary_sensor.wxalerts_home_severe_thunderstorm_warning"
SEVERITY = "sensor.wxalerts_home_highest_severity"
COUNT = "sensor.wxalerts_home_active_alerts"
TOP = "sensor.wxalerts_home_top_alert"
# The lightning device is named "Lightning — Home", so Home Assistant builds
# these ids from the device and entity names together. They match what a
# dashboard built against the standalone proximity service already points at,
# which is the whole point of naming the device that way.
NEAREST = "sensor.lightning_home_nearest_strike"
RING_10 = "sensor.lightning_home_strikes_10mi"
RATE = "sensor.lightning_home_strike_rate"
SINCE = "sensor.lightning_home_since_last_strike"
TREND = "sensor.lightning_home_trend"
NEARBY = "binary_sensor.lightning_home_lightning_within_10mi"


async def feed(hass, coordinator, topic, payload):
    """Deliver one MQTT message and let the entities settle."""
    await coordinator._handle_message(topic, payload)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Entity creation
# ---------------------------------------------------------------------------


async def test_entities_are_created_for_each_county(hass, setup_integration):
    for entity_id in (ACTIVE_ALERT, SEVERITY, COUNT, NEAREST, RING_10, NEARBY):
        assert hass.states.get(entity_id) is not None, entity_id

    assert hass.states.get("binary_sensor.wxalerts_cabin_active_alert") is not None
    assert hass.states.get("sensor.wxalerts_cabin_highest_severity") is not None


async def test_life_threatening_warnings_are_the_default_selection(
    hass, setup_integration
):
    """A fresh entry gets the hazards that can kill someone asleep, and
    nothing else — every other type is a device trigger away, and an entity
    only when asked for."""
    for entity_id in (TORNADO_SENSOR, SEVERE_SENSOR):
        assert hass.states.get(entity_id) is not None, entity_id

    # A watch is not a warning: it is not created unless selected, which is
    # exactly the distinction the phenomenon buckets could not make.
    assert hass.states.get(TORNADO_WATCH) is None
    assert hass.states.get("binary_sensor.wxalerts_home_heat_advisory") is None


async def test_selecting_event_types_creates_exactly_those_sensors(
    hass, mock_feed_client, enable_custom_integrations
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: False,
        },
        options={CONF_EVENT_SENSORS: ["tornado_watch", "blizzard_warning"]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(TORNADO_WATCH) is not None
    assert hass.states.get("binary_sensor.wxalerts_home_blizzard_warning") is not None
    # Not selected, so not created — the default set does not leak back in.
    assert hass.states.get(TORNADO_SENSOR) is None


async def test_a_watch_does_not_light_the_warning_sensor(
    hass, mock_feed_client, enable_custom_integrations
):
    """The whole reason for the rework. Under the phenomenon buckets both of
    these turned on one "Tornado" sensor, so a take-cover automation fired on
    a watch."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: False,
        },
        options={CONF_EVENT_SENSORS: ["tornado_warning", "tornado_watch"]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    coordinator._handle_connection(True)
    await hass.async_block_till_done()

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

    assert hass.states.get(TORNADO_WATCH).state == STATE_ON
    assert hass.states.get(TORNADO_SENSOR).state == STATE_ON
    # Severity ties at "Severe" between a watch and many warnings, so the top
    # sensor has to break the tie on significance rather than arrival order.
    assert hass.states.get(TOP).state == "Tornado Warning"


async def test_entities_are_unavailable_until_the_feed_connects(
    hass, config_entry, mock_feed_client, enable_custom_integrations
):
    """Better an honest ``unavailable`` than a confident "no alerts" from a
    client that never connected."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT).state == STATE_UNAVAILABLE

    config_entry.runtime_data._handle_connection(True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF


async def test_losing_the_connection_marks_entities_unavailable(
    hass, setup_integration
):
    coordinator = setup_integration
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF

    coordinator._handle_connection(False)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT).state == STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Alert entities
# ---------------------------------------------------------------------------


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
    """This attribute is what a Markdown card renders."""
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))

    attributes = hass.states.get(COUNT).attributes
    assert attributes["same"] == "012113"
    assert attributes["county_ugc"] == "FLC113"
    assert attributes["location"] == "Home"

    alert = attributes["alerts"][0]
    assert alert["event"] == "Tornado Warning"
    assert alert["headline"] == TORNADO_ALERT["headline"]
    assert alert["instruction"] == TORNADO_ALERT["instruction"]
    assert alert["ends"] == "2026-08-11T20:45:00+00:00"
    assert alert["geometry_source"] == "polygon"
    assert alert["event_type"] == "tornado_warning"
    assert attributes["events"] == ["Tornado Warning"]
    assert attributes["event_types"] == ["tornado_warning"]


async def test_two_hazards_count_and_rank(hass, setup_integration):
    coordinator = setup_integration
    await feed(hass, coordinator, TOPIC_SEVERE, encode(SEVERE_ALERT))
    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))

    assert hass.states.get(COUNT).state == "2"
    assert hass.states.get(SEVERITY).state == "Extreme"
    assert hass.states.get(ACTIVE_ALERT).attributes["events"] == [
        "Tornado Warning",
        "Severe Thunderstorm Warning",
    ]


async def test_a_tombstone_clears_the_entities(hass, setup_integration):
    """The bug this integration was warned about: without this the warning
    stays on forever."""
    coordinator = setup_integration
    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON

    await feed(hass, coordinator, TOPIC_TORNADO, TOMBSTONE)

    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert hass.states.get(SEVERITY).state == "None"
    assert hass.states.get(COUNT).state == "0"
    assert hass.states.get(TORNADO_SENSOR).state == STATE_OFF


async def test_ending_one_hazard_leaves_the_other_on(hass, setup_integration):
    coordinator = setup_integration
    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await feed(hass, coordinator, TOPIC_SEVERE, encode(SEVERE_ALERT))

    await feed(hass, coordinator, TOPIC_TORNADO, TOMBSTONE)

    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get(TORNADO_SENSOR).state == STATE_OFF
    assert hass.states.get(SEVERE_SENSOR).state == STATE_ON
    assert hass.states.get(SEVERITY).state == "Severe"


async def test_a_second_county_is_not_disturbed(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_HEAT, encode(HEAT_ALERT))

    assert hass.states.get("binary_sensor.wxalerts_cabin_active_alert").state == STATE_ON
    assert hass.states.get("sensor.wxalerts_cabin_highest_severity").state == "Moderate"
    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert hass.states.get(SEVERITY).state == "None"


async def test_severity_sensor_options_cover_every_state_it_reports(
    hass, setup_integration
):
    """An enum sensor reporting a value outside its options logs an error and
    breaks long-term statistics."""
    state = hass.states.get(SEVERITY)
    options = state.attributes["options"]

    for severity in ("Extreme", "Severe", "Moderate", "Minor", "None"):
        assert severity in options


# ---------------------------------------------------------------------------
# Lightning
# ---------------------------------------------------------------------------


def _nearest_miles(hass) -> float:
    """The nearest-strike reading in miles, whatever unit it is displayed in."""
    state = hass.states.get(NEAREST)
    return DistanceConverter.convert(
        float(state.state),
        state.attributes["unit_of_measurement"],
        UnitOfLength.MILES,
    )


async def test_proximity_sensors_report_distance_not_box_membership(
    hass, setup_integration
):
    assert hass.states.get(RING_10).state == "0"
    assert hass.states.get(NEAREST).state == STATE_UNKNOWN

    await feed(hass, setup_integration, TOPIC_GLM_LEAF, encode(fresh_burst()))

    assert hass.states.get(RING_10).state == "2"
    # Distance is a device_class, so Home Assistant renders it in the viewer's
    # unit system — miles on an imperial install, kilometres on this one.
    assert _nearest_miles(hass) == pytest.approx(2.2, abs=0.2)
    assert float(hass.states.get(RATE).state) == pytest.approx(0.13, abs=0.02)
    assert float(hass.states.get(SINCE).state) < 1.0


async def test_nearby_binary_sensor_is_a_safety_device(hass, setup_integration):
    """This is the entity a seek-shelter automation triggers on."""
    nearby = hass.states.get(NEARBY)
    assert nearby.state == STATE_OFF
    assert nearby.attributes["device_class"] == "safety"

    await feed(hass, setup_integration, TOPIC_GLM_LEAF, encode(fresh_burst()))

    nearby = hass.states.get(NEARBY)
    assert nearby.state == STATE_ON
    # The attribute is always miles: it is raw data for automations, not a
    # rendered value, so it does not follow the unit system.
    assert nearby.attributes["nearest_mi"] == pytest.approx(2.2, abs=0.2)


async def test_a_strike_outside_the_alert_ring_does_not_arm_the_sensor(
    hass, setup_integration
):
    """20 miles away is inside the 30-mile radius and outside the 10-mile
    seek-shelter ring — the distinction the whole entity exists to make."""
    await feed(
        hass, setup_integration, TOPIC_GLM_LEAF, encode(fresh_burst(lat=30.35))
    )

    assert hass.states.get(NEARBY).state == STATE_OFF
    assert hass.states.get(RING_10).state == "0"
    assert hass.states.get("sensor.lightning_home_strikes_30mi").state == "2"


async def test_lightning_identity_survives_a_radius_change(
    hass, config_entry, mock_feed_client, enable_custom_integrations
):
    """The radius is an option. Deriving the unique_id from it would rename the
    entity to _2 and orphan the original every time someone tuned it, silently
    breaking any card or automation pointing at it."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    before = registry.async_get(NEAREST).unique_id

    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_GLM_RADIUS: 10}
    )
    await hass.async_block_till_done()

    assert registry.async_get(NEAREST).unique_id == before
    assert hass.states.get(f"{NEAREST}_2") is None
    config_entry.runtime_data._handle_connection(True)
    await hass.async_block_till_done()
    assert hass.states.get(NEAREST).attributes["radius_mi"] == 10


async def test_the_alert_ring_keeps_its_identity_when_retuned(
    hass, config_entry, mock_feed_client, enable_custom_integrations
):
    """The binary sensor's *name* carries the distance, so it is renamed — but
    its identity must not move, or every automation loses its trigger."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    before = registry.async_get(NEARBY).unique_id

    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, CONF_GLM_ALERT: 5}
    )
    await hass.async_block_till_done()

    assert registry.async_get(NEARBY).unique_id == before


async def test_every_zone_gets_its_own_proximity_set(
    hass, mock_feed_client, enable_custom_integrations
):
    """Two zones close enough to share coverage must both get sensors."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    neighbour = {
        **LOCATION_SANTA_ROSA,
        "zone_entity_id": "zone.work",
        "name": "Work",
        "latitude": 30.70,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home", "zone.work"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA, neighbour],
            CONF_ENABLE_ALERTS: False,
            CONF_ENABLE_GLM: True,
        },
        options={CONF_GLM_RADIUS: 30, CONF_GLM_ALERT: 10},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data._handle_connection(True)
    await hass.async_block_till_done()

    assert hass.states.get(NEAREST) is not None
    assert hass.states.get("sensor.lightning_work_nearest_strike") is not None


async def test_lightning_goes_unavailable_when_the_feed_goes_stale(
    hass, setup_integration
):
    """A confident zero during an outage looks exactly like calm weather."""
    await feed(hass, setup_integration, TOPIC_GLM_LEAF, encode(fresh_burst()))
    assert hass.states.get(RING_10).state == "2"

    await feed(
        hass,
        setup_integration,
        TOPIC_GLM_STATUS,
        encode({"healthy": True, "last_granule_age_seconds": 4000}),
    )

    assert hass.states.get(RING_10).state == STATE_UNAVAILABLE
    assert hass.states.get(NEARBY).state == STATE_UNAVAILABLE


async def test_lightning_does_not_touch_the_alert_entities(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_GLM_LEAF, encode(GLM_BURST))

    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF
    assert hass.states.get(COUNT).state == "0"


# ---------------------------------------------------------------------------
# Map markers
# ---------------------------------------------------------------------------


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
    assert marker.attributes["friendly_name"] == "Tornado Warning"
    assert marker.attributes["latitude"] == pytest.approx(30.64, abs=0.01)
    assert marker.attributes["longitude"] == pytest.approx(-87.06, abs=0.01)
    assert marker.attributes["severity"] == "Extreme"
    assert marker.attributes["geometry"] == TORNADO_ALERT["geometry"]
    assert marker.attributes["geometry_source"] == "polygon"


async def test_marker_geometry_is_kept_out_of_the_recorder(hass, setup_integration):
    """A county-union polygon is tens of kilobytes. Left recordable it blows
    the 16 KB attribute cap, and the recorder then stores none of the
    attributes at all and warns on every write."""
    from custom_components.wxalerts.geo_location import AlertMarker

    assert "geometry" in AlertMarker._unrecorded_attributes

    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))

    # Still present live — that is what the map card reads.
    assert geo_entities(hass)[0].attributes["geometry"] == TORNADO_ALERT["geometry"]


async def test_the_marker_vanishes_on_the_tombstone(hass, setup_integration):
    coordinator = setup_integration
    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    assert len(geo_entities(hass)) == 1

    await feed(hass, coordinator, TOPIC_TORNADO, TOMBSTONE)

    assert geo_entities(hass) == []


async def test_one_marker_per_hazard(hass, setup_integration):
    coordinator = setup_integration
    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    await feed(hass, coordinator, TOPIC_SEVERE, encode(SEVERE_ALERT))

    assert len(geo_entities(hass)) == 2


async def test_updating_a_hazard_does_not_duplicate_its_marker(hass, setup_integration):
    """Retained updates arrive repeatedly for a long-running warning."""
    coordinator = setup_integration
    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    for _ in range(3):
        await feed(
            hass, coordinator, TOPIC_TORNADO, encode({**TORNADO_ALERT, "action": "CON"})
        )

    assert len(geo_entities(hass)) == 1


async def test_a_hazard_with_no_geometry_gets_no_marker(hass, setup_integration):
    """``geometry_source: none`` alerts have nowhere to sit on a map."""
    await feed(hass, setup_integration, TOPIC_HEAT, encode(HEAT_ALERT))

    assert geo_entities(hass) == []
    # ...but the county entities still report it.
    assert hass.states.get("binary_sensor.wxalerts_cabin_active_alert").state == STATE_ON


async def test_a_moving_polygon_moves_the_marker(hass, setup_integration):
    """A tornado warning's polygon is re-issued as the storm tracks."""
    coordinator = setup_integration
    await feed(hass, coordinator, TOPIC_TORNADO, encode(TORNADO_ALERT))
    first = geo_entities(hass)[0].attributes["longitude"]

    moved = {
        **TORNADO_ALERT,
        "action": "CON",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [-86.60, 30.60],
                    [-86.50, 30.60],
                    [-86.50, 30.70],
                    [-86.60, 30.70],
                    [-86.60, 30.60],
                ]
            ],
        },
    }
    await feed(hass, coordinator, TOPIC_TORNADO, encode(moved))

    markers = geo_entities(hass)
    assert len(markers) == 1
    assert markers[0].attributes["longitude"] != first
    assert markers[0].attributes["longitude"] == pytest.approx(-86.56, abs=0.01)


# ---------------------------------------------------------------------------
# Feature toggles
# ---------------------------------------------------------------------------


async def test_lightning_only_setup_creates_no_alert_entities(
    hass, mock_feed_client, enable_custom_integrations
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: False,
            CONF_ENABLE_GLM: True,
        },
        options={CONF_GLM_RADIUS: 30, CONF_GLM_ALERT: 10},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data._handle_connection(True)
    await hass.async_block_till_done()

    assert hass.states.get(NEAREST) is not None
    assert hass.states.get(ACTIVE_ALERT) is None
    assert hass.states.get(SEVERITY) is None
    assert geo_entities(hass) == []


async def test_alerts_only_setup_creates_no_lightning_sensor(
    hass, mock_feed_client, enable_custom_integrations
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: False,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT) is not None
    assert hass.states.get(NEAREST) is None


async def test_selecting_nothing_creates_no_event_sensors(
    hass, mock_feed_client, enable_custom_integrations
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: False,
        },
        options={CONF_EVENT_SENSORS: []},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT) is not None
    assert hass.states.get(TORNADO_SENSOR) is None


async def test_two_zones_in_one_county_do_not_collide(
    hass, mock_feed_client, enable_custom_integrations
):
    """Duplicate SAME codes must not produce duplicate unique_ids, which HA
    rejects at registration."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home", "zone.work"],
            CONF_LOCATIONS: [
                LOCATION_SANTA_ROSA,
                {**LOCATION_SANTA_ROSA, "zone_entity_id": "zone.work", "name": "Work"},
            ],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT) is not None
    assert hass.states.get("binary_sensor.wxalerts_work_active_alert") is None


# ---------------------------------------------------------------------------
# Unload
# ---------------------------------------------------------------------------


async def test_unload_stops_the_feed_and_retires_the_entities(
    hass, config_entry, setup_integration, mock_feed_client
):
    """The MQTT task must be cancelled, not left running against a torn-down
    entry. Registry entities keep a restored placeholder, which is HA's own
    behaviour — what matters is that they no longer report live state."""
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_feed_client.return_value.stop.assert_awaited_once()

    state = hass.states.get(ACTIVE_ALERT)
    assert state.state == STATE_UNAVAILABLE
    assert state.attributes.get("restored") is True


async def test_reload_restores_the_entities(
    hass, config_entry, setup_integration, mock_feed_client
):
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    config_entry.runtime_data._handle_connection(True)
    await hass.async_block_till_done()

    assert hass.states.get(ACTIVE_ALERT).state == STATE_OFF


# ---------------------------------------------------------------------------
# Upgrading from the phenomenon buckets
# ---------------------------------------------------------------------------


async def test_an_upgraded_entry_gets_the_default_selection(
    hass, mock_feed_client, enable_custom_integrations
):
    """A 0.3.x entry has no selection at all, only the boolean that switched
    the buckets on. It has to come out of the upgrade with working sensors."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: False,
        },
        options={"phenomenon_sensors": True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(TORNADO_SENSOR) is not None
    assert hass.states.get(SEVERE_SENSOR) is not None


async def test_the_old_bucket_entities_are_removed_on_upgrade(
    hass, mock_feed_client, enable_custom_integrations
):
    """Nothing recreates them, so left in the registry they sit there as
    unavailable forever — and an automation still pointed at one looks fine
    while never firing again. Removing them makes the break visible now.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: False,
        },
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry.entry_id}_012113_phen_to",
        config_entry=entry,
        suggested_object_id="wxalerts_home_tornado_alert",
    )
    # Renamed by the user, so matching has to be on the unique_id.
    registry.async_update_entity(legacy.entity_id, new_entity_id="binary_sensor.twister")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get("binary_sensor.twister") is None
    assert hass.states.get(TORNADO_SENSOR) is not None
