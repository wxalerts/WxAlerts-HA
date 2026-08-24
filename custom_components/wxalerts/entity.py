"""Shared entity plumbing for WxAlerts."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import (
    DOMAIN,
    LOC_NAME,
    LOC_SAME,
    LOC_ZONE_ENTITY,
    SIGNAL_ALERTS_UPDATED,
    SIGNAL_CONNECTION,
    SIGNAL_GLM_UPDATED,
)
from .coordinator import WxAlertsCoordinator
from .lightning import Target


class WxAlertsLocationEntity(Entity):
    """Base entity tied to one configured location (one HA zone).

    Wires up the relevant dispatcher signals; subclasses only implement
    state properties reading from the coordinator.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: WxAlertsCoordinator, location: dict[str, Any]
    ) -> None:
        self.coordinator = coordinator
        self.location = location
        self.same: str | None = location.get(LOC_SAME)

        entry_id = coordinator.entry.entry_id
        # Counties group zones onto one device. A zone with no county falls
        # back to its zone id rather than its geohash prefix, which would move
        # the whole device whenever the lightning box size changed.
        device_key = (
            self.same
            or location.get(LOC_ZONE_ENTITY)
            or location.get(LOC_NAME, "unknown")
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{device_key}")},
            name=f"WxAlerts {location.get(LOC_NAME)}",
            manufacturer="WxAlerts.org",
            model="NWS alert & GLM lightning feed",
            configuration_url="https://wxalerts.org",
        )

    @property
    def available(self) -> bool:
        return self.coordinator.connected

    async def async_added_to_hass(self) -> None:
        entry_id = self.coordinator.entry.entry_id
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_CONNECTION}_{entry_id}",
                self._handle_update,
            )
        )
        if self.same:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    f"{SIGNAL_ALERTS_UPDATED}_{entry_id}_{self.same}",
                    self._handle_update,
                )
            )
    @callback
    def _handle_update(self, *_args: Any) -> None:
        # Must be a callback: the dispatcher runs an undecorated sync target
        # in an executor thread, and async_write_ha_state from a thread is a
        # hard error — every entity would stay stuck at its initial state.
        self.async_write_ha_state()


class WxAlertsTargetEntity(Entity):
    """Base entity for lightning proximity, tied to one target.

    A target is a zone or a tracked person, so this cannot hang off the county
    device the alert entities share — two people in one county need separate
    devices, and a person is not in a fixed county at all.

    The device is named ``Lightning — <name>`` deliberately: Home Assistant
    builds entity ids from the device and entity names together, so this
    produces ``sensor.lightning_home_nearest_strike`` and friends, matching what
    a dashboard built against the standalone proximity service already points
    at.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: WxAlertsCoordinator, target: Target) -> None:
        self.coordinator = coordinator
        self.target_key = target.key
        self._target_name = target.name

        entry_id = coordinator.entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_lightning_{target.key}")},
            name=f"Lightning — {target.name}",
            manufacturer="WxAlerts.org",
            model="GOES GLM proximity",
            configuration_url="https://wxalerts.org",
        )

    @property
    def target(self) -> Target | None:
        return self.coordinator.target(self.target_key)

    @property
    def metrics(self):
        return self.coordinator.metrics_for(self.target_key)

    @property
    def available(self) -> bool:
        # A target that has gone away (a person who lost their fix) is
        # unavailable rather than frozen at its last position.
        return self.coordinator.glm_available and self.target is not None

    async def async_added_to_hass(self) -> None:
        entry_id = self.coordinator.entry.entry_id
        for signal in (
            f"{SIGNAL_CONNECTION}_{entry_id}",
            f"{SIGNAL_GLM_UPDATED}_{entry_id}_{self.target_key}",
        ):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self._handle_update)
            )

    @callback
    def _handle_update(self, *_args: Any) -> None:
        # Callback for the same reason the alert entities are: the dispatcher
        # runs an undecorated sync target in an executor thread, and writing
        # state from a thread is a hard error.
        self.async_write_ha_state()
