"""Binary sensors: any-active-alert per county, one sensor per NWS event type
the user selected, and the lightning proximity sensor a seek-shelter
automation triggers on.

Through 0.3.x these were six hardcoded phenomenon buckets, which collapsed the
distinction the products exist to draw: a Tornado Watch and a Tornado Warning
both turned on "Tornado alert", so an automation could not tell "conditions are
favourable" from "take cover now". Selection is per event type now — the
phenomenon and the significance together — and the catalogue covers every type
NWS publishes.
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
    LOC_COUNTY,
    LOC_SAME,
)
from .coordinator import WxAlertsCoordinator
from .entity import WxAlertsLocationEntity, WxAlertsTargetEntity
from .events import BY_KEY, EventType, event_name_for
from .lightning import Target
from .options import selected_event_keys

# Only these carry the safety device class, which renders as Safe/Unsafe. A
# watch means conditions are favourable, not that anyone is currently in
# danger, and labelling one "Unsafe" trains people to ignore the word.
_URGENT_SUFFIXES = ("Warning", "Emergency", "Immediate")


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

    # Unknown keys are skipped rather than fatal: an options blob written by a
    # newer release must not stop the platform loading after a downgrade.
    selected = [
        BY_KEY[key] for key in selected_event_keys(entry) if key in BY_KEY
    ]

    seen_same: set[str] = set()
    for location in coordinator.locations:
        same = location.get(LOC_SAME)
        if not same or same in seen_same:
            continue
        seen_same.add(same)
        entities.append(ActiveAlertBinarySensor(coordinator, location))
        entities.extend(
            EventBinarySensor(coordinator, location, event) for event in selected
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
            "events": [event_name_for(alert) for alert in alerts],
            "event_types": sorted(self.coordinator.active_event_keys(self.same)),
        }


class EventBinarySensor(WxAlertsLocationEntity, BinarySensorEntity):
    """On while one NWS event type is in effect for this county."""

    def __init__(
        self,
        coordinator: WxAlertsCoordinator,
        location: dict[str, Any],
        event: EventType,
    ) -> None:
        super().__init__(coordinator, location)
        self._event = event
        self._attr_name = event.name
        # Keyed on the catalogue key, not the VTEC pair: several codes can fold
        # into one type (WC.W and EC.W are both Extreme Cold Warning), and the
        # entity has to survive the table gaining another.
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{self.same}_event_{event.key}"
        )
        if event.name.endswith(_URGENT_SUFFIXES):
            self._attr_device_class = BinarySensorDeviceClass.SAFETY

    @property
    def is_on(self) -> bool:
        return self.coordinator.has_event(self.same, self._event.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alerts = self.coordinator.alerts_for_event(self.same, self._event.key)
        return {
            "event_type": self._event.key,
            "event": self._event.name,
            "same": self.same,
            # Several live products for one type is normal, so the count is
            # reported rather than assumed to be one.
            "alert_count": len(alerts),
            "alerts": [
                {
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
                }
                for alert in alerts
            ],
        }


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
