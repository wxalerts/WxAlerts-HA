"""The WxAlerts integration.

NWS alerts and storm cell alerts for the places on a WxAlerts account,
delivered over the wxalerts.org MQTT feed with a per-account broker login.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_EVENT_SENSORS, CONF_PLACES, CONFIG_VERSION, DOMAIN
from .coordinator import WxAlertsCoordinator, place_device_identifier
from .options import selected_event_keys

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.GEO_LOCATION,
]

WxAlertsConfigEntry = ConfigEntry[WxAlertsCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WxAlertsConfigEntry) -> bool:
    """Set up WxAlerts from a config entry."""
    if not entry.data.get(CONF_USERNAME) or not entry.data.get(CONF_PASSWORD):
        # An entry migrated from 0.x used the shared public login and has no
        # account behind it. Home Assistant turns this into a re-authenticate
        # prompt asking for the new broker login.
        raise ConfigEntryAuthFailed(
            "WxAlerts 1.0 needs the Home Assistant login from your wxalerts.org "
            "account"
        )

    coordinator = WxAlertsCoordinator(hass, entry)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WxAlertsConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: WxAlertsConfigEntry) -> None:
    """Reload when the options change.

    The places list is cached in entry data and rewritten whenever the person
    edits their places; that is handled live and must not reload anything.
    """
    coordinator = entry.runtime_data
    if dict(entry.options) != coordinator.options_snapshot:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: WxAlertsConfigEntry, device: DeviceEntry
) -> bool:
    """Allow deleting a device whose place no longer exists on the account."""
    coordinator = entry.runtime_data
    return not any(
        place_device_identifier(entry.entry_id, place_id) in device.identifiers
        for place_id in coordinator.places
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move a 0.x entry onto places and a per-account login.

    Everything 0.x registered is keyed on a Home Assistant zone, a county or a
    lightning target, and none of it maps onto a WxAlerts place. Leaving those
    entities in the registry would show them as unavailable forever, and an
    automation pointed at one would look fine while never firing again, so they
    are removed here, at the moment someone is looking.

    The new entry has no broker login yet. Setup then raises
    ConfigEntryAuthFailed, which asks for it instead of failing silently.
    """
    if entry.version > CONFIG_VERSION:
        # A downgrade from a future release: refuse rather than guess.
        return False

    if entry.version == 1:
        # Carry the alert type selection over; it still means the same thing.
        event_keys = selected_event_keys(entry)

        entity_registry = er.async_get(hass)
        stale = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        for registry_entry in stale:
            entity_registry.async_remove(registry_entry.entity_id)

        device_registry = dr.async_get(hass)
        devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for device in devices:
            device_registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )

        hass.config_entries.async_update_entry(
            entry,
            data={CONF_PLACES: []},
            options={CONF_EVENT_SENSORS: event_keys},
            version=CONFIG_VERSION,
            minor_version=1,
        )
        _LOGGER.info(
            "Migrated WxAlerts from 0.x: removed %d zone and lightning entities "
            "and %d devices. Enter the Home Assistant login from your "
            "wxalerts.org account to finish the upgrade",
            len(stale),
            len(devices),
        )

    return True
