"""Config flow for the WxAlerts integration.

The user picks Home Assistant zones; each zone's coordinates are resolved
once — county SAME code via api.weather.gov (never polled at runtime) and a
geohash computed locally for the GLM lightning tree.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_GLM_PRECISION,
    CONF_GLM_WINDOW,
    CONF_LOCATIONS,
    CONF_PHENOMENON_SENSORS,
    CONF_ZONES,
    DEFAULT_ENABLE_ALERTS,
    DEFAULT_ENABLE_GLM,
    DEFAULT_GLM_PRECISION,
    DEFAULT_GLM_WINDOW,
    DEFAULT_PHENOMENON_SENSORS,
    DOMAIN,
    LOC_COUNTY,
    LOC_GEOHASH,
    LOC_LAT,
    LOC_LON,
    LOC_NAME,
    LOC_SAME,
    LOC_STATE,
    LOC_ZONE_ENTITY,
    NWS_POINTS_URL,
)
from .geo import geohash_encode, same_from_county_url

_LOGGER = logging.getLogger(__name__)

_USER_AGENT = "WxAlerts-HA (github.com/wxalerts/WxAlerts-HA)"

_ZONE_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="zone", multiple=True)
)


async def _resolve_zone(hass: HomeAssistant, zone_entity_id: str) -> dict[str, Any] | None:
    """Turn a zone entity into a location record.

    Returns None only when the zone has no usable coordinates. A zone that
    resolves to no county (offshore, marine, outside the US) still gets a
    geohash so lightning works; it just has no SAME code and thus no alerts.
    """
    state = hass.states.get(zone_entity_id)
    if state is None:
        return None
    lat = state.attributes.get(ATTR_LATITUDE)
    lon = state.attributes.get(ATTR_LONGITUDE)
    if lat is None or lon is None:
        return None

    location: dict[str, Any] = {
        LOC_ZONE_ENTITY: zone_entity_id,
        LOC_NAME: state.name or zone_entity_id.split(".", 1)[-1],
        LOC_LAT: lat,
        LOC_LON: lon,
        LOC_GEOHASH: geohash_encode(lat, lon, precision=5),
        LOC_SAME: None,
        LOC_COUNTY: None,
        LOC_STATE: None,
    }

    session = async_get_clientsession(hass)
    try:
        resp = await session.get(
            NWS_POINTS_URL.format(lat=round(lat, 4), lon=round(lon, 4)),
            headers={"User-Agent": _USER_AGENT, "Accept": "application/geo+json"},
            timeout=15,
        )
        if resp.status == 200:
            data = await resp.json()
            props = data.get("properties", {})
            county_url = props.get("county")
            if county_url:
                location[LOC_SAME] = same_from_county_url(county_url)
                location[LOC_COUNTY] = county_url.rstrip("/").rsplit("/", 1)[-1]
            rel = props.get("relativeLocation", {}).get("properties", {})
            location[LOC_STATE] = rel.get("state")
        else:
            _LOGGER.warning(
                "api.weather.gov returned %s for %s; alerts unavailable for it",
                resp.status,
                zone_entity_id,
            )
    except Exception:  # noqa: BLE001 - offline flow should still allow GLM
        _LOGGER.warning(
            "Could not reach api.weather.gov to resolve %s; "
            "lightning will work, alerts will not",
            zone_entity_id,
        )
    return location


async def _resolve_zones(
    hass: HomeAssistant, zone_entity_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve every selected zone; return (locations, skipped_zone_ids)."""
    locations: list[dict[str, Any]] = []
    skipped: list[str] = []
    for zone_entity_id in zone_entity_ids:
        location = await _resolve_zone(hass, zone_entity_id)
        if location is None:
            skipped.append(zone_entity_id)
        else:
            locations.append(location)
    return locations, skipped


def _feature_schema(defaults: dict[str, Any]) -> dict:
    return {
        vol.Required(
            CONF_ENABLE_ALERTS,
            default=defaults.get(CONF_ENABLE_ALERTS, DEFAULT_ENABLE_ALERTS),
        ): selector.BooleanSelector(),
        vol.Required(
            CONF_ENABLE_GLM,
            default=defaults.get(CONF_ENABLE_GLM, DEFAULT_ENABLE_GLM),
        ): selector.BooleanSelector(),
    }


class WxAlertsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # One entry per HA instance: one MQTT connection is the whole budget.
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            zones: list[str] = user_input[CONF_ZONES]
            if not zones:
                errors["base"] = "no_zones"
            else:
                locations, skipped = await _resolve_zones(self.hass, zones)
                if not locations:
                    errors["base"] = "no_locations"
                else:
                    if skipped:
                        _LOGGER.warning(
                            "Skipped zones without coordinates: %s", skipped
                        )
                    if user_input[CONF_ENABLE_ALERTS] and not any(
                        loc[LOC_SAME] for loc in locations
                    ):
                        errors["base"] = "no_same_codes"
                if not errors:
                    return self.async_create_entry(
                        title="WxAlerts",
                        data={
                            CONF_ZONES: zones,
                            CONF_LOCATIONS: locations,
                            CONF_ENABLE_ALERTS: user_input[CONF_ENABLE_ALERTS],
                            CONF_ENABLE_GLM: user_input[CONF_ENABLE_GLM],
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ZONES,
                    default=(user_input or {}).get(CONF_ZONES, ["zone.home"]),
                ): _ZONE_SELECTOR,
                **_feature_schema(user_input or {}),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> WxAlertsOptionsFlow:
        return WxAlertsOptionsFlow()


class WxAlertsOptionsFlow(OptionsFlow):
    """Options: change zones, features, and lightning tuning.

    Zones are re-resolved on save, so moving a zone or adding a county
    never needs a remove/re-add of the integration.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        current = {**entry.data, **entry.options}
        errors: dict[str, str] = {}

        if user_input is not None:
            locations, _skipped = await _resolve_zones(
                self.hass, user_input[CONF_ZONES]
            )
            if not locations:
                errors["base"] = "no_locations"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_ZONES: user_input[CONF_ZONES],
                        CONF_LOCATIONS: locations,
                        CONF_ENABLE_ALERTS: user_input[CONF_ENABLE_ALERTS],
                        CONF_ENABLE_GLM: user_input[CONF_ENABLE_GLM],
                        CONF_GLM_PRECISION: user_input[CONF_GLM_PRECISION],
                        CONF_GLM_WINDOW: user_input[CONF_GLM_WINDOW],
                        CONF_PHENOMENON_SENSORS: user_input[CONF_PHENOMENON_SENSORS],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ZONES, default=current.get(CONF_ZONES, [])
                ): _ZONE_SELECTOR,
                **_feature_schema(current),
                vol.Required(
                    CONF_GLM_PRECISION,
                    default=current.get(CONF_GLM_PRECISION, DEFAULT_GLM_PRECISION),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=3, max=5, step=1, mode=selector.NumberSelectorMode.SLIDER
                    )
                ),
                vol.Required(
                    CONF_GLM_WINDOW,
                    default=current.get(CONF_GLM_WINDOW, DEFAULT_GLM_WINDOW),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=60, step=5, mode=selector.NumberSelectorMode.SLIDER
                    )
                ),
                vol.Required(
                    CONF_PHENOMENON_SENSORS,
                    default=current.get(
                        CONF_PHENOMENON_SENSORS, DEFAULT_PHENOMENON_SENSORS
                    ),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
