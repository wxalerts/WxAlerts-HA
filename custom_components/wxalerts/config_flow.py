"""Config flow for the WxAlerts integration.

The user enters the Home Assistant broker login created on wxalerts.org
(Account, Home Assistant). It is validated by connecting with it and
subscribing to the account's own topics; places then come from the account.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    ACCOUNT_URL,
    CONF_EVENT_SENSORS,
    CONF_PLACES,
    CONFIG_VERSION,
    DOMAIN,
    PRICING_URL,
)
from .coordinator import (
    CannotConnect,
    InvalidAuth,
    SupporterRequired,
    async_validate_login,
    parse_places,
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


def _login_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")
            ): selector.TextSelector(
                selector.TextSelectorConfig(autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(
                    type=selector.TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            ),
        }
    )


_PLACEHOLDERS = {"pricing_url": PRICING_URL, "account_url": ACCOUNT_URL}


class WxAlertsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup and re-authentication."""

    VERSION = CONFIG_VERSION
    MINOR_VERSION = 1

    async def _async_check(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, str], list[dict[str, Any]] | None]:
        """Validate a login. Returns (errors, places)."""
        username = user_input[CONF_USERNAME].strip()
        try:
            places = await async_validate_login(
                self.hass, username, user_input[CONF_PASSWORD]
            )
        except InvalidAuth:
            return {"base": "invalid_auth"}, None
        except SupporterRequired:
            return {"base": "supporter_required"}, None
        except CannotConnect:
            return {"base": "cannot_connect"}, None
        except Exception:  # noqa: BLE001 - surface anything else as unknown
            _LOGGER.exception("Unexpected error validating the WxAlerts login")
            return {"base": "unknown"}, None
        return {}, places

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            errors, places = await self._async_check(user_input)
            if not errors:
                parsed = parse_places(places) or {}
                return self.async_create_entry(
                    title="WxAlerts",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_PLACES: [place.as_dict() for place in parsed.values()],
                    },
                    # Written explicitly so a fresh entry never depends on a
                    # migration path to know what it selected.
                    options={CONF_EVENT_SENSORS: list(DEFAULT_EVENT_KEYS)},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_login_schema(user_input or {}),
            errors=errors,
            description_placeholders=_PLACEHOLDERS,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """A rotated password, a lapsed plan, or an upgrade from 0.x."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, places = await self._async_check(user_input)
            if not errors:
                updates: dict[str, Any] = {
                    CONF_USERNAME: user_input[CONF_USERNAME].strip(),
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                parsed = parse_places(places)
                if parsed is not None:
                    updates[CONF_PLACES] = [p.as_dict() for p in parsed.values()]
                return self.async_update_reload_and_abort(entry, data_updates=updates)

        defaults = user_input or {CONF_USERNAME: entry.data.get(CONF_USERNAME, "")}
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_login_schema(defaults),
            errors=errors,
            description_placeholders=_PLACEHOLDERS,
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
