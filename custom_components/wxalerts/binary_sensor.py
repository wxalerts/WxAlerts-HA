"""Binary sensor platform for WxAlerts — one sensor per monitored zone."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DOMAIN,
    SEVERITY_ORDER,
    TOPIC_PATTERN,
    URGENCY_ORDER,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WxAlerts binary sensors from a config entry."""
    mqtt_client = hass.data[DOMAIN][entry.entry_id]["mqtt_client"]
    zones: list[dict] = entry.data.get(CONF_ZONES, [])

    entities = []
    for zone in zones:
        entity = WxAlertsZoneSensor(hass, mqtt_client, zone)
        entities.append(entity)

    async_add_entities(entities, update_before_add=True)


class WxAlertsZoneSensor(BinarySensorEntity):
    """Binary sensor representing NWS alert state for a single UGC zone.

    State is ON when one or more alerts are active for the zone.
    Attributes expose all active alerts plus worst-case severity/urgency
    for use in automations.
    """

    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_client: Any,
        zone: dict,
    ) -> None:
        self._hass = hass
        self._mqtt_client = mqtt_client
        self._zone_id: str = zone[CONF_ZONE_ID]
        self._zone_name: str = zone.get(CONF_ZONE_NAME, self._zone_id)
        self._state: str = zone.get("state", "")

        self._alerts: list[dict] = []
        self._attr_is_on = False
        self._attr_unique_id = f"wxalerts_{self._zone_id.lower()}"
        self._attr_name = f"{self._zone_id} — {self._zone_name}"

        topic = TOPIC_PATTERN.format(
            state=self._state, zone=self._zone_id
        )
        self._topic = topic

    async def async_added_to_hass(self) -> None:
        """Subscribe to MQTT topic when entity is added."""
        self._mqtt_client.subscribe(self._topic, self._on_mqtt_message)
        _LOGGER.debug("WxAlerts sensor subscribed to %s", self._topic)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe when entity is removed."""
        self._mqtt_client.unsubscribe(self._topic)

    @callback
    def _on_mqtt_message(self, topic: str, payload: dict | None) -> None:
        """Handle incoming MQTT message for this zone."""
        if payload is None:
            # Tombstone — clear all alerts
            _LOGGER.debug("Tombstone received for zone %s", self._zone_id)
            self._alerts = []
        else:
            nws_id = payload.get("nws_id")
            if not nws_id:
                _LOGGER.warning(
                    "Alert missing nws_id on topic %s, skipping", topic
                )
                return

            # Update existing alert or append new one
            existing = next(
                (a for a in self._alerts if a.get("nws_id") == nws_id), None
            )
            if existing:
                existing.update(payload)
            else:
                self._alerts.append(payload)

            # Prune expired alerts
            self._prune_expired()

        self._attr_is_on = len(self._alerts) > 0
        self.schedule_update_ha_state()

    def _prune_expired(self) -> None:
        """Remove alerts whose expires timestamp has passed."""
        from datetime import timezone
        now = datetime.now(timezone.utc)
        active = []
        for alert in self._alerts:
            expires_str = alert.get("expires")
            if not expires_str:
                active.append(alert)
                continue
            try:
                expires_str_clean = expires_str.replace("Z", "+00:00")
                expires_dt = datetime.fromisoformat(expires_str_clean)
                if expires_dt.tzinfo is None:
                    expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                if expires_dt > now:
                    active.append(alert)
                else:
                    _LOGGER.debug(
                        "Pruned expired alert %s from zone %s",
                        alert.get("nws_id"),
                        self._zone_id,
                    )
            except (ValueError, TypeError):
                active.append(alert)
        self._alerts = active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all active alerts plus worst-case summary attributes."""
        if not self._alerts:
            return {
                "zone_id": self._zone_id,
                "zone_name": self._zone_name,
                "alert_count": 0,
                "alerts": [],
                "worst_severity": None,
                "worst_urgency": None,
                "worst_event": None,
            }

        worst = max(
            self._alerts,
            key=lambda a: (
                SEVERITY_ORDER.get(a.get("severity", "Unknown"), 0),
                URGENCY_ORDER.get(a.get("urgency", "Unknown"), 0),
            ),
        )

        return {
            "zone_id": self._zone_id,
            "zone_name": self._zone_name,
            "alert_count": len(self._alerts),
            "alerts": self._alerts,
            "worst_severity": worst.get("severity"),
            "worst_urgency": worst.get("urgency"),
            "worst_event": worst.get("event"),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        """Group sensors by integration for the HA device registry."""
        return {
            "identifiers": {(DOMAIN, self._zone_id)},
            "name": f"WxAlerts — {self._zone_name}",
            "manufacturer": "WxAlerts Inc.",
            "model": "NWS Zone Monitor",
            "configuration_url": "https://wxalerts.org",
        }
