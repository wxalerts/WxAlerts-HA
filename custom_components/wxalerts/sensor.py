"""Sensors: highest live severity and active-alert count per county, and a
lightning flash counter per configured geohash box.

The count sensor carries the full alert list in attributes — that is what a
Markdown card renders.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WxAlertsConfigEntry
from .const import (
    LOC_COUNTY,
    LOC_NAME,
    LOC_SAME,
    SEVERITY_NONE,
    SEVERITY_ORDER,
)
from .coordinator import WxAlertsCoordinator
from .entity import WxAlertsLocationEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WxAlertsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []

    if coordinator.enable_alerts:
        seen_same: set[str] = set()
        for location in coordinator.locations:
            same = location.get(LOC_SAME)
            if not same or same in seen_same:
                continue
            seen_same.add(same)
            entities.append(HighestSeveritySensor(coordinator, location))
            entities.append(AlertCountSensor(coordinator, location))

    if coordinator.enable_glm:
        seen_prefix: set[str] = set()
        for location in coordinator.locations:
            prefix = (location.get("geohash") or "")[: coordinator.glm_precision]
            if not prefix or prefix in seen_prefix:
                continue
            seen_prefix.add(prefix)
            entities.append(LightningCountSensor(coordinator, location))

    async_add_entities(entities)


class HighestSeveritySensor(WxAlertsLocationEntity, SensorEntity):
    """Extreme / Severe / Moderate / Minor / None — dashboard colour fuel."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [*SEVERITY_ORDER, SEVERITY_NONE]
    _attr_translation_key = "highest_severity"

    def __init__(
        self, coordinator: WxAlertsCoordinator, location: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, location)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self.same}_severity"

    @property
    def native_value(self) -> str:
        return self.coordinator.highest_severity(self.same)


class AlertCountSensor(WxAlertsLocationEntity, SensorEntity):
    """Number of live hazards; the full list rides in attributes."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "alert_count"
    _attr_native_unit_of_measurement = "alerts"

    def __init__(
        self, coordinator: WxAlertsCoordinator, location: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, location)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self.same}_count"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.alerts_for(self.same))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alerts = [
            {
                "id": alert.get("id"),
                "vtec": alert.get("vtec"),
                "event": alert.get("event"),
                "severity": alert.get("severity"),
                "urgency": alert.get("urgency"),
                "certainty": alert.get("certainty"),
                "headline": alert.get("headline"),
                "instruction": alert.get("instruction"),
                "onset": alert.get("onset"),
                "ends": alert.get("ends"),
                "action": alert.get("action"),
                "geometry_source": alert.get("geometry_source"),
            }
            for alert in self.coordinator.alerts_for(self.same)
        ]
        return {
            "same": self.same,
            "county_ugc": self.location.get(LOC_COUNTY),
            "location": self.location.get(LOC_NAME),
            "alerts": alerts,
        }


class LightningCountSensor(WxAlertsLocationEntity, SensorEntity):
    """GLM flashes in the configured geohash box over the rolling window.

    Flash *rate* is the meaningful severe-weather signal — GLM cannot tell
    cloud-to-ground from intra-cloud, so energy is deliberately not the
    state. A sharp rise in this value is the documented precursor.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "lightning_flashes"
    _attr_native_unit_of_measurement = "flashes"
    _attr_icon = "mdi:flash"

    def __init__(
        self, coordinator: WxAlertsCoordinator, location: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, location)
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_glm_{self.glm_prefix}"
        )

    @property
    def native_value(self) -> int:
        return self.coordinator.flash_count(self.glm_prefix)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.coordinator.last_flash_time(self.glm_prefix)
        return {
            "geohash": self.glm_prefix,
            "window_minutes": self.coordinator.glm_window_minutes,
            "last_flash": last.isoformat() if last else None,
            "location": self.location.get(LOC_NAME),
        }
