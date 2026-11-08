"""Device triggers: the whole catalogue plus storm cell episodes on every place
device, with no configuration and no entities.

These go through ``async_get_device_automations`` and a real automation rather
than calling the platform functions directly: a device trigger that lists
correctly but never fires is the failure mode worth catching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.components import automation
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_get_device_automations,
)

from custom_components.wxalerts.coordinator import entry_device
from custom_components.wxalerts.const import DOMAIN

from .conftest import make_entry, start
from .const import (
    CACHED_PLACES,
    TOMBSTONE,
    TOPIC_TORNADO,
    TORNADO_ALERT,
    encode,
    storm,
    storm_topic,
)


def _device_id(hass, entry, key: str) -> str:
    device = entry_device(hass, entry.entry_id, (DOMAIN, f"{entry.entry_id}_{key}"))
    assert device is not None, key
    return device.id


@pytest.fixture
async def trigger_setup(hass, mock_feed_client, enable_custom_integrations):
    """Home and Work in one county, plus an offshore Boat with no county."""
    await async_setup_component(hass, "homeassistant", {})
    places = [
        CACHED_PLACES[0],
        {**CACHED_PLACES[0], "id": "104", "name": "Work"},
        {"id": "103", "name": "Boat", "lat": 29.0, "lon": -87.5, "same": None},
    ]
    entry = make_entry(places=places)
    coordinator = await start(hass, entry)
    return entry, coordinator


async def _domain_triggers(hass, device_id):
    triggers = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    return [t for t in triggers if t["domain"] == DOMAIN]


async def test_the_whole_catalogue_is_offered_on_a_place(hass, trigger_setup):
    entry, _ = trigger_setup
    from custom_components.wxalerts.events import EVENT_TYPES

    types = {t["type"] for t in await _domain_triggers(hass, _device_id(hass, entry, "place_101"))}
    assert "tornado_warning_begin" in types
    assert "tornado_warning_end" in types
    assert "storm_cell_open" in types
    assert "storm_cell_escalate" in types
    assert len(types) == len(EVENT_TYPES) * 2 + 2


async def test_a_place_with_no_county_offers_only_storm_triggers(hass, trigger_setup):
    entry, _ = trigger_setup
    types = {t["type"] for t in await _domain_triggers(hass, _device_id(hass, entry, "place_103"))}
    assert types == {"storm_cell_open", "storm_cell_escalate"}


async def test_the_feed_device_offers_none(hass, trigger_setup):
    entry, _ = trigger_setup
    assert await _domain_triggers(hass, _device_id(hass, entry, "service")) == []


async def test_every_offered_trigger_has_a_translated_name(hass, trigger_setup):
    from custom_components.wxalerts.device_trigger import TRIGGER_TYPES

    strings = json.loads(
        (
            Path(__file__).parent.parent / "custom_components/wxalerts/strings.json"
        ).read_text()
    )
    names = strings["device_automation"]["trigger_type"]
    assert TRIGGER_TYPES <= set(names)


def _automation(device_id, trigger_type, tag):
    return {
        "trigger": {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": device_id,
            "type": trigger_type,
        },
        "action": {"event": "caught", "event_data": {"tag": tag}},
    }


async def test_hazard_triggers_fire_per_place(hass, trigger_setup):
    entry, coordinator = trigger_setup
    home = _device_id(hass, entry, "place_101")
    work = _device_id(hass, entry, "place_104")
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                _automation(home, "tornado_warning_begin", "home_begin"),
                _automation(home, "tornado_warning_end", "home_end"),
                _automation(work, "tornado_warning_begin", "work_begin"),
                # Must stay silent: a watch and a warning are different types.
                _automation(home, "tornado_watch_begin", "watch"),
            ]
        },
    )
    caught = async_capture_events(hass, "caught")

    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await hass.async_block_till_done()
    assert sorted(e.data["tag"] for e in caught) == ["home_begin", "work_begin"]

    await coordinator._handle_message(TOPIC_TORNADO, TOMBSTONE)
    await hass.async_block_till_done()
    assert sorted(e.data["tag"] for e in caught) == [
        "home_begin",
        "home_end",
        "work_begin",
    ]


async def test_storm_cell_triggers_fire_for_their_place_only(hass, trigger_setup):
    entry, coordinator = trigger_setup
    home = _device_id(hass, entry, "place_101")
    work = _device_id(hass, entry, "place_104")
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                _automation(home, "storm_cell_open", "home_open"),
                _automation(home, "storm_cell_escalate", "home_escalate"),
                _automation(work, "storm_cell_open", "work_open"),
            ]
        },
    )
    caught = async_capture_events(hass, "caught")

    await coordinator._handle_message(storm_topic(101), encode(storm()))
    await hass.async_block_till_done()
    await coordinator._handle_message(
        storm_topic(101), encode(storm(episode="escalate", rung=70))
    )
    await hass.async_block_till_done()

    assert [e.data["tag"] for e in caught] == ["home_open", "home_escalate"]


async def test_a_trigger_type_splits_on_the_last_underscore(hass):
    from custom_components.wxalerts.device_trigger import _split

    assert _split("tornado_warning_begin") == ("tornado_warning", "begin")
    assert _split("911_telephone_outage_end") == ("911_telephone_outage", "end")
