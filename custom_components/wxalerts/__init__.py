"""WxAlerts Home Assistant Integration.

Provides real-time NWS weather alert binary sensors via the WxAlerts
MQTT broker (mqtt.wxalerts.org), independent of HA's built-in MQTT
integration.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .mqtt_client import WxAlertsMQTTClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up WxAlerts from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    mqtt_client = WxAlertsMQTTClient()

    # Start MQTT client in executor to avoid blocking event loop
    await hass.async_add_executor_job(mqtt_client.start)

    hass.data[DOMAIN][entry.entry_id] = {
        "mqtt_client": mqtt_client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("WxAlerts integration loaded for entry %s", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and stop the MQTT client."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        mqtt_client: WxAlertsMQTTClient = data["mqtt_client"]
        await hass.async_add_executor_job(mqtt_client.stop)
        _LOGGER.info("WxAlerts integration unloaded for entry %s", entry.entry_id)

    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options updates — reload the entry to pick up zone changes."""
    await hass.config_entries.async_reload(entry.entry_id)
