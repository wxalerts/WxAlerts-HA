"""Config flow for WxAlerts integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    API_BASE_URL,
    API_COUNTIES_ENDPOINT,
    API_SEARCH_ENDPOINT,
    API_STATES_ENDPOINT,
    CONF_COUNTY,
    CONF_STATE,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


_HEADERS = {"User-Agent": "WxAlerts-HA/1.0 (Home Assistant Integration)"}


async def _fetch_states(session: aiohttp.ClientSession) -> list[str]:
    """Fetch available states from WxAlerts API."""
    async with session.get(f"{API_BASE_URL}{API_STATES_ENDPOINT}", headers=_HEADERS) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data if isinstance(data, list) else data.get("states", [])


async def _fetch_counties(
    session: aiohttp.ClientSession, state: str
) -> list[dict]:
    """Fetch counties/zones for a state from WxAlerts API."""
    async with session.get(
        f"{API_BASE_URL}{API_COUNTIES_ENDPOINT}",
        params={"state": state},
        headers=_HEADERS,
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data if isinstance(data, list) else data.get("zones", [])


async def _search_zones(
    session: aiohttp.ClientSession, state: str, county: str
) -> list[dict]:
    """Fuzzy search zones by state and county name."""
    params: dict[str, str] = {}
    if state:
        params["state"] = state
    if county:
        params["county"] = county
    async with session.get(
        f"{API_BASE_URL}{API_SEARCH_ENDPOINT}", params=params, headers=_HEADERS
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data if isinstance(data, list) else data.get("zones", [])


class WxAlertsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the WxAlerts config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._zones: list[dict] = []
        self._states: list[str] = []
        self._available_zones: list[dict] = []
        self._selected_state: str = ""
        self._session: aiohttp.ClientSession | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """First step — pick a state."""
        errors: dict[str, str] = {}

        try:
            async with aiohttp.ClientSession() as session:
                self._states = await _fetch_states(session)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to fetch states from WxAlerts API")
            errors["base"] = "cannot_connect"

        if errors:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({vol.Required(CONF_STATE): str}),
                errors=errors,
            )

        if user_input is not None:
            self._selected_state = user_input[CONF_STATE]
            return await self.async_step_county()

        state_options = {s: s for s in self._states}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_STATE): vol.In(state_options)}
            ),
            description_placeholders={
                "api_url": API_BASE_URL,
            },
        )

    async def async_step_county(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Second step — search/select county and pick zones."""
        errors: dict[str, str] = {}

        if user_input is not None:
            county_search = user_input.get(CONF_COUNTY, "").strip()
            selected_zone_ids = user_input.get("selected_zones", [])

            if selected_zone_ids:
                # User confirmed zone selection
                for zone in self._available_zones:
                    if zone[CONF_ZONE_ID] in selected_zone_ids:
                        if not any(
                            z[CONF_ZONE_ID] == zone[CONF_ZONE_ID]
                            for z in self._zones
                        ):
                            self._zones.append(zone)

                add_more = user_input.get("add_more", False)
                if add_more:
                    return await self.async_step_user()
                else:
                    return self._create_entry()

            # Perform search
            if county_search:
                try:
                    async with aiohttp.ClientSession() as session:
                        self._available_zones = await _search_zones(
                            session, self._selected_state, county_search
                        )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Failed to search zones")
                    errors["base"] = "cannot_connect"
            else:
                try:
                    async with aiohttp.ClientSession() as session:
                        self._available_zones = await _fetch_counties(
                            session, self._selected_state
                        )
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Failed to fetch counties")
                    errors["base"] = "cannot_connect"

            if not self._available_zones and not errors:
                errors[CONF_COUNTY] = "no_zones_found"

        zone_options = {
            z[CONF_ZONE_ID]: f"{z[CONF_ZONE_ID]} — {z.get(CONF_ZONE_NAME, z[CONF_ZONE_ID])}"
            for z in self._available_zones
        }

        schema_dict: dict = {
            vol.Optional(CONF_COUNTY): str,
        }

        if zone_options:
            schema_dict[vol.Optional("selected_zones")] = vol.All(
                [vol.In(zone_options)], vol.Length(min=1)
            )
            if self._zones:
                schema_dict[vol.Optional("add_more", default=False)] = bool

        return self.async_show_form(
            step_id="county",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "state": self._selected_state,
                "zone_count": str(len(self._zones)),
                "current_zones": ", ".join(z[CONF_ZONE_ID] for z in self._zones)
                if self._zones
                else "None yet",
            },
            errors=errors,
        )

    def _create_entry(self) -> FlowResult:
        """Create the config entry with selected zones."""
        zone_ids = [z[CONF_ZONE_ID] for z in self._zones]
        title = f"WxAlerts ({', '.join(zone_ids)})"
        return self.async_create_entry(
            title=title,
            data={CONF_ZONES: self._zones},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> WxAlertsOptionsFlow:
        """Return the options flow to allow adding/removing zones post-setup."""
        return WxAlertsOptionsFlow(config_entry)


class WxAlertsOptionsFlow(config_entries.OptionsFlow):
    """Handle options — add or remove monitored zones."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._zones: list[dict] = list(
            config_entry.data.get(CONF_ZONES, [])
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage zones — remove existing or add new."""
        errors: dict[str, str] = {}

        if user_input is not None:
            keep_zone_ids = user_input.get("keep_zones", [])
            self._zones = [
                z for z in self._zones if z[CONF_ZONE_ID] in keep_zone_ids
            ]

            if user_input.get("add_more", False):
                # Redirect through main flow to add zones
                # (simplified: user can re-run config flow)
                pass

            return self.async_create_entry(
                title="",
                data={CONF_ZONES: self._zones},
            )

        current_zones = {
            z[CONF_ZONE_ID]: f"{z[CONF_ZONE_ID]} — {z.get(CONF_ZONE_NAME, z[CONF_ZONE_ID])}"
            for z in self._zones
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "keep_zones",
                        default=list(current_zones.keys()),
                    ): vol.All([vol.In(current_zones)]),
                    vol.Optional("add_more", default=False): bool,
                }
            ),
            description_placeholders={
                "zone_count": str(len(self._zones)),
            },
            errors=errors,
        )
