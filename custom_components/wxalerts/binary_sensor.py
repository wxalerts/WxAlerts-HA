"""Binary sensors: any-active-alert per county, optional per-phenomenon sensors
(tornado, severe thunderstorm, ...) that read far better in automations than
string-matching attributes, and the lightning proximity sensor that a
seek-shelter automation triggers on.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
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
from .entity import WxAlertsLocationEntity, WxAlertsTargetEntity
from .lightning import Target


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WxAlertsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []

    if coordinator.enable_glm:
        entities.extend(
            LightningNearbyBinarySensor(coordinator, target)
            for target in coordinator.targets
        )

        @callback
        def _targets_added(targets: list[Target]) -> None:
            async_add_entities(
                LightningNearbyBinarySensor(coordinator, target) for target in targets
            )

        entry.async_on_unload(coordinator.add_target_listener(_targets_added))

    if not coordinator.enable_alerts:
        async_add_entities(entities)
        return

    options = {**entry.data, **entry.options}
    phenomenon_sensors = options.get(
        CONF_PHENOMENON_SENSORS, DEFAULT_PHENOMENON_SENSORS
    )

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


class LightningNearbyBinarySensor(WxAlertsTargetEntity, BinarySensorEntity):
    """On when the closest flash is inside the alert radius.

    ``device_class: safety`` because this is the entity a seek-shelter
    automation triggers on, and it is the reason the whole proximity set exists:
    everything else on the lightning device is context for this one bit.

    Automations should trigger ``from: "off"`` rather than on any change to on.
    A stale feed makes this unavailable, so an ``unavailable -> on`` transition
    means the feed recovered, not that the sky changed.
    """

    _attr_device_class = BinarySensorDeviceClass.SAFETY

    def __init__(self, coordinator: WxAlertsCoordinator, target: Target) -> None:
        super().__init__(coordinator, target)
        alert = int(coordinator.glm_alert_mi)
        self._attr_name = f"Lightning within {alert}mi"
        # Identity is the target, never the radius: keying on the alert
        # distance would orphan the sensor the first time it was tuned.
        self._attr_unique_id = f"{coordinator.entry.entry_id}_glm_{target.key}_nearby"

    @property
    def is_on(self) -> bool:
        return self.metrics.nearby

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        metrics = self.metrics
        return {
            "target": self._target_name,
            "alert_mi": self.coordinator.glm_alert_mi,
            "nearest_mi": metrics.nearest_mi,
            "trend": metrics.trend,
            "feed_age_seconds": (
                None
                if self.coordinator.glm_feed_age is None
                else round(self.coordinator.glm_feed_age)
            ),
        }
