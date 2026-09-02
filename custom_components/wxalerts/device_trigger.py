"""Device triggers: every NWS event type, on every location, with no setup.

The binary sensors cover what belongs on a dashboard or in a condition, but
making all 122 event types available that way would mean 122 entities per
county in the registry forever. A device trigger costs nothing until it is
used, so the automation editor can offer the whole catalogue: pick the county
device, pick "Tornado Warning begins", done.

These fire off ``EVENT_HAZARD``, which the coordinator emits when an event
TYPE starts or stops being in effect — not once per product. A second tornado
warning for the same county while one is already live is a new alert but not a
new hazard, and a trigger firing on it would announce a tornado that is already
being announced.
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
    DOMAIN,
    EVENT_HAZARD,
    HAZARD_BEGIN,
    HAZARD_END,
    LOC_SAME,
)
from .events import EVENT_TYPES

# "<key>_begin" / "<key>_end" — one trigger type per catalogue entry per edge.
TRIGGER_TYPES: set[str] = {
    f"{event.key}_{edge}"
    for event in EVENT_TYPES
    for edge in (HAZARD_BEGIN, HAZARD_END)
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)


def _split(trigger_type: str) -> tuple[str, str]:
    """"tornado_warning_begin" -> ("tornado_warning", "begin").

    Split from the right, because catalogue keys contain underscores of their
    own — "911_telephone_outage_begin" is not a key called "911".
    """
    key, _, edge = trigger_type.rpartition("_")
    return key, edge


def _same_for_device(hass: HomeAssistant, device_id: str) -> str | None:
    """The county code the device stands for.

    Device identifiers are ``<entry_id>_<key>``, where the key is the SAME code
    for any county device. Rather than parse that back out, the entry's own
    locations are searched — the identifier format is an implementation detail
    of the entity base class and should stay one.
    """
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        options = {**entry.data, **entry.options}
        for location in options.get("locations", []):
            same = location.get(LOC_SAME)
            if same and (DOMAIN, f"{entry_id}_{same}") in device.identifiers:
                return same
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Offer the catalogue — but only on devices that receive alerts.

    A lightning device, or a zone that resolved to no US county, has no county
    feed behind it; listing hazard triggers there would offer automations that
    can never fire.
    """
    if _same_for_device(hass, device_id) is None:
        return []
    base = {CONF_PLATFORM: "device", CONF_DEVICE_ID: device_id, CONF_DOMAIN: DOMAIN}
    return [
        {**base, CONF_TYPE: f"{event.key}_{edge}"}
        for event in EVENT_TYPES
        for edge in (HAZARD_BEGIN, HAZARD_END)
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Listen for the one hazard transition this trigger describes."""
    same = _same_for_device(hass, config[CONF_DEVICE_ID])
    if same is None:
        raise HomeAssistantError(
            "This WxAlerts device has no county feed, so hazard triggers "
            "cannot fire for it"
        )
    event_type, edge = _split(config[CONF_TYPE])

    # Filtering in the event trigger's own data match, rather than in a
    # callback here, keeps the whole thing inside HA's trigger machinery —
    # including the trace an automation shows when it fires.
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: EVENT_HAZARD,
            CONF_EVENT_DATA: {
                "same": same,
                "event_type": event_type,
                "action": edge,
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
