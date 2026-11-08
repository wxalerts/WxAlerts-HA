"""Daily and hourly forecast on the per-place weather entity, from the
retained forecast/place/<place_id> topic."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.components.weather import (
    DOMAIN as WEATHER_DOMAIN,
    SERVICE_GET_FORECASTS,
    WeatherEntityFeature,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import feed
from .const import (
    TOMBSTONE,
    conditions,
    conditions_topic,
    encode,
    forecast,
    forecast_topic,
)

WEATHER = "weather.wxalerts_home"


async def get(hass, kind: str) -> list:
    response = await hass.services.async_call(
        WEATHER_DOMAIN,
        SERVICE_GET_FORECASTS,
        {"entity_id": WEATHER, "type": kind},
        blocking=True,
        return_response=True,
    )
    return response[WEATHER]["forecast"]


async def test_the_entity_offers_daily_and_hourly(hass, setup_integration):
    features = hass.states.get(WEATHER).attributes["supported_features"]
    assert features & WeatherEntityFeature.FORECAST_DAILY
    assert features & WeatherEntityFeature.FORECAST_HOURLY
    assert "attribution" not in hass.states.get(WEATHER).attributes


async def test_daily_forecast_from_today(hass, setup_integration):
    await feed(hass, setup_integration, forecast_topic(101), encode(forecast()))

    days = await get(hass, "daily")
    # Yesterday is dropped; today plus five more remain.
    assert len(days) == 6
    today = days[0]
    assert today["condition"] == "lightning-rainy"
    assert today["temperature"] == 31.0
    assert today["templow"] == 22.0
    assert today["precipitation_probability"] == 60
    assert today["precipitation"] == 8.4
    assert today["wind_speed"] == 14.0
    assert today["wind_gust_speed"] == 40.0
    assert today["wind_bearing"] == 180
    assert today["humidity"] == 78
    assert today["uv_index"] == 9
    assert days[1]["condition"] == "partlycloudy"
    assert days[1].get("uv_index") is None


async def test_hourly_forecast_drops_finished_hours(hass, setup_integration):
    await feed(hass, setup_integration, forecast_topic(101), encode(forecast()))

    hours = await get(hass, "hourly")
    # Two hours ago and one hour ago are over; the current hour stays.
    assert len(hours) == 46
    first = dt_util.parse_datetime(hours[0]["datetime"])
    assert first == dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    hour = hours[0]
    assert hour["temperature"] == 25.0
    assert hour["apparent_temperature"] == 27.0
    assert hour["dew_point"] == 21.0
    assert hour["cloud_coverage"] == 5
    assert hour.get("wind_gust_speed") is None
    assert hour["precipitation_probability"] == 10


async def test_hourly_clear_follows_is_daytime(hass, setup_integration):
    await feed(hass, setup_integration, forecast_topic(101), encode(forecast()))
    hours = await get(hass, "hourly")
    by_daytime = {h["is_daytime"]: h["condition"] for h in hours[:2]}
    assert by_daytime == {True: "sunny", False: "clear-night"}


async def test_forecast_without_an_observation_keeps_the_entity_up(
    hass, setup_integration
):
    await feed(hass, setup_integration, forecast_topic(101), encode(forecast()))
    state = hass.states.get(WEATHER)
    assert state.state == STATE_UNKNOWN
    assert len(await get(hass, "daily")) == 6


async def test_an_observation_without_a_forecast_still_works(hass, setup_integration):
    await feed(hass, setup_integration, conditions_topic(101), encode(conditions()))
    assert hass.states.get(WEATHER).state == "rainy"
    assert await get(hass, "daily") == []


async def test_a_cleared_or_stale_forecast_is_withdrawn(hass, setup_integration):
    await feed(hass, setup_integration, forecast_topic(101), encode(forecast()))
    await feed(hass, setup_integration, forecast_topic(101), TOMBSTONE)
    assert hass.states.get(WEATHER).state == STATE_UNAVAILABLE

    await feed(
        hass,
        setup_integration,
        forecast_topic(101),
        encode(forecast(issued_age_seconds=25 * 3600)),
    )
    assert hass.states.get(WEATHER).state == STATE_UNAVAILABLE


async def test_an_undecodable_forecast_is_ignored(hass, setup_integration):
    await feed(hass, setup_integration, forecast_topic(101), encode(forecast()))
    await feed(hass, setup_integration, forecast_topic(101), b"[broken")
    await feed(hass, setup_integration, forecast_topic(101), encode([1]))
    assert len(await get(hass, "daily")) == 6


async def test_subscribers_get_new_forecasts_pushed(
    hass, hass_ws_client, setup_integration
):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "weather/subscribe_forecast",
            "forecast_type": "hourly",
            "entity_id": WEATHER,
        }
    )
    assert (await client.receive_json())["success"]
    first = await client.receive_json()
    assert first["event"]["forecast"] is None

    await feed(hass, setup_integration, forecast_topic(101), encode(forecast()))
    pushed = await client.receive_json()
    assert len(pushed["event"]["forecast"]) == 46


async def test_an_hour_rolling_off_pushes_an_update(
    hass, hass_ws_client, freezer, setup_integration
):
    await feed(hass, setup_integration, forecast_topic(101), encode(forecast()))
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "weather/subscribe_forecast",
            "forecast_type": "hourly",
            "entity_id": WEATHER,
        }
    )
    assert (await client.receive_json())["success"]
    assert len((await client.receive_json())["event"]["forecast"]) == 46

    freezer.tick(timedelta(hours=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert len((await client.receive_json())["event"]["forecast"]) == 45


@pytest.mark.parametrize("bad", [{"daily": "nope", "hourly": None}, {}])
async def test_a_malformed_forecast_offers_nothing(hass, setup_integration, bad):
    await feed(hass, setup_integration, conditions_topic(101), encode(conditions()))
    await feed(hass, setup_integration, forecast_topic(101), encode(bad))
    assert await get(hass, "daily") == []
    assert await get(hass, "hourly") == []
