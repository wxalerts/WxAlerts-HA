"""Binary sensors per place: any active alert, one sensor per NWS event type
the user selected, and storm cell approaching. One account-wide sensor for the
broker connection.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WxAlertsConfigEntry
from .const import (
    STORM_BEARING_DEG,
    STORM_DISTANCE_KM,
    STORM_EPISODE,
    STORM_ID,
    STORM_PROB_HAIL,
    STORM_PROB_SEVERE,
    STORM_PROB_TOR,
    STORM_PROB_WIND,
    STORM_RUNG,
    STORM_RUNG_LABEL,
    STORM_SPEED_KMH,
    STORM_UPDATED_AT,
)
from .coordinator import Place, WxAlertsCoordinator, threat_for_rung
from .entity import (
    WxAlertsAlertEntity,
    WxAlertsServiceEntity,
    WxAlertsStormEntity,
)
from .events import BY_KEY, EventType, event_name_for
from .options import selected_event_keys

# Only these carry the safety device class, which renders as Safe/Unsafe. A
# watch means conditions are favourable, not that anyone is currently in
# danger, and labelling one "Unsafe" trains people to ignore the word.
_URGENT_SUFFIXES = ("Warning", "Emergency", "Immediate")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WxAlertsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    # Unknown keys are skipped rather than fatal: an options blob written by a
    # newer release must not stop the platform loading after a downgrade.
    selected = [BY_KEY[key] for key in selected_event_keys(entry) if key in BY_KEY]

    def _for_places(places: list[Place]) -> list[BinarySensorEntity]:
        entities: list[BinarySensorEntity] = []
        for place in places:
            entities.append(ActiveAlertBinarySensor(coordinator, place))
            entities.extend(
                EventBinarySensor(coordinator, place, event) for event in selected
            )
            entities.append(StormApproachingBinarySensor(coordinator, place))
        return entities

    @callback
    def _places_added(places: list[Place]) -> None:
        async_add_entities(_for_places(places))

    entry.async_on_unload(coordinator.add_place_listener(_places_added))
    async_add_entities(
        [
            ConnectedBinarySensor(coordinator),
            *_for_places(list(coordinator.places.values())),
        ]
    )


class ActiveAlertBinarySensor(WxAlertsAlertEntity, BinarySensorEntity):
    """On when any hazard is live for this place's county."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_translation_key = "active_alert"

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "active")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.alerts_for(self.same))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alerts = self.coordinator.alerts_for(self.same)
        return {
            "place_id": self.place_id,
            "same": self.same,
            "alert_count": len(alerts),
            "events": [event_name_for(alert) for alert in alerts],
            "event_types": sorted(self.coordinator.active_event_keys(self.same)),
        }


class EventBinarySensor(WxAlertsAlertEntity, BinarySensorEntity):
    """On while one NWS event type is in effect for this place."""

    def __init__(
        self, coordinator: WxAlertsCoordinator, place: Place, event: EventType
    ) -> None:
        # Keyed on the catalogue key, not the VTEC pair: several codes can fold
        # into one type, and the entity has to survive the table gaining one.
        super().__init__(coordinator, place, f"event_{event.key}")
        self._event = event
        self._attr_name = event.name
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
            "place_id": self.place_id,
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


class StormApproachingBinarySensor(WxAlertsStormEntity, BinarySensorEntity):
    """On while a storm cell threatens this place.

    Automations should trigger ``from: "off"`` rather than on any change to
    on: a stale feed makes this unavailable, so ``unavailable`` to ``on``
    means the feed recovered, not that a new storm arrived.
    """

    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_translation_key = "storm_cell_approaching"

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "storm")

    @property
    def is_on(self) -> bool:
        return self.storm is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        storm = self.storm or {}
        age = self.coordinator.feed_age
        return {
            "place_id": self.place_id,
            "storm_id": storm.get(STORM_ID),
            "episode": storm.get(STORM_EPISODE),
            "eta_min": self.coordinator.storm_eta(storm) if storm else None,
            "rung": storm.get(STORM_RUNG),
            "rung_label": storm.get(STORM_RUNG_LABEL),
            "threat": threat_for_rung(storm.get(STORM_RUNG)),
            "prob_severe": storm.get(STORM_PROB_SEVERE),
            "prob_tor": storm.get(STORM_PROB_TOR),
            "prob_hail": storm.get(STORM_PROB_HAIL),
            "prob_wind": storm.get(STORM_PROB_WIND),
            "speed_kmh": storm.get(STORM_SPEED_KMH),
            "bearing_deg": storm.get(STORM_BEARING_DEG),
            "distance_km": storm.get(STORM_DISTANCE_KM),
            "updated_at": storm.get(STORM_UPDATED_AT),
            "storm_count": len(self.coordinator.storms_for(self.place_id)),
            "feed_age_seconds": None if age is None else round(age),
        }


class ConnectedBinarySensor(WxAlertsServiceEntity, BinarySensorEntity):
    """Whether the broker session is up."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connected"

    def __init__(self, coordinator: WxAlertsCoordinator) -> None:
        super().__init__(coordinator, "connected")

    @property
    def is_on(self) -> bool:
        return self.coordinator.connected
