"""A weather entity per place: the latest observation near it, plus the
place's daily and hourly forecast.

Current conditions and the forecast are independent: either one missing or
stale leaves the other working.
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
    Forecast,
    WeatherEntity,
    WeatherEntityFeature,
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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.sun import is_up
from homeassistant.util import dt as dt_util

from . import WxAlertsConfigEntry
from .const import (
    FC_APPARENT_TEMPERATURE_C,
    FC_CLOUD_COVER,
    FC_CONDITION_CODE,
    FC_DAILY,
    FC_DATE,
    FC_DATETIME,
    FC_DEWPOINT_C,
    FC_HOURLY,
    FC_HUMIDITY,
    FC_IS_DAYTIME,
    FC_PRECIP_MM,
    FC_PRECIP_PROBABILITY,
    FC_TEMP_HIGH_C,
    FC_TEMP_LOW_C,
    FC_TEMPERATURE_C,
    FC_UV_INDEX,
    FC_WIND_DIRECTION_DEG,
    FC_WIND_GUST_KMH,
    FC_WIND_SPEED_KMH,
    SIGNAL_FORECAST_UPDATED,
    SIGNAL_TICK,
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
from .entity import WxAlertsConditionsEntity, WxAlertsPlaceEntity

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


def _int(entry: dict[str, Any], field: str) -> int | None:
    value = _number(entry, field)
    return None if value is None else round(value)


def _when(entry: dict[str, Any]) -> Any:
    raw = entry.get(FC_DATETIME)
    return dt_util.parse_datetime(raw) if isinstance(raw, str) else None


def daily_forecast(forecast: dict[str, Any] | None) -> list[Forecast] | None:
    """Home Assistant daily Forecast dicts, from today on, native metric."""
    if not forecast or not isinstance(forecast.get(FC_DAILY), list):
        return None
    today = dt_util.now().date()
    days: list[Forecast] = []
    for entry in forecast[FC_DAILY]:
        if not isinstance(entry, dict):
            continue
        when = _when(entry)
        date_text = entry.get(FC_DATE)
        day = dt_util.parse_date(date_text) if isinstance(date_text, str) else None
        if day is None and when is not None:
            day = dt_util.as_local(when).date()
        if when is None and day is None:
            continue
        if day is not None and day < today:
            continue
        days.append(
            Forecast(
                datetime=(when.isoformat() if when else f"{day.isoformat()}T00:00:00+00:00"),
                condition=map_condition(entry.get(FC_CONDITION_CODE), True),
                native_temperature=_number(entry, FC_TEMP_HIGH_C),
                native_templow=_number(entry, FC_TEMP_LOW_C),
                precipitation_probability=_int(entry, FC_PRECIP_PROBABILITY),
                native_precipitation=_number(entry, FC_PRECIP_MM),
                native_wind_speed=_number(entry, FC_WIND_SPEED_KMH),
                native_wind_gust_speed=_number(entry, FC_WIND_GUST_KMH),
                wind_bearing=_number(entry, FC_WIND_DIRECTION_DEG),
                humidity=_number(entry, FC_HUMIDITY),
                uv_index=_number(entry, FC_UV_INDEX),
            )
        )
    return days


def hourly_forecast(forecast: dict[str, Any] | None) -> list[Forecast] | None:
    """Home Assistant hourly Forecast dicts from the current hour on.

    An hour is dropped once it is over; the one in progress stays. Clear
    reads sunny or clear-night by the entry's own is_daytime.
    """
    if not forecast or not isinstance(forecast.get(FC_HOURLY), list):
        return None
    now = dt_util.utcnow()
    hours: list[Forecast] = []
    for entry in forecast[FC_HOURLY]:
        if not isinstance(entry, dict):
            continue
        when = _when(entry)
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt_util.UTC)
        if (now - when).total_seconds() >= 3600:
            continue
        daytime = entry.get(FC_IS_DAYTIME)
        hours.append(
            Forecast(
                datetime=when.isoformat(),
                condition=map_condition(
                    entry.get(FC_CONDITION_CODE), daytime is not False
                ),
                native_temperature=_number(entry, FC_TEMPERATURE_C),
                native_apparent_temperature=_number(entry, FC_APPARENT_TEMPERATURE_C),
                native_dew_point=_number(entry, FC_DEWPOINT_C),
                humidity=_number(entry, FC_HUMIDITY),
                precipitation_probability=_int(entry, FC_PRECIP_PROBABILITY),
                native_precipitation=_number(entry, FC_PRECIP_MM),
                native_wind_speed=_number(entry, FC_WIND_SPEED_KMH),
                native_wind_gust_speed=_number(entry, FC_WIND_GUST_KMH),
                wind_bearing=_number(entry, FC_WIND_DIRECTION_DEG),
                cloud_coverage=_int(entry, FC_CLOUD_COVER),
                is_daytime=daytime if isinstance(daytime, bool) else None,
            )
        )
    return hours


class PlaceWeather(WxAlertsConditionsEntity, WeatherEntity):
    """Current conditions at the nearest station to the place."""

    # Named after the device alone: weather.wxalerts_home.
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_native_visibility_unit = UnitOfLength.KILOMETERS
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    )

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "weather")
        # What the forecast cards were last sent, so the clock only pushes an
        # update when an hour or a day has actually rolled off.
        self._forecast_shape: tuple[int, int] | None = None

    @property
    def available(self) -> bool:
        # Either half is worth showing on its own: a station that stopped
        # reporting must not hide the forecast, nor the reverse.
        return WxAlertsPlaceEntity.available.fget(self) and (
            self.observation is not None or self._forecast is not None
        )

    @property
    def _forecast(self) -> dict[str, Any] | None:
        return self.coordinator.forecast_for(self.place_id)

    async def async_forecast_daily(self) -> list[Forecast] | None:
        return daily_forecast(self._forecast)

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        return hourly_forecast(self._forecast)

    def _shape(self) -> tuple[int, int]:
        forecast = self._forecast
        return (
            len(daily_forecast(forecast) or []),
            len(hourly_forecast(forecast) or []),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        entry_id = self.coordinator.entry.entry_id
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_FORECAST_UPDATED}_{entry_id}_{self.place_id}",
                self._handle_forecast,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_TICK}_{entry_id}", self._handle_forecast_tick
            )
        )

    @callback
    def _handle_forecast(self, *_args: Any) -> None:
        self._forecast_shape = self._shape()
        self.async_write_ha_state()
        self.hass.async_create_task(self.async_update_listeners(None))

    @callback
    def _handle_forecast_tick(self, *_args: Any) -> None:
        shape = self._shape()
        if shape != self._forecast_shape:
            self._forecast_shape = shape
            self.hass.async_create_task(self.async_update_listeners(None))

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
