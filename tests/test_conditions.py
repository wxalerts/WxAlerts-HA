"""Current conditions per place: sensors and a weather entity from the
retained conditions/place/<place_id> topic."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.wxalerts.weather import map_condition

from .conftest import feed, make_entry, start
from .const import TOMBSTONE, conditions, conditions_topic, encode

WEATHER = "weather.wxalerts_home"
TEMPERATURE = "sensor.wxalerts_home_temperature"
SENSORS = {
    TEMPERATURE: ("24.4", "temperature", "°C"),
    "sensor.wxalerts_home_dew_point": ("21.1", "temperature", "°C"),
    "sensor.wxalerts_home_feels_like": ("25.6", "temperature", "°C"),
    "sensor.wxalerts_home_humidity": ("82", "humidity", "%"),
    "sensor.wxalerts_home_wind_speed": ("18.5", "wind_speed", "km/h"),
    "sensor.wxalerts_home_wind_gust": ("33.3", "wind_speed", "km/h"),
    "sensor.wxalerts_home_wind_bearing": ("200", "wind_direction", "°"),
    "sensor.wxalerts_home_pressure": ("1012.9", "atmospheric_pressure", "hPa"),
    "sensor.wxalerts_home_visibility": ("16.1", "distance", "km"),
    "sensor.wxalerts_home_precipitation_last_hour": ("1.3", "precipitation", "mm"),
}


async def test_entities_exist_and_wait_for_an_observation(hass, setup_integration):
    for entity_id in (*SENSORS, WEATHER, "sensor.wxalerts_home_condition"):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id
    assert hass.states.get("weather.wxalerts_cabin") is not None


async def test_an_observation_fills_every_sensor(hass, setup_integration):
    await feed(hass, setup_integration, conditions_topic(101), encode(conditions()))

    for entity_id, (value, device_class, unit) in SENSORS.items():
        state = hass.states.get(entity_id)
        assert state.state == value, entity_id
        assert state.attributes["device_class"] == device_class, entity_id
        assert state.attributes["unit_of_measurement"] == unit, entity_id
        assert state.attributes["station"] == "Whiting Field"
    assert hass.states.get(TEMPERATURE).attributes["state_class"] == "measurement"
    condition = hass.states.get("sensor.wxalerts_home_condition")
    assert condition.state == "Light Rain"
    assert condition.attributes["condition_code"] == "rain"
    # The other place has its own topic.
    assert hass.states.get("sensor.wxalerts_cabin_temperature").state == STATE_UNAVAILABLE


async def test_the_weather_entity_reports_current_conditions(hass, setup_integration):
    await feed(hass, setup_integration, conditions_topic(101), encode(conditions()))

    state = hass.states.get(WEATHER)
    assert state.state == "rainy"
    assert state.attributes["temperature"] == 24.4
    assert state.attributes["humidity"] == 82
    assert state.attributes["pressure"] == 1012.9
    assert state.attributes["wind_bearing"] == 200
    assert state.attributes["station"] == "Whiting Field"
    assert state.attributes["station_id"] == "KNDZ"
    assert state.attributes["distance_km"] == 9.4
    assert state.attributes["observed_at"].endswith("Z")


@pytest.mark.parametrize(("is_day", "expected"), [(True, "sunny"), (False, "clear-night")])
async def test_a_clear_sky_follows_is_day(hass, setup_integration, is_day, expected):
    # The sun says the opposite; the observation's own is_day wins.
    with patch("custom_components.wxalerts.weather.is_up", return_value=not is_day):
        await feed(
            hass,
            setup_integration,
            conditions_topic(101),
            encode(conditions(condition_code="clear", condition="Clear", is_day=is_day)),
        )
        assert hass.states.get(WEATHER).state == expected


@pytest.mark.parametrize(("daytime", "expected"), [(True, "sunny"), (False, "clear-night")])
async def test_without_is_day_a_clear_sky_follows_the_sun(
    hass, setup_integration, daytime, expected
):
    with patch("custom_components.wxalerts.weather.is_up", return_value=daytime):
        await feed(
            hass,
            setup_integration,
            conditions_topic(101),
            encode(conditions(condition_code="clear", condition="Clear", is_day=None)),
        )
        assert hass.states.get(WEATHER).state == expected


async def test_units_follow_the_unit_system(hass, mock_feed_client, enable_custom_integrations):
    hass.config.units = US_CUSTOMARY_SYSTEM
    coordinator = await start(hass, make_entry())
    await feed(hass, coordinator, conditions_topic(101), encode(conditions()))

    state = hass.states.get(TEMPERATURE)
    assert state.attributes["unit_of_measurement"] == "°F"
    assert float(state.state) == pytest.approx(75.9, abs=0.1)
    assert hass.states.get(WEATHER).attributes["temperature_unit"] == "°F"


async def test_missing_fields_are_unknown(hass, setup_integration):
    await feed(
        hass,
        setup_integration,
        conditions_topic(101),
        encode(conditions(wind_gust_kmh=None, visibility_km=None, condition_code=None)),
    )
    assert hass.states.get("sensor.wxalerts_home_wind_gust").state == STATE_UNKNOWN
    assert hass.states.get("sensor.wxalerts_home_visibility").state == STATE_UNKNOWN
    assert hass.states.get(TEMPERATURE).state == "24.4"
    assert hass.states.get(WEATHER).state == STATE_UNKNOWN


async def test_an_old_observation_is_unavailable(hass, setup_integration):
    await feed(
        hass, setup_integration, conditions_topic(101), encode(conditions(age_seconds=3 * 3600))
    )
    assert hass.states.get(TEMPERATURE).state == STATE_UNAVAILABLE
    assert hass.states.get(WEATHER).state == STATE_UNAVAILABLE


async def test_an_observation_ages_out_with_no_messages(hass, freezer, setup_integration):
    await feed(hass, setup_integration, conditions_topic(101), encode(conditions()))
    assert hass.states.get(TEMPERATURE).state == "24.4"

    freezer.tick(timedelta(hours=2))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert hass.states.get(TEMPERATURE).state == STATE_UNAVAILABLE
    assert hass.states.get(WEATHER).state == STATE_UNAVAILABLE


async def test_an_empty_message_clears_the_observation(hass, setup_integration):
    await feed(hass, setup_integration, conditions_topic(101), encode(conditions()))
    await feed(hass, setup_integration, conditions_topic(101), TOMBSTONE)
    assert hass.states.get(TEMPERATURE).state == STATE_UNAVAILABLE


async def test_an_undecodable_observation_is_ignored(hass, setup_integration):
    await feed(hass, setup_integration, conditions_topic(101), encode(conditions()))
    await feed(hass, setup_integration, conditions_topic(101), b"{nope")
    await feed(hass, setup_integration, conditions_topic(101), encode([1]))
    assert hass.states.get(TEMPERATURE).state == "24.4"


@pytest.mark.parametrize(
    ("code", "daytime", "expected"),
    [
        ("rain", True, "rainy"),
        ("Partly Cloudy", True, "partlycloudy"),
        ("partly-cloudy", False, "partlycloudy"),
        ("tsra", True, "lightning-rainy"),
        ("snow_sleet", True, "snowy-rainy"),
        ("fog", True, "fog"),
        ("wind_bkn", True, "windy-variant"),
        ("skc", False, "clear-night"),
        ("tornado", True, "exceptional"),
        ("something_new", True, None),
        (None, True, None),
        ("", True, None),
    ],
)
def test_map_condition(code, daytime, expected):
    assert map_condition(code, daytime) == expected


EXPECTED_SERVER_CODES = {
    "clear": ("sunny", "clear-night"),
    "clear_night": ("clear-night", "clear-night"),
    "partly_cloudy": ("partlycloudy", "partlycloudy"),
    "cloudy": ("cloudy", "cloudy"),
    "overcast": ("cloudy", "cloudy"),
    "fog": ("fog", "fog"),
    "haze": ("fog", "fog"),
    "smoke": ("fog", "fog"),
    "dust": ("fog", "fog"),
    "drizzle": ("rainy", "rainy"),
    "rain": ("rainy", "rainy"),
    "heavy_rain": ("pouring", "pouring"),
    "showers": ("rainy", "rainy"),
    "freezing_rain": ("snowy-rainy", "snowy-rainy"),
    "sleet": ("snowy-rainy", "snowy-rainy"),
    "snow": ("snowy", "snowy"),
    "heavy_snow": ("snowy", "snowy"),
    "rain_snow": ("snowy-rainy", "snowy-rainy"),
    "hail": ("hail", "hail"),
    "thunderstorm": ("lightning-rainy", "lightning-rainy"),
    "squall": ("windy-variant", "windy-variant"),
    "funnel_cloud": ("exceptional", "exceptional"),
    "tornado": ("exceptional", "exceptional"),
}


def test_every_server_condition_code_maps_to_a_home_assistant_condition():
    from homeassistant.components import weather

    from custom_components.wxalerts.const import CONDITION_CODES

    valid = {
        value
        for name, value in vars(weather).items()
        if name.startswith("ATTR_CONDITION_") and name != "ATTR_CONDITION_CLASS"
    }

    assert set(CONDITION_CODES) == set(EXPECTED_SERVER_CODES)
    for code in CONDITION_CODES:
        day, night = EXPECTED_SERVER_CODES[code]
        assert map_condition(code, True) == day, code
        assert map_condition(code, False) == night, code
        assert {day, night} <= valid, code


RAW = dict(
    temperature_c=3.97677563031894,
    dewpoint_c=-1.04999,
    feels_like_c=0.333333,
    relative_humidity=71.6667,
    wind_speed_kmh=12.96000001,
    wind_gust_kmh=27.7777,
    wind_direction_deg=247.5,
    pressure_hpa=1013.2499,
    visibility_km=16.09344,
    precip_last_hour_mm=0.254,
)


async def test_long_tails_of_digits_are_rounded(hass, setup_integration):
    await feed(hass, setup_integration, conditions_topic(101), encode(conditions(**RAW)))

    expected = {
        TEMPERATURE: ("4.0", 1),
        "sensor.wxalerts_home_dew_point": ("-1.0", 1),
        "sensor.wxalerts_home_feels_like": ("0.3", 1),
        "sensor.wxalerts_home_humidity": ("72", 0),
        "sensor.wxalerts_home_wind_speed": ("13.0", 1),
        "sensor.wxalerts_home_wind_gust": ("27.8", 1),
        "sensor.wxalerts_home_wind_bearing": ("248", 0),
        "sensor.wxalerts_home_pressure": ("1013.2", 1),
        "sensor.wxalerts_home_visibility": ("16.1", 1),
        "sensor.wxalerts_home_precipitation_last_hour": ("0.3", 1),
    }
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entity_id, (value, precision) in expected.items():
        assert hass.states.get(entity_id).state == value, entity_id
        options = registry.async_get(entity_id).options.get("sensor", {})
        assert options.get("suggested_display_precision") == precision, entity_id

    weather = hass.states.get(WEATHER).attributes
    assert weather["temperature"] == 4.0
    assert weather["humidity"] == 72
    assert weather["wind_bearing"] == 248
    assert weather["pressure"] == 1013.2
    assert weather["wind_speed"] == 13.0


async def test_forecast_values_are_rounded(hass, setup_integration):
    from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN, SERVICE_GET_FORECASTS

    from .const import forecast, forecast_topic

    payload = forecast()
    payload["hourly"][2].update(temperature_c=21.456, humidity=63.5, wind_speed_kmh=7.04, precip_mm=0.125)
    payload["daily"][1].update(temp_high_c=30.04, temp_low_c=19.96, uv_index=7.6)
    await feed(hass, setup_integration, forecast_topic(101), encode(payload))

    async def get(kind):
        response = await hass.services.async_call(
            WEATHER_DOMAIN, SERVICE_GET_FORECASTS,
            {"entity_id": WEATHER, "type": kind}, blocking=True, return_response=True,
        )
        return response[WEATHER]["forecast"]

    hour = (await get("hourly"))[0]
    assert hour["temperature"] == 21.5
    assert hour["humidity"] == 64
    assert hour["wind_speed"] == 7.0
    assert hour["precipitation"] == 0.1
    day = (await get("daily"))[0]
    assert day["temperature"] == 30.0
    assert day["templow"] == 20.0
    assert day["uv_index"] == 8


@pytest.mark.parametrize(
    ("station_id", "station_name", "expected"),
    [
        ("aprs:KN4OQW-13", None, "KN4OQW-13"),
        ("cwop:EW1234", None, "EW1234"),
        ("KNDZ", None, "KNDZ"),
        ("KNDZ", "Whiting Field", "Whiting Field"),
    ],
)
async def test_the_station_label_falls_back_to_the_id(
    hass, setup_integration, station_id, station_name, expected
):
    await feed(
        hass,
        setup_integration,
        conditions_topic(101),
        encode(conditions(station_id=station_id, station_name=station_name)),
    )
    assert hass.states.get(WEATHER).attributes["station"] == expected
    assert hass.states.get(TEMPERATURE).attributes["station"] == expected
    assert hass.states.get(WEATHER).attributes["station_id"] == station_id
