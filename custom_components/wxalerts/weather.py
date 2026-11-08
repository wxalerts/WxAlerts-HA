"""A weather entity per place: the latest observation near it.

Current conditions only. The feed carries no forecast, so none is offered.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.weather import (
    ATTR_CONDITION_CLEAR_NIGHT,
    ATTR_CONDITION_CLOUDY,
    ATTR_CONDITION_EXCEPTIONAL,
    ATTR_CONDITION_FOG,
    ATTR_CONDITION_HAIL,
    ATTR_CONDITION_LIGHTNING,
    ATTR_CONDITION_LIGHTNING_RAINY,
    ATTR_CONDITION_PARTLYCLOUDY,
    ATTR_CONDITION_POURING,
    ATTR_CONDITION_RAINY,
    ATTR_CONDITION_SNOWY,
    ATTR_CONDITION_SNOWY_RAINY,
    ATTR_CONDITION_SUNNY,
    ATTR_CONDITION_WINDY,
    ATTR_CONDITION_WINDY_VARIANT,
    WeatherEntity,
)
from homeassistant.const import (
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.sun import is_up

from . import WxAlertsConfigEntry
from .const import (
    COND_CONDITION_CODE,
    COND_DEWPOINT_C,
    COND_FEELS_LIKE_C,
    COND_HUMIDITY,
    COND_PRESSURE_HPA,
    COND_TEMPERATURE_C,
    COND_VISIBILITY_KM,
    COND_WIND_DIRECTION_DEG,
    COND_WIND_GUST_KMH,
    COND_WIND_SPEED_KMH,
)
from .coordinator import Place, WxAlertsCoordinator
from .entity import WxAlertsConditionsEntity

# A clear sky reads "sunny" by day and "clear-night" by night; every other
# condition is the same either way.
_CLEAR = "clear"

# Stable condition codes to Home Assistant conditions. Covers plain names and
# the NWS observation icon codes, since observations are NWS sourced; a Home
# Assistant condition name passes through as itself.
CONDITION_MAP: dict[str, str] = {
    # Sky
    "clear": _CLEAR,
    "skc": _CLEAR,
    "sunny": _CLEAR,
    "mostly_clear": _CLEAR,
    "few": _CLEAR,
    "fair": _CLEAR,
    "partly_cloudy": ATTR_CONDITION_PARTLYCLOUDY,
    "partlycloudy": ATTR_CONDITION_PARTLYCLOUDY,
    "sct": ATTR_CONDITION_PARTLYCLOUDY,
    "mostly_cloudy": ATTR_CONDITION_CLOUDY,
    "bkn": ATTR_CONDITION_CLOUDY,
    "cloudy": ATTR_CONDITION_CLOUDY,
    "overcast": ATTR_CONDITION_CLOUDY,
    "ovc": ATTR_CONDITION_CLOUDY,
    # Wind
    "wind_skc": ATTR_CONDITION_WINDY,
    "wind_few": ATTR_CONDITION_WINDY,
    "wind_sct": ATTR_CONDITION_WINDY_VARIANT,
    "wind_bkn": ATTR_CONDITION_WINDY_VARIANT,
    "wind_ovc": ATTR_CONDITION_WINDY_VARIANT,
    "windy": ATTR_CONDITION_WINDY,
    "breezy": ATTR_CONDITION_WINDY,
    # Rain
    "rain": ATTR_CONDITION_RAINY,
    "rainy": ATTR_CONDITION_RAINY,
    "drizzle": ATTR_CONDITION_RAINY,
    "light_rain": ATTR_CONDITION_RAINY,
    "rain_showers": ATTR_CONDITION_RAINY,
    "rain_showers_hi": ATTR_CONDITION_RAINY,
    "showers": ATTR_CONDITION_RAINY,
    "heavy_rain": ATTR_CONDITION_POURING,
    "pouring": ATTR_CONDITION_POURING,
    # Thunder
    "tsra": ATTR_CONDITION_LIGHTNING_RAINY,
    "tsra_sct": ATTR_CONDITION_LIGHTNING_RAINY,
    "tsra_hi": ATTR_CONDITION_LIGHTNING_RAINY,
    "thunderstorm": ATTR_CONDITION_LIGHTNING_RAINY,
    "thunderstorms": ATTR_CONDITION_LIGHTNING_RAINY,
    "lightning_rainy": ATTR_CONDITION_LIGHTNING_RAINY,
    "lightning": ATTR_CONDITION_LIGHTNING,
    # Winter
    "snow": ATTR_CONDITION_SNOWY,
    "snowy": ATTR_CONDITION_SNOWY,
    "light_snow": ATTR_CONDITION_SNOWY,
    "heavy_snow": ATTR_CONDITION_SNOWY,
    "blizzard": ATTR_CONDITION_SNOWY,
    "rain_snow": ATTR_CONDITION_SNOWY_RAINY,
    "rain_sleet": ATTR_CONDITION_SNOWY_RAINY,
    "snow_sleet": ATTR_CONDITION_SNOWY_RAINY,
    "sleet": ATTR_CONDITION_SNOWY_RAINY,
    "fzra": ATTR_CONDITION_SNOWY_RAINY,
    "rain_fzra": ATTR_CONDITION_SNOWY_RAINY,
    "snow_fzra": ATTR_CONDITION_SNOWY_RAINY,
    "freezing_rain": ATTR_CONDITION_SNOWY_RAINY,
    "freezing_drizzle": ATTR_CONDITION_SNOWY_RAINY,
    "snowy_rainy": ATTR_CONDITION_SNOWY_RAINY,
    "hail": ATTR_CONDITION_HAIL,
    "ice_pellets": ATTR_CONDITION_HAIL,
    # Obscuration
    "fog": ATTR_CONDITION_FOG,
    "mist": ATTR_CONDITION_FOG,
    "haze": ATTR_CONDITION_FOG,
    "smoke": ATTR_CONDITION_FOG,
    "dust": ATTR_CONDITION_FOG,
    # Extremes
    "tornado": ATTR_CONDITION_EXCEPTIONAL,
    "funnel_cloud": ATTR_CONDITION_EXCEPTIONAL,
    "hurricane": ATTR_CONDITION_EXCEPTIONAL,
    "tropical_storm": ATTR_CONDITION_EXCEPTIONAL,
    "exceptional": ATTR_CONDITION_EXCEPTIONAL,
    "clear_night": ATTR_CONDITION_CLEAR_NIGHT,
    "clear-night": ATTR_CONDITION_CLEAR_NIGHT,
}


def map_condition(code: Any, daytime: bool) -> str | None:
    """A Home Assistant condition for a stable condition code, or None.

    Codes are compared lower-case with spaces and hyphens folded to
    underscores, so "Partly Cloudy" and "partly-cloudy" both land. An unknown
    code is None (state unknown) rather than a guess.
    """
    if not isinstance(code, str) or not code.strip():
        return None
    key = code.strip().lower().replace(" ", "_")
    mapped = CONDITION_MAP.get(key) or CONDITION_MAP.get(key.replace("-", "_"))
    if mapped is None:
        return None
    if mapped == _CLEAR:
        return ATTR_CONDITION_SUNNY if daytime else ATTR_CONDITION_CLEAR_NIGHT
    return mapped


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WxAlertsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    @callback
    def _places_added(places: list[Place]) -> None:
        async_add_entities(PlaceWeather(coordinator, place) for place in places)

    entry.async_on_unload(coordinator.add_place_listener(_places_added))
    async_add_entities(
        PlaceWeather(coordinator, place) for place in coordinator.places.values()
    )


def _number(observation: dict[str, Any], field: str) -> float | None:
    value = observation.get(field)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PlaceWeather(WxAlertsConditionsEntity, WeatherEntity):
    """Current conditions at the nearest station to the place."""

    # Named after the device alone: weather.wxalerts_home.
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_native_visibility_unit = UnitOfLength.KILOMETERS
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "weather")

    @property
    def _obs(self) -> dict[str, Any]:
        return self.observation or {}

    @property
    def condition(self) -> str | None:
        return map_condition(self._obs.get(COND_CONDITION_CODE), is_up(self.hass))

    @property
    def native_temperature(self) -> float | None:
        return _number(self._obs, COND_TEMPERATURE_C)

    @property
    def native_apparent_temperature(self) -> float | None:
        return _number(self._obs, COND_FEELS_LIKE_C)

    @property
    def native_dew_point(self) -> float | None:
        return _number(self._obs, COND_DEWPOINT_C)

    @property
    def humidity(self) -> float | None:
        return _number(self._obs, COND_HUMIDITY)

    @property
    def native_pressure(self) -> float | None:
        return _number(self._obs, COND_PRESSURE_HPA)

    @property
    def native_wind_speed(self) -> float | None:
        return _number(self._obs, COND_WIND_SPEED_KMH)

    @property
    def native_wind_gust_speed(self) -> float | None:
        return _number(self._obs, COND_WIND_GUST_KMH)

    @property
    def wind_bearing(self) -> float | None:
        return _number(self._obs, COND_WIND_DIRECTION_DEG)

    @property
    def native_visibility(self) -> float | None:
        return _number(self._obs, COND_VISIBILITY_KM)
