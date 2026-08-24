"""Sensors: highest live severity and active-alert count per county, and the
lightning proximity set per target.

The alert count sensor carries the full alert list in attributes — that is what
a Markdown card renders. The lightning sensors answer the only question worth
asking of a lightning feed: how close, how many, how fast, and which way is it
moving.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WxAlertsConfigEntry
from .const import (
    LOC_COUNTY,
    LOC_NAME,
    LOC_SAME,
    SEVERITY_NONE,
    SEVERITY_ORDER,
    TREND_OPTIONS,
)
from .coordinator import WxAlertsCoordinator
from .entity import WxAlertsLocationEntity, WxAlertsTargetEntity
from .lightning import Target


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
        # One set per target — a configured zone, or a tracked person. Identity
        # is the zone or person entity id, which does not move when a setting
        # changes; keying on the geohash box (as an earlier release did) renamed
        # every entity whenever the box size was edited.
        entities.extend(_lightning_sensors(coordinator, coordinator.targets))

        @callback
        def _targets_added(targets: list[Target]) -> None:
            # A person who gains a GPS fix after setup is a new target; without
            # this they would get no entities until the next restart.
            async_add_entities(_lightning_sensors(coordinator, targets))

        entry.async_on_unload(coordinator.add_target_listener(_targets_added))

    async_add_entities(entities)


def _lightning_sensors(
    coordinator: WxAlertsCoordinator, targets: list[Target]
) -> list[SensorEntity]:
    entities: list[SensorEntity] = []
    for target in targets:
        entities.append(NearestStrikeSensor(coordinator, target))
        entities.append(StrikeRateSensor(coordinator, target))
        entities.append(SinceLastStrikeSensor(coordinator, target))
        entities.append(LightningTrendSensor(coordinator, target))
        entities.extend(
            RingCountSensor(coordinator, target, ring)
            for ring in coordinator.glm_rings
        )
    return entities


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


class _LightningSensor(WxAlertsTargetEntity, SensorEntity):
    """Common attributes for every proximity sensor."""

    _attr_icon = "mdi:flash"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        target = self.target
        return {
            "target": self._target_name,
            "target_kind": target.kind if target else None,
            "window_minutes": self.coordinator.glm_window_minutes,
            "radius_mi": self.coordinator.glm_radius_mi,
        }


class NearestStrikeSensor(_LightningSensor):
    """Distance to the closest flash in the window.

    Unknown rather than the radius when nothing has been seen: "no lightning
    within 30 miles" and "lightning exactly 30 miles away" are different
    answers, and a number cannot carry both.
    """

    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.MILES
    _attr_translation_key = "nearest_strike"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: WxAlertsCoordinator, target: Target) -> None:
        super().__init__(coordinator, target)
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_glm_{target.key}_nearest"
        )

    @property
    def native_value(self) -> float | None:
        return self.metrics.nearest_mi

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **super().extra_state_attributes,
            "closest_flashes": self.coordinator.recent_flashes(self.target_key, 10),
        }


class RingCountSensor(_LightningSensor):
    """Flashes inside one ring over the window."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "flashes"

    def __init__(
        self, coordinator: WxAlertsCoordinator, target: Target, ring: int
    ) -> None:
        super().__init__(coordinator, target)
        self._ring = ring
        self._attr_name = f"Strikes {ring}mi"
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_glm_{target.key}_ring{ring}"
        )

    @property
    def native_value(self) -> int:
        return self.metrics.rings.get(self._ring, 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**super().extra_state_attributes, "ring_mi": self._ring}


class StrikeRateSensor(_LightningSensor):
    """Flashes per minute inside the outer radius.

    Rate is the severe-weather signal, not energy: GLM cannot tell
    cloud-to-ground from intra-cloud, so a big energy number says nothing about
    danger, while a sharp rise in rate is the documented precursor.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "flashes/min"
    _attr_translation_key = "strike_rate"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: WxAlertsCoordinator, target: Target) -> None:
        super().__init__(coordinator, target)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_glm_{target.key}_rate"

    @property
    def native_value(self) -> float:
        return self.metrics.rate_per_min


class SinceLastStrikeSensor(_LightningSensor):
    """Minutes since the most recent flash inside the radius.

    This is what a 30-30 style all-clear is built on, so it stays a number in
    minutes rather than a timestamp an automation would have to subtract.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_translation_key = "since_last_strike"
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: WxAlertsCoordinator, target: Target) -> None:
        super().__init__(coordinator, target)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_glm_{target.key}_last"

    @property
    def native_value(self) -> float | None:
        return self.metrics.last_strike_min

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        last = self.metrics.last_flash_at
        return {
            **super().extra_state_attributes,
            "last_flash": last.isoformat() if last else None,
        }


class LightningTrendSensor(_LightningSensor):
    """Approaching / Receding / Steady / Unknown."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = TREND_OPTIONS
    _attr_translation_key = "lightning_trend"
    _attr_icon = "mdi:trending-up"

    def __init__(self, coordinator: WxAlertsCoordinator, target: Target) -> None:
        super().__init__(coordinator, target)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_glm_{target.key}_trend"

    @property
    def native_value(self) -> str:
        return self.metrics.trend
