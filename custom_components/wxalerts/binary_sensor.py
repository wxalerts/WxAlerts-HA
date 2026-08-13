"""Binary sensors: any-active-alert per county, plus optional
per-phenomenon sensors (tornado, severe thunderstorm, ...) that read far
better in automations than string-matching attributes.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WxAlertsConfigEntry
from .const import (
    CONF_PHENOMENON_SENSORS,
    DEFAULT_PHENOMENON_SENSORS,
    LOC_COUNTY,
    LOC_SAME,
    PHENOMENON_SENSOR_TYPES,
)
from .coordinator import WxAlertsCoordinator
from .entity import WxAlertsLocationEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WxAlertsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if not coordinator.enable_alerts:
        return

    options = {**entry.data, **entry.options}
    phenomenon_sensors = options.get(
        CONF_PHENOMENON_SENSORS, DEFAULT_PHENOMENON_SENSORS
    )

    entities: list[BinarySensorEntity] = []
    seen_same: set[str] = set()
    for location in coordinator.locations:
        same = location.get(LOC_SAME)
        if not same or same in seen_same:
            continue
        seen_same.add(same)
        entities.append(ActiveAlertBinarySensor(coordinator, location))
        if phenomenon_sensors:
            entities.extend(
                PhenomenonBinarySensor(coordinator, location, code, label)
                for code, label in PHENOMENON_SENSOR_TYPES.items()
            )
    async_add_entities(entities)


class ActiveAlertBinarySensor(WxAlertsLocationEntity, BinarySensorEntity):
    """On when any hazard is live for this county."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_translation_key = "active_alert"

    def __init__(
        self, coordinator: WxAlertsCoordinator, location: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, location)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self.same}_active"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.alerts_for(self.same))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alerts = self.coordinator.alerts_for(self.same)
        return {
            "same": self.same,
            "county_ugc": self.location.get(LOC_COUNTY),
            "alert_count": len(alerts),
            "events": [alert.get("event") for alert in alerts],
        }


class PhenomenonBinarySensor(WxAlertsLocationEntity, BinarySensorEntity):
    """On when a specific phenomenon (TO, SV, FF, ...) is live."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(
        self,
        coordinator: WxAlertsCoordinator,
        location: dict[str, Any],
        phenomenon: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, location)
        self._phenomenon = phenomenon
        self._attr_name = f"{label} alert"
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{self.same}_phen_{phenomenon.lower()}"
        )
        self._attr_entity_registry_enabled_default = phenomenon in ("TO", "SV", "FF")

    @property
    def is_on(self) -> bool:
        return self.coordinator.has_phenomenon(self.same, self._phenomenon)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        live = [
            {
                "event": alert.get("event"),
                "significance": alert.get("significance"),
                "headline": alert.get("headline"),
                "ends": alert.get("ends"),
            }
            for alert in self.coordinator.alerts_for(self.same)
            if alert.get("phenomena") == self._phenomenon
        ]
        return {"phenomenon": self._phenomenon, "alerts": live}
