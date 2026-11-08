"""Shared entity plumbing for WxAlerts."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import (
    FIELD_DIGITS,
    COND_DISTANCE_KM,
    COND_OBSERVED_AT,
    COND_STATION_ID,
    COND_STATION_NAME,
    SIGNAL_CONDITIONS_UPDATED,
    SIGNAL_ALERTS_UPDATED,
    SIGNAL_CONNECTION,
    SIGNAL_PLACE_UPDATED,
    SIGNAL_STORMS_UPDATED,
    SIGNAL_TICK,
)
from .coordinator import (
    Place,
    WxAlertsCoordinator,
    place_device_identifier,
    service_device_identifier,
)

MANUFACTURER = "WxAlerts.org"


def field_number(data: dict[str, Any] | None, field: str) -> float | int | None:
    """A numeric field rounded to its FIELD_DIGITS, or None.

    Stations and models report long tails of digits (3.97677563031894); the
    state should read 4.0. Whole-number fields come back as int.
    """
    value = (data or {}).get(field)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    digits = FIELD_DIGITS.get(field)
    if digits is None:
        return number
    if digits == 0:
        return int(round(number))
    return round(number, digits)


def station_label(observation: dict[str, Any]) -> str | None:
    """The station's name, or its id without the network prefix.

    APRS and CWOP stations have no name; "aprs:KN4OQW-13" reads KN4OQW-13.
    """
    name = observation.get(COND_STATION_NAME)
    if name:
        return name
    station_id = observation.get(COND_STATION_ID)
    if not station_id:
        return None
    return str(station_id).split(":", 1)[-1]


def place_device_info(entry_id: str, place: Place) -> DeviceInfo:
    """The device every entity of a place hangs off."""
    return DeviceInfo(
        identifiers={place_device_identifier(entry_id, place.id)},
        name=f"WxAlerts {place.name}",
        manufacturer=MANUFACTURER,
        model="Place",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://wxalerts.org",
    )


class WxAlertsPlaceEntity(Entity):
    """Base entity tied to one WxAlerts place.

    Holds the place id, never the place itself: the places list is republished
    whenever the person edits it, and an entity reading a stale copy would keep
    alerting for a county the place has moved out of.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    # Which coordinator signals wake this entity, beyond the connection state.
    _listen_alerts = False
    _listen_storms = False
    _listen_tick = False
    _listen_conditions = False

    def __init__(
        self, coordinator: WxAlertsCoordinator, place: Place, suffix: str
    ) -> None:
        self.coordinator = coordinator
        self.place_id = place.id
        entry_id = coordinator.entry.entry_id
        self._attr_unique_id = f"{entry_id}_place_{place.id}_{suffix}"
        self._attr_device_info = place_device_info(entry_id, place)

    @property
    def place(self) -> Place | None:
        return self.coordinator.place(self.place_id)

    @property
    def same(self) -> str | None:
        place = self.place
        return place.same if place else None

    @property
    def available(self) -> bool:
        return (
            self.coordinator.connected
            and self.coordinator.deactivated_reason is None
            and self.place is not None
        )

    async def async_added_to_hass(self) -> None:
        entry_id = self.coordinator.entry.entry_id
        signals = [
            f"{SIGNAL_CONNECTION}_{entry_id}",
            f"{SIGNAL_PLACE_UPDATED}_{entry_id}_{self.place_id}",
        ]
        if self._listen_storms:
            signals.append(f"{SIGNAL_STORMS_UPDATED}_{entry_id}_{self.place_id}")
        if self._listen_tick:
            signals.append(f"{SIGNAL_TICK}_{entry_id}")
        if self._listen_conditions:
            signals.append(f"{SIGNAL_CONDITIONS_UPDATED}_{entry_id}_{self.place_id}")
        for signal in signals:
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self._handle_update)
            )
        if self._listen_alerts:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{SIGNAL_ALERTS_UPDATED}_{entry_id}",
                    self._handle_alerts,
                )
            )

    @callback
    def _handle_alerts(self, same: str) -> None:
        # One signal for every county; the place's county is read at the moment
        # of the update, so a place whose county changed follows it.
        if same == self.same:
            self.async_write_ha_state()

    @callback
    def _handle_update(self, *_args: Any) -> None:
        # Must be a callback: the dispatcher runs an undecorated sync target
        # in an executor thread, and async_write_ha_state from a thread is a
        # hard error.
        self.async_write_ha_state()


class WxAlertsAlertEntity(WxAlertsPlaceEntity):
    """A place entity driven by NWS alerts for the place's county."""

    _listen_alerts = True

    @property
    def available(self) -> bool:
        # A place with no county has no alert feed behind it. Unavailable says
        # that honestly; "off" would claim the county is quiet.
        return super().available and self.same is not None


class WxAlertsStormEntity(WxAlertsPlaceEntity):
    """A place entity driven by the place's storm cell topics."""

    _listen_storms = True
    _listen_tick = True

    @property
    def available(self) -> bool:
        return self.coordinator.storms_available and self.place is not None

    @property
    def storm(self) -> dict[str, Any] | None:
        return self.coordinator.primary_storm(self.place_id)


class WxAlertsConditionsEntity(WxAlertsPlaceEntity):
    """A place entity reading the latest observation near the place.

    Listens to the tick as well, so an observation crossing the two-hour
    line goes unavailable with no new message arriving.
    """

    _listen_conditions = True
    _listen_tick = True

    @property
    def observation(self) -> dict[str, Any] | None:
        return self.coordinator.conditions_for(self.place_id)

    @property
    def available(self) -> bool:
        return super().available and self.observation is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        observation = self.observation or {}
        return {
            "station_id": observation.get(COND_STATION_ID),
            "station": station_label(observation),
            "distance_km": observation.get(COND_DISTANCE_KM),
            "observed_at": observation.get(COND_OBSERVED_AT),
        }


class WxAlertsServiceEntity(Entity):
    """Account-wide entities: the feed itself, not any one place."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: WxAlertsCoordinator, suffix: str) -> None:
        self.coordinator = coordinator
        entry_id = coordinator.entry.entry_id
        self._attr_unique_id = f"{entry_id}_service_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={service_device_identifier(entry_id)},
            name="WxAlerts",
            manufacturer=MANUFACTURER,
            model="Feed",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://wxalerts.org/account/",
        )

    async def async_added_to_hass(self) -> None:
        entry_id = self.coordinator.entry.entry_id
        for signal in (f"{SIGNAL_CONNECTION}_{entry_id}", f"{SIGNAL_TICK}_{entry_id}"):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self._handle_update)
            )

    @callback
    def _handle_update(self, *_args: Any) -> None:
        self.async_write_ha_state()
