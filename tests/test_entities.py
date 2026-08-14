"""Entities end to end: a message goes in at the MQTT layer and the test
asserts on what a user would see in Home Assistant.

Messages are handed to ``_handle_message`` because that is exactly what the
real ``FeedClient`` calls with the topic and raw payload — everything below
that line is the MQTT library's job, not this integration's.
"""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

from custom_components.wxalerts.const import (
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_GLM_PRECISION,
    CONF_LOCATIONS,
    CONF_PHENOMENON_SENSORS,
    CONF_ZONES,
    DOMAIN,
)

from .const import (
    GLM_BURST,
    HEAT_ALERT,
    LOCATION_AUTAUGA,
    LOCATION_SANTA_ROSA,
    SEVERE_ALERT,
    TOMBSTONE,
    TOPIC_GLM_LEAF,
    TOPIC_HEAT,
    TOPIC_SEVERE,
    TOPIC_TORNADO,
    TORNADO_ALERT,
    encode,
)

ACTIVE_ALERT = "binary_sensor.wxalerts_home_active_alert"
TORNADO_SENSOR = "binary_sensor.wxalerts_home_tornado_alert"
SEVERE_SENSOR = "binary_sensor.wxalerts_home_severe_thunderstorm_alert"
SEVERITY = "sensor.wxalerts_home_highest_severity"
COUNT = "sensor.wxalerts_home_active_alerts"
LIGHTNING = "sensor.wxalerts_home_lightning_flashes"


async def feed(hass, coordinator, topic, payload):
    """Deliver one MQTT message and let the entities settle."""
    await coordinator._handle_message(topic, payload)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Entity creation
# ---------------------------------------------------------------------------


async def test_entities_are_created_for_each_county(hass, setup_integration):
    for entity_id in (ACTIVE_ALERT, SEVERITY, COUNT, LIGHTNING):
        assert hass.states.get(entity_id) is not None, entity_id

    assert hass.states.get("binary_sensor.wxalerts_cabin_active_alert") is not None
    assert hass.states.get("sensor.wxalerts_cabin_highest_severity") is not None


async def test_dangerous_phenomenon_sensors_are_enabled_by_default(
    hass, setup_integration
):
    """Tornado, severe thunderstorm and flash flood are on; the slower
    hazards exist but stay out of the way until asked for."""
    registry = er.async_get(hass)

    for entity_id in (TORNADO_SENSOR, SEVERE_SENSOR):
        assert registry.async_get(entity_id).disabled is False
    assert hass.states.get(TORNADO_SENSOR) is not None

    winter = registry.async_get("binary_sensor.wxalerts_home_winter_storm_alert")
    assert winter is not None
    assert winter.disabled is True
    assert hass.states.get("binary_sensor.wxalerts_home_winter_storm_alert") is None


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
    assert hass.states.get(TORNADO_SENSOR).state == STATE_OFF


async def test_a_tornado_warning_lights_everything_up(hass, setup_integration):
    await feed(hass, setup_integration, TOPIC_TORNADO, encode(TORNADO_ALERT))

    assert hass.states.get(ACTIVE_ALERT).state == STATE_ON
    assert hass.states.get(SEVERITY).state == "Extreme"
    assert hass.states.get(COUNT).state == "1"
    assert hass.states.get(TORNADO_SENSOR).state == STATE_ON
    assert hass.states.get(SEVERE_SENSOR).state == STATE_OFF


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


async def test_lightning_sensor_counts_flashes(hass, setup_integration):
    assert hass.states.get(LIGHTNING).state == "0"

    await feed(hass, setup_integration, TOPIC_GLM_LEAF, encode(GLM_BURST))

    state = hass.states.get(LIGHTNING)
    assert state.state == "2"
    assert state.attributes["geohash"] == "dj6"
    assert state.attributes["window_minutes"] == 15
    assert state.attributes["last_flash"] == "2026-08-13T23:57:44.695584+00:00"
    assert state.attributes["unit_of_measurement"] == "flashes"


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
        options={CONF_GLM_PRECISION: 3},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data._handle_connection(True)
    await hass.async_block_till_done()

    assert hass.states.get(LIGHTNING) is not None
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
    assert hass.states.get(LIGHTNING) is None


async def test_phenomenon_sensors_can_be_turned_off(
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
        options={CONF_PHENOMENON_SENSORS: False},
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
