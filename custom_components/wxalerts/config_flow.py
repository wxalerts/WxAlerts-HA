"""Config flow for the WxAlerts integration.

The person signs in to their wxalerts.org account in the browser (OAuth2 with
PKCE through authentik). The integration then registers this Home Assistant
instance with the API and stores the broker login it gets back. Nothing is
copied or pasted.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .api import (
    ApiAuthError,
    ApiUnavailable,
    InstanceLimit,
    SupporterRequired,
    async_register_instance,
    async_register_oauth,
    broker_data,
)
from .const import (
    CONF_EVENT_SENSORS,
    CONF_PLACES,
    CONFIG_VERSION,
    DOMAIN,
    PRICING_URL,
)
from .events import DEFAULT_EVENT_KEYS, SELECTABLE_EVENTS
from .options import selected_event_keys

_LOGGER = logging.getLogger(__name__)

# Every NWS event type, searchable. Sorted by name rather than grouped by
# hazard family, because the box is a search box first.
_EVENT_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value=event.key, label=event.name)
            for event in SELECTABLE_EVENTS
        ],
        multiple=True,
        mode=selector.SelectSelectorMode.DROPDOWN,
        sort=False,
    )
)


class WxAlertsConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Sign in, register this instance, done."""

    DOMAIN = DOMAIN
    VERSION = CONFIG_VERSION
    MINOR_VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self.source != SOURCE_REAUTH:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
        # Registered here as well as at setup: a flow can start before the
        # integration has ever been set up.
        async_register_oauth(self.hass)
        return await super().async_step_user(user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The broker login could not be renewed, or an upgrade from 0.x."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Signed in: register this instance and keep its broker login."""
        placeholders = {"pricing_url": PRICING_URL}
        try:
            registration = await async_register_instance(
                self.hass, data["token"]["access_token"]
            )
        except SupporterRequired:
            return self.async_abort(
                reason="supporter_required", description_placeholders=placeholders
            )
        except InstanceLimit as err:
            return self.async_abort(
                reason="instance_limit",
                description_placeholders={
                    "pricing_url": err.upgrade_url,
                    "limit": str(err.limit if err.limit is not None else "?"),
                },
            )
        except ApiAuthError:
            return self.async_abort(reason="oauth_unauthorized")
        except ApiUnavailable as err:
            _LOGGER.warning("Could not register with wxalerts.org: %s", err)
            return self.async_abort(reason="cannot_connect")

        instance_id = await async_get_instance_id(self.hass)
        entry_data = {**data, **broker_data(registration, instance_id)}

        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=entry_data
            )
        return self.async_create_entry(
            title="WxAlerts",
            data={**entry_data, CONF_PLACES: []},
            # Written explicitly so a fresh entry never depends on a migration
            # path to know what it selected.
            options={CONF_EVENT_SENSORS: list(DEFAULT_EVENT_KEYS)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WxAlertsOptionsFlow:
        return WxAlertsOptionsFlow()


class WxAlertsOptionsFlow(OptionsFlow):
    """Options: which alert types get binary sensors.

    Places are edited on wxalerts.org and arrive on their own; there is
    nothing about them to configure here.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={CONF_EVENT_SENSORS: user_input.get(CONF_EVENT_SENSORS, [])},
            )

        schema = vol.Schema(
            {
                # Not vol.Required: an empty list is a legitimate answer
                # (device triggers alone), and Required would reject it.
                vol.Optional(
                    CONF_EVENT_SENSORS, default=selected_event_keys(self.config_entry)
                ): _EVENT_SELECTOR,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
