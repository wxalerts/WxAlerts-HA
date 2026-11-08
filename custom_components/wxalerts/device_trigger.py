"""Device triggers: every NWS event type, plus storm cell episodes, on every
place, with no setup.

The binary sensors cover what belongs on a dashboard or in a condition, but
making all 122 event types available that way would mean 122 entities per
place in the registry forever. A device trigger costs nothing until it is
used, so the automation editor can offer the whole catalogue: pick the place
device, pick "Tornado Warning begins", done.

Hazard triggers fire off ``EVENT_HAZARD``, which the coordinator emits when an
event TYPE starts or stops being in effect for a place, not once per product.
Storm cell triggers fire off ``EVENT_STORM_CELL`` on open and escalate.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_EVENT_DATA,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_PLACES,
    DOMAIN,
    EPISODE_ESCALATE,
    EPISODE_OPEN,
    EVENT_HAZARD,
    EVENT_STORM_CELL,
    HAZARD_BEGIN,
    HAZARD_END,
    STORM_EPISODE,
    STORM_PLACE_ID,
)
from .coordinator import Place, parse_places, place_device_identifier
from .events import EVENT_TYPES

# "<key>_begin" / "<key>_end": one trigger type per catalogue entry per edge.
HAZARD_TRIGGER_TYPES: list[str] = [
    f"{event.key}_{edge}"
    for event in EVENT_TYPES
    for edge in (HAZARD_BEGIN, HAZARD_END)
]
# Storm cell episodes that fire EVENT_STORM_CELL.
STORM_TRIGGER_TYPES: dict[str, str] = {
    "storm_cell_open": EPISODE_OPEN,
    "storm_cell_escalate": EPISODE_ESCALATE,
}
TRIGGER_TYPES: set[str] = {*HAZARD_TRIGGER_TYPES, *STORM_TRIGGER_TYPES}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


def _split(trigger_type: str) -> tuple[str, str]:
    """"tornado_warning_begin" -> ("tornado_warning", "begin").

    Split from the right, because catalogue keys contain underscores of their
    own: "911_telephone_outage_begin" is not a key called "911".
    """
    key, _, edge = trigger_type.rpartition("_")
    return key, edge


def _place_for_device(hass: HomeAssistant, device_id: str) -> Place | None:
    """The place a device stands for, from the entry's cached places list.

    Read from entry data rather than the running coordinator, so the
    automation editor can list triggers even while the entry is not loaded.
    """
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        places = parse_places(entry.data.get(CONF_PLACES)) or {}
        for place in places.values():
            if place_device_identifier(entry_id, place.id) in device.identifiers:
                return place
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Storm cell triggers on every place; hazard triggers where there is a
    county feed behind the place, since elsewhere they could never fire."""
    place = _place_for_device(hass, device_id)
    if place is None:
        return []
    base = {CONF_PLATFORM: "device", CONF_DEVICE_ID: device_id, CONF_DOMAIN: DOMAIN}
    types = list(STORM_TRIGGER_TYPES)
    if place.same:
        types.extend(HAZARD_TRIGGER_TYPES)
    return [{**base, CONF_TYPE: trigger_type} for trigger_type in types]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Listen for the one transition this trigger describes."""
    place = _place_for_device(hass, config[CONF_DEVICE_ID])
    if place is None:
        raise HomeAssistantError(
            "This WxAlerts device is not one of your places, so its triggers "
            "cannot fire"
        )
    trigger_type = config[CONF_TYPE]
    if trigger_type in STORM_TRIGGER_TYPES:
        event_type = EVENT_STORM_CELL
        event_data = {
            STORM_PLACE_ID: place.id,
            STORM_EPISODE: STORM_TRIGGER_TYPES[trigger_type],
        }
    else:
        key, edge = _split(trigger_type)
        event_type = EVENT_HAZARD
        event_data = {"place_id": place.id, "event_type": key, "action": edge}

    # Filtering in the event trigger's own data match, rather than in a
    # callback here, keeps the whole thing inside HA's trigger machinery,
    # including the trace an automation shows when it fires.
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: event_type,
            CONF_EVENT_DATA: event_data,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
