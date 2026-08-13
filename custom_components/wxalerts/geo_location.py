"""Map markers for live alerts, following the USGS/NSW-fire pattern:
``geo_location`` entities that appear with the hazard and vanish with its
tombstone. The marker sits on the polygon centroid; the polygon itself is
exposed as an attribute for anyone drawing the real shape.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.location import distance as location_distance

from . import WxAlertsConfigEntry
from .const import DOMAIN, LOC_SAME, SIGNAL_ALERTS_UPDATED
from .coordinator import WxAlertsCoordinator
from .geo import geometry_centroid

SOURCE = DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WxAlertsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if not coordinator.enable_alerts:
        return

    seen_same: set[str] = set()
    for location in coordinator.locations:
        same = location.get(LOC_SAME)
        if not same or same in seen_same:
            continue
        seen_same.add(same)
        manager = AlertMarkerManager(hass, coordinator, same, async_add_entities)
        entry.async_on_unload(manager.start())


class AlertMarkerManager:
    """Creates a marker entity per live hazard topic and lets each entity
    remove itself when its hazard is tombstoned."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: WxAlertsCoordinator,
        same: str,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._same = same
        self._async_add_entities = async_add_entities
        self._known_topics: set[str] = set()

    def start(self):
        unsub = async_dispatcher_connect(
            self._hass,
            f"{SIGNAL_ALERTS_UPDATED}_{self._coordinator.entry.entry_id}_{self._same}",
            self._sync,
        )
        self._sync()
        return unsub

    @callback
    def _sync(self, *_args: Any) -> None:
        current = self._coordinator.alert_topics_for(self._same)
        # Entities for vanished topics remove themselves on the same signal.
        self._known_topics.intersection_update(current)
        new_entities = []
        for topic, alert in current.items():
            if topic in self._known_topics:
                continue
            centroid = geometry_centroid(alert.get("geometry"))
            if centroid is None:
                continue
            self._known_topics.add(topic)
            new_entities.append(
                AlertMarker(self._coordinator, self._same, topic, centroid)
            )
        if new_entities:
            self._async_add_entities(new_entities)


class AlertMarker(GeolocationEvent):
    """One live hazard on the map."""

    _attr_should_poll = False
    _attr_source = SOURCE
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:alert"

    def __init__(
        self,
        coordinator: WxAlertsCoordinator,
        same: str,
        topic: str,
        centroid: tuple[float, float],
    ) -> None:
        self._coordinator = coordinator
        self._same = same
        self._topic = topic
        self._attr_latitude, self._attr_longitude = centroid
        self._attr_unique_id = f"{coordinator.entry.entry_id}_geo_{topic}"
        self._refresh_from_alert()

    def _current_alert(self) -> dict[str, Any] | None:
        return self._coordinator.alert_topics_for(self._same).get(self._topic)

    def _refresh_from_alert(self) -> None:
        alert = self._current_alert()
        if alert is None:
            return
        self._attr_name = alert.get("event") or "Weather alert"
        centroid = geometry_centroid(alert.get("geometry"))
        if centroid:
            self._attr_latitude, self._attr_longitude = centroid

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_ALERTS_UPDATED}_{self._coordinator.entry.entry_id}_{self._same}",
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self, *_args: Any) -> None:
        if self._current_alert() is None:
            # Hazard tombstoned — take the marker off the map.
            self.hass.async_create_task(self.async_remove(force_remove=True))
            return
        self._refresh_from_alert()
        self.async_write_ha_state()

    @property
    def distance(self) -> float | None:
        meters = location_distance(
            self.hass.config.latitude,
            self.hass.config.longitude,
            self._attr_latitude,
            self._attr_longitude,
        )
        return round(meters / 1000, 1) if meters is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alert = self._current_alert() or {}
        return {
            "same": self._same,
            "vtec": alert.get("vtec"),
            "severity": alert.get("severity"),
            "headline": alert.get("headline"),
            "ends": alert.get("ends"),
            "geometry_source": alert.get("geometry_source"),
            "geometry": alert.get("geometry"),
        }
