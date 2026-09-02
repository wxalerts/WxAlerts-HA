"""The WxAlerts integration.

Live NWS alerts and GOES GLM lightning from the wxalerts.org MQTT feed,
mapped onto Home Assistant zones.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import LEGACY_PHENOMENON_CODES
from .coordinator import WxAlertsCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.GEO_LOCATION,
]

WxAlertsConfigEntry = ConfigEntry[WxAlertsCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WxAlertsConfigEntry) -> bool:
    """Set up WxAlerts from a config entry."""
    _remove_legacy_phenomenon_entities(hass, entry)

    coordinator = WxAlertsCoordinator(hass, entry)
    entry.runtime_data = coordinator
    # Targets first: the platforms build one entity set per target, so they
    # have to exist before the platforms are forwarded.
    coordinator.prepare()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WxAlertsConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: WxAlertsConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _remove_legacy_phenomenon_entities(
    hass: HomeAssistant, entry: WxAlertsConfigEntry
) -> None:
    """Drop the six phenomenon-bucket sensors 0.3.x created.

    Nothing recreates them, so left alone they sit in the registry forever as
    unavailable entities — and an automation still pointed at one would look
    fine while never firing again. Removing them makes the break loud at the
    moment of upgrade, which is when someone can act on it.

    Matched on the unique_id suffix rather than the entity id, because the
    entity id is whatever the user renamed it to.
    """
    registry = er.async_get(hass)
    suffixes = tuple(f"_phen_{code.lower()}" for code in LEGACY_PHENOMENON_CODES)
    stale = [
        registry_entry.entity_id
        for registry_entry in er.async_entries_for_config_entry(
            registry, entry.entry_id
        )
        if registry_entry.unique_id.endswith(suffixes)
    ]
    for entity_id in stale:
        registry.async_remove(entity_id)
    if stale:
        _LOGGER.info(
            "Removed %d phenomenon-bucket sensors replaced by per-event-type "
            "sensors: %s",
            len(stale),
            ", ".join(stale),
        )
