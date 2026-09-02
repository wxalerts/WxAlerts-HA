"""Device triggers: the whole catalogue in the automation editor, with no
configuration and no entities.

These go through ``async_get_device_automations`` and a real automation rather
than calling the platform functions directly — a device trigger that lists
correctly but never fires is the failure mode worth catching, and only driving
the automation engine catches it.
"""

from __future__ import annotations

import pytest
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
)

from custom_components.wxalerts.const import (
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_LOCATIONS,
    CONF_ZONES,
    DOMAIN,
)

from .const import (
    LOCATION_OFFSHORE,
    LOCATION_SANTA_ROSA,
    SAME_SANTA_ROSA,
    TOMBSTONE,
    TOPIC_TORNADO,
    TORNADO_ALERT,
    encode,
)


def _device_id(hass, entry, key: str) -> str:
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{entry.entry_id}_{key}")}
    )
    assert device is not None, key
    return device.id


@pytest.fixture
async def trigger_setup(hass, mock_feed_client, enable_custom_integrations):
    """One county zone and one offshore zone, so both branches are reachable."""
    await async_setup_component(hass, "homeassistant", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home", "zone.boat"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA, LOCATION_OFFSHORE],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    coordinator._handle_connection(True)
    coordinator._priming_until = 0.0
    await hass.async_block_till_done()
    return entry, coordinator


async def test_the_whole_catalogue_is_offered(hass, trigger_setup):
    entry, _ = trigger_setup
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, _device_id(hass, entry, SAME_SANTA_ROSA)
    )
    types = {trigger["type"] for trigger in triggers if trigger["domain"] == DOMAIN}

    from custom_components.wxalerts.events import EVENT_TYPES

    assert len(types) == len(EVENT_TYPES) * 2
    for expected in (
        "tornado_warning_begin",
        "tornado_warning_end",
        "blizzard_warning_begin",
        "severe_thunderstorm_watch_begin",
    ):
        assert expected in types


async def test_every_offered_trigger_has_a_translated_name(hass, trigger_setup):
    """A missing string renders as the raw key in the automation editor."""
    import json
    import pathlib

    strings = json.loads(
        pathlib.Path(
            "custom_components/wxalerts/strings.json"
        ).read_text()
    )["device_automation"]["trigger_type"]

    entry, _ = trigger_setup
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, _device_id(hass, entry, SAME_SANTA_ROSA)
    )
    for trigger in triggers:
        if trigger["domain"] == DOMAIN:
            assert trigger["type"] in strings, trigger["type"]


async def test_a_device_with_no_county_feed_offers_none(hass, trigger_setup):
    """The lightning devices are the ones a user will scroll past in the
    picker. They carry no county feed, so hazard triggers on them could never
    fire and offering them would be a trap.

    The offshore zone is here for the same reason at the location level: it
    resolved to no SAME code, so it gets a lightning device and no county one.
    """
    entry, _ = trigger_setup
    for key in ("lightning_zone.boat", "lightning_zone.home"):
        triggers = await async_get_device_automations(
            hass, DeviceAutomationType.TRIGGER, _device_id(hass, entry, key)
        )
        assert [t for t in triggers if t["domain"] == DOMAIN] == [], key


async def test_the_trigger_fires_on_its_own_hazard_only(hass, trigger_setup):
    entry, coordinator = trigger_setup
    device_id = _device_id(hass, entry, SAME_SANTA_ROSA)

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": device_id,
                        "type": "tornado_warning_begin",
                    },
                    "action": {
                        "event": "caught",
                        "event_data": {"edge": "begin"},
                    },
                },
                {
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": device_id,
                        "type": "tornado_warning_end",
                    },
                    "action": {
                        "event": "caught",
                        "event_data": {"edge": "end"},
                    },
                },
                {
                    # Must stay silent throughout: a watch is not a warning.
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": device_id,
                        "type": "tornado_watch_begin",
                    },
                    "action": {
                        "event": "caught",
                        "event_data": {"edge": "watch"},
                    },
                },
            ]
        },
    )

    from pytest_homeassistant_custom_component.common import async_capture_events

    caught = async_capture_events(hass, "caught")

    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await hass.async_block_till_done()
    assert [event.data["edge"] for event in caught] == ["begin"]

    await coordinator._handle_message(TOPIC_TORNADO, TOMBSTONE)
    await hass.async_block_till_done()
    assert [event.data["edge"] for event in caught] == ["begin", "end"]


async def test_a_trigger_type_splits_on_the_last_underscore(hass):
    """Catalogue keys contain underscores, so the edge has to come off the
    right-hand end — "911_telephone_outage_begin" is not a key called "911"."""
    from custom_components.wxalerts.device_trigger import _split

    assert _split("tornado_warning_begin") == ("tornado_warning", "begin")
    assert _split("911_telephone_outage_end") == ("911_telephone_outage", "end")
