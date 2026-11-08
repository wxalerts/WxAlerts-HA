"""Map markers for live alerts, following the USGS/NSW-fire pattern:
``geo_location`` entities that appear with the hazard and vanish with its
tombstone. The marker sits on the polygon centroid; the polygon itself is
exposed as an attribute for anyone drawing the real shape.

Markers belong to a place: each sits on its place's device, is named
``geo_location.wxalerts_<place>_<event>``, and goes when the place goes.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.geo_location import GeolocationEvent
from homeassistant.const import UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.location import distance as location_distance

from . import WxAlertsConfigEntry
from .const import (
    DOMAIN,
    SIGNAL_ALERTS_UPDATED,
    SIGNAL_CONNECTION,
    SIGNAL_PLACE_UPDATED,
    TOPIC_ALERT_ROOT,
)
from .coordinator import Place, WxAlertsCoordinator
from .entity import place_device_info
from .events import event_key_for, event_name_for
from .geo import geometry_centroid

SOURCE = DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WxAlertsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    manager = AlertMarkerManager(hass, entry.runtime_data, async_add_entities)
    for unsub in manager.start():
        entry.async_on_unload(unsub)


class AlertMarkerManager:
    """Creates a marker per (place, live hazard topic); each marker removes
    itself when its hazard ends or its place leaves the county."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: WxAlertsCoordinator,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._known: set[tuple[str, str]] = set()

    def start(self) -> list:
        entry_id = self._coordinator.entry.entry_id
        unsubs = [
            async_dispatcher_connect(
                self._hass, f"{SIGNAL_ALERTS_UPDATED}_{entry_id}", self._sync
            ),
            self._coordinator.add_place_listener(self._places_added),
        ]
        for same in self._coordinator.same_codes:
            self._sync(same)
        return unsubs

    @callback
    def _places_added(self, places: list[Place]) -> None:
        for same in {place.same for place in places if place.same}:
            self._sync(same)

    @callback
    def _sync(self, same: str) -> None:
        current = self._coordinator.alert_topics_for(same)
        prefix = f"{TOPIC_ALERT_ROOT}{same}/"
        places = self._coordinator.places_for_same(same)
        place_ids = {place.id for place in places}
        # Markers for ended hazards or departed places remove themselves on
        # the same signal; forget them here so a reissue gets a new one.
        self._known = {
            (place_id, topic)
            for place_id, topic in self._known
            if not topic.startswith(prefix)
            or (topic in current and place_id in place_ids)
        }
        new_entities = []
        for place in places:
            for topic, alert in current.items():
                if (place.id, topic) in self._known:
                    continue
                centroid = geometry_centroid(alert.get("geometry"))
                if centroid is None:
                    continue
                self._known.add((place.id, topic))
                new_entities.append(
                    AlertMarker(self._coordinator, place, same, topic, centroid)
                )
        if new_entities:
            self._async_add_entities(new_entities)


class AlertMarker(GeolocationEvent):
    """One live hazard on the map, for one place."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_source = SOURCE
    _attr_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_icon = "mdi:alert"
    # A county-union polygon runs to tens of kilobytes, well past the
    # recorder's 16 KB attribute cap, which makes it drop every attribute on
    # the entity. Keeping geometry out of the recorder leaves it live in the
    # state machine for cards and templates.
    _unrecorded_attributes = frozenset({"geometry"})

    def __init__(
        self,
        coordinator: WxAlertsCoordinator,
        place: Place,
        same: str,
        topic: str,
        centroid: tuple[float, float],
    ) -> None:
        self._coordinator = coordinator
        self._place_id = place.id
        self._same = same
        self._topic = topic
        self._attr_latitude, self._attr_longitude = centroid
        entry_id = coordinator.entry.entry_id
        self._attr_unique_id = f"{entry_id}_place_{place.id}_geo_{topic}"
        self._attr_device_info = place_device_info(entry_id, place)
        self._refresh_from_alert()

    def _current_alert(self) -> dict[str, Any] | None:
        return self._coordinator.alert_topics_for(self._same).get(self._topic)

    def _still_here(self) -> bool:
        place = self._coordinator.place(self._place_id)
        return (
            place is not None
            and place.same == self._same
            and self._current_alert() is not None
        )

    def _refresh_from_alert(self) -> None:
        alert = self._current_alert()
        if alert is None:
            return
        # The catalogue's spelling, so the marker on the map and the binary
        # sensor in the automation agree on what the hazard is called.
        self._attr_name = event_name_for(alert)
        centroid = geometry_centroid(alert.get("geometry"))
        if centroid:
            self._attr_latitude, self._attr_longitude = centroid

    @property
    def available(self) -> bool:
        return self._coordinator.connected and self._coordinator.deactivated_reason is None

    async def async_added_to_hass(self) -> None:
        entry_id = self._coordinator.entry.entry_id
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_ALERTS_UPDATED}_{entry_id}", self._handle_alerts
            )
        )
        for signal in (
            f"{SIGNAL_PLACE_UPDATED}_{entry_id}_{self._place_id}",
            f"{SIGNAL_CONNECTION}_{entry_id}",
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self._handle_update)
            )

    @callback
    def _handle_alerts(self, same: str) -> None:
        if same == self._same:
            self._handle_update()

    @callback
    def _handle_update(self, *_args: Any) -> None:
        if not self._still_here():
            # Hazard ended, or the place moved county: off the map, and out
            # of the registry so the id is free for the next one.
            self.hass.async_create_task(self._async_retire())
            return
        self._refresh_from_alert()
        self.async_write_ha_state()

    async def _async_retire(self) -> None:
        registry = er.async_get(self.hass)
        if self.registry_entry is not None:
            registry.async_remove(self.entity_id)
        else:
            await self.async_remove(force_remove=True)

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
        place = self._coordinator.place(self._place_id)
        return {
            "place_id": self._place_id,
            "place_name": place.name if place else None,
            "same": self._same,
            "event_type": event_key_for(alert) if alert else None,
            "vtec": alert.get("vtec"),
            "severity": alert.get("severity"),
            "headline": alert.get("headline"),
            "ends": alert.get("ends"),
            "geometry_source": alert.get("geometry_source"),
            "geometry": alert.get("geometry"),
        }
