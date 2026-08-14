"""Config and options flow.

Zone resolution is the one place the integration talks HTTP, and it happens
exactly once per zone at setup — so these tests pin both the happy path and
every way api.weather.gov can fail to answer.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wxalerts.const import (
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_GLM_PRECISION,
    CONF_GLM_WINDOW,
    CONF_LOCATIONS,
    CONF_PHENOMENON_SENSORS,
    CONF_ZONES,
    DOMAIN,
    LOC_COUNTY,
    LOC_GEOHASH,
    LOC_SAME,
)

from .const import LOCATION_SANTA_ROSA, SAME_AUTAUGA, SAME_SANTA_ROSA

POINTS_SANTA_ROSA = "https://api.weather.gov/points/30.6435,-87.0545"
POINTS_CABIN = "https://api.weather.gov/points/32.5361,-86.6435"
POINTS_OFFSHORE = "https://api.weather.gov/points/29.0,-87.5"


def points_response(county_ugc: str | None, state: str | None = "FL") -> dict:
    """A trimmed api.weather.gov /points body."""
    properties: dict = {
        "relativeLocation": {"properties": {"city": "Milton", "state": state}}
    }
    if county_ugc is not None:
        properties["county"] = f"https://api.weather.gov/zones/county/{county_ugc}"
    return {"properties": properties}


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    yield


@pytest.fixture
def zones(hass):
    """Three zones: a US county, a second county, and an offshore point."""
    hass.states.async_set(
        "zone.home",
        "zoning",
        {"latitude": 30.6435, "longitude": -87.0545, "friendly_name": "Home"},
    )
    hass.states.async_set(
        "zone.cabin",
        "zoning",
        {"latitude": 32.5361, "longitude": -86.6435, "friendly_name": "Cabin"},
    )
    hass.states.async_set(
        "zone.boat",
        "zoning",
        {"latitude": 29.0, "longitude": -87.5, "friendly_name": "Boat"},
    )
    # A zone with no coordinates at all — HA allows this for passive zones.
    hass.states.async_set("zone.nowhere", "zoning", {"friendly_name": "Nowhere"})


# ---------------------------------------------------------------------------
# Initial setup
# ---------------------------------------------------------------------------


async def test_user_flow_resolves_a_zone_to_a_county_and_a_geohash(
    hass, zones, aioclient_mock, mock_feed_client
):
    aioclient_mock.get(POINTS_SANTA_ROSA, json=points_response("FLC113"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home"],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "WxAlerts"

    location = result["data"][CONF_LOCATIONS][0]
    assert location[LOC_SAME] == SAME_SANTA_ROSA
    assert location[LOC_COUNTY] == "FLC113"
    assert location[LOC_GEOHASH] == "dj6n7"
    assert location["name"] == "Home"
    assert result["data"][CONF_ENABLE_ALERTS] is True


async def test_user_flow_resolves_several_zones(
    hass, zones, aioclient_mock, mock_feed_client
):
    aioclient_mock.get(POINTS_SANTA_ROSA, json=points_response("FLC113"))
    aioclient_mock.get(POINTS_CABIN, json=points_response("ALC001", state="AL"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home", "zone.cabin"],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [loc[LOC_SAME] for loc in result["data"][CONF_LOCATIONS]] == [
        SAME_SANTA_ROSA,
        SAME_AUTAUGA,
    ]


async def test_zone_without_coordinates_is_skipped_not_fatal(
    hass, zones, aioclient_mock, mock_feed_client
):
    aioclient_mock.get(POINTS_SANTA_ROSA, json=points_response("FLC113"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.nowhere", "zone.home"],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(result["data"][CONF_LOCATIONS]) == 1


async def test_no_usable_zones_is_an_error(hass, zones, aioclient_mock):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.nowhere"],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_locations"}


async def test_selecting_no_zones_is_an_error(hass, zones):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ZONES: [], CONF_ENABLE_ALERTS: True, CONF_ENABLE_GLM: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_zones"}


async def test_alerts_on_with_no_county_is_an_error(hass, zones, aioclient_mock):
    """An offshore-only setup with alerts enabled would create no entities at
    all; say so rather than shipping a silent no-op."""
    aioclient_mock.get(POINTS_OFFSHORE, json=points_response(None, state=None))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ZONES: ["zone.boat"], CONF_ENABLE_ALERTS: True, CONF_ENABLE_GLM: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_same_codes"}


async def test_offshore_zone_is_accepted_for_lightning_only(
    hass, zones, aioclient_mock, mock_feed_client
):
    aioclient_mock.get(POINTS_OFFSHORE, json=points_response(None, state=None))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ZONES: ["zone.boat"], CONF_ENABLE_ALERTS: False, CONF_ENABLE_GLM: True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    location = result["data"][CONF_LOCATIONS][0]
    assert location[LOC_SAME] is None
    assert location[LOC_GEOHASH] == "dj1ub"


async def test_a_forecast_zone_yields_no_same_code(hass, zones, aioclient_mock):
    """Marine points resolve to a zone URL, which has no SAME code."""
    aioclient_mock.get(
        POINTS_OFFSHORE,
        json={
            "properties": {
                "county": "https://api.weather.gov/zones/forecast/AMZ650",
                "relativeLocation": {"properties": {"state": None}},
            }
        },
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ZONES: ["zone.boat"], CONF_ENABLE_ALERTS: True, CONF_ENABLE_GLM: True},
    )

    assert result["errors"] == {"base": "no_same_codes"}


@pytest.mark.parametrize("status", [404, 429, 500, 503])
async def test_weather_gov_error_leaves_lightning_working(
    hass, zones, aioclient_mock, mock_feed_client, status
):
    """The zone still resolves — it just has no county, so the flow can be
    completed for lightning."""
    aioclient_mock.get(POINTS_SANTA_ROSA, status=status)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ZONES: ["zone.home"], CONF_ENABLE_ALERTS: False, CONF_ENABLE_GLM: True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    location = result["data"][CONF_LOCATIONS][0]
    assert location[LOC_SAME] is None
    assert location[LOC_GEOHASH] == "dj6n7"


async def test_weather_gov_unreachable_does_not_crash_the_flow(
    hass, zones, aioclient_mock, mock_feed_client
):
    aioclient_mock.get(POINTS_SANTA_ROSA, exc=TimeoutError)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ZONES: ["zone.home"], CONF_ENABLE_ALERTS: False, CONF_ENABLE_GLM: True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOCATIONS][0][LOC_SAME] is None


async def test_only_one_entry_per_instance(hass, zones, mock_feed_client):
    """One MQTT connection is the whole budget for an HA instance."""
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


async def test_options_flow_re_resolves_zones(
    hass, zones, aioclient_mock, mock_feed_client
):
    """Moving a zone or adding a county must not need a remove/re-add."""
    aioclient_mock.get(POINTS_SANTA_ROSA, json=points_response("FLC113"))
    aioclient_mock.get(POINTS_CABIN, json=points_response("ALC001", state="AL"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home", "zone.cabin"],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
            CONF_GLM_PRECISION: 5,
            CONF_GLM_WINDOW: 30,
            CONF_PHENOMENON_SENSORS: False,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [loc[LOC_SAME] for loc in entry.options[CONF_LOCATIONS]] == [
        SAME_SANTA_ROSA,
        SAME_AUTAUGA,
    ]
    assert entry.options[CONF_GLM_PRECISION] == 5
    assert entry.options[CONF_GLM_WINDOW] == 30


async def test_options_flow_rejects_a_selection_with_no_usable_zone(
    hass, zones, aioclient_mock, mock_feed_client
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.nowhere"],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
            CONF_GLM_PRECISION: 4,
            CONF_GLM_WINDOW: 15,
            CONF_PHENOMENON_SENSORS: True,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_locations"}


async def test_changing_options_reloads_the_entry(
    hass, zones, aioclient_mock, mock_feed_client
):
    """The new subscription set only takes effect on reload."""
    aioclient_mock.get(POINTS_SANTA_ROSA, json=points_response("FLC113"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    mock_feed_client.reset_mock()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ZONES: ["zone.home"],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
            CONF_GLM_PRECISION: 3,
            CONF_GLM_WINDOW: 45,
            CONF_PHENOMENON_SENSORS: True,
        },
    )
    await hass.async_block_till_done()

    # A reload builds a fresh client with the new box size.
    config = mock_feed_client.call_args[0][0]
    assert ("wxalerts/glm/v1/d/j/6/#", 0) in config.subscriptions
    assert entry.runtime_data.glm_window_minutes == 45
