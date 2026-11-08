"""Setup, upgrade from 0.x, and diagnostics."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wxalerts.const import (
    API_INSTANCES_URL,
    OAUTH_AUTHORIZE_URL,
    OAUTH_TOKEN_URL,
    CONF_EVENT_SENSORS,
    CONF_PLACES,
    CONFIG_VERSION,
    DOMAIN,
)
from custom_components.wxalerts.diagnostics import async_get_config_entry_diagnostics

from .conftest import feed, make_entry
from .test_coordinator import mock_heartbeat
from .const import (
    ACCESS_TOKEN,
    PASSWORD,
    PLACES,
    REFRESH_TOKEN,
    TOPIC_PLACES,
    TOPIC_STATUS,
    USERNAME,
    encode,
    registration,
    status,
    storm,
    storm_topic,
)

# A v0.4.0 entry exactly as that release wrote it: zones resolved to counties,
# lightning on, the shared public login implied, no account at all.
V040_DATA = {
    "zones": ["zone.home"],
    "locations": [
        {
            "zone_entity_id": "zone.home",
            "name": "Home",
            "latitude": 30.6435,
            "longitude": -87.0545,
            "same": "012113",
            "county": "FLC113",
            "state": "FL",
            "geohash": "dj6n7",
        }
    ],
    "enable_alerts": True,
    "enable_glm": True,
    "event_sensors": ["tornado_warning", "flash_flood_warning"],
}
V040_OPTIONS = {
    "glm_radius_mi": 30,
    "glm_alert_mi": 10,
    "glm_window_minutes": 30,
    "glm_track_persons": True,
    "glm_backfill_url": "",
    "event_sensors": ["tornado_warning", "tornado_watch"],
}


def _v040_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="WxAlerts",
        unique_id=DOMAIN,
        version=1,
        data=V040_DATA,
        options=V040_OPTIONS,
    )


def _seed_v040_registry(hass, entry):
    """What 0.4.0 left in the registries: a county device with alert sensors,
    a lightning device per zone and per person, and their entities."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    county = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_012113")},
        name="WxAlerts Home",
    )
    lightning = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_lightning_zone.home")},
        name="Lightning Home",
    )
    person = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_lightning_person.sam")},
        name="Lightning Sam",
    )
    seeded = [
        ("binary_sensor", f"{entry.entry_id}_012113_active", county),
        ("binary_sensor", f"{entry.entry_id}_012113_event_tornado_warning", county),
        ("sensor", f"{entry.entry_id}_012113_severity", county),
        ("sensor", f"{entry.entry_id}_glm_zone.home_nearest", lightning),
        ("binary_sensor", f"{entry.entry_id}_glm_zone.home_nearby", lightning),
        ("sensor", f"{entry.entry_id}_glm_person.sam_rate", person),
    ]
    for platform, unique_id, device in seeded:
        entity_registry.async_get_or_create(
            platform, DOMAIN, unique_id, config_entry=entry, device_id=device.id
        )
    return len(seeded)


async def test_a_v040_entry_is_migrated_and_asks_for_the_login(
    hass, mock_feed_client, enable_custom_integrations
):
    entry = _v040_entry()
    entry.add_to_hass(hass)
    seeded = _seed_v040_registry(hass, entry)
    assert len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) == seeded

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Migrated: new version, lightning and zones gone, the selection kept.
    assert entry.version == CONFIG_VERSION
    assert entry.data == {CONF_PLACES: []}
    assert entry.options == {CONF_EVENT_SENSORS: ["tornado_warning", "tornado_watch"]}
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []
    assert dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id) == []

    # Not a silent failure: setup stops for credentials and asks for them.
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    assert flows[0]["step_id"] == "reauth_confirm"
    mock_feed_client.assert_not_called()


async def test_a_migrated_entry_comes_up_after_signing_in(
    hass, aioclient_mock, mock_feed_client, enable_custom_integrations,
    current_request_with_host,
):
    from urllib.parse import parse_qs, urlparse

    from homeassistant.helpers.config_entry_oauth2_flow import _decode_jwt

    entry = _v040_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flow = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]

    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert result["url"].startswith(OAUTH_AUTHORIZE_URL)
    state = _decode_jwt(hass, parse_qs(urlparse(result["url"]).query)["state"][0])
    aioclient_mock.post(
        OAUTH_TOKEN_URL,
        json={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    )
    aioclient_mock.post(API_INSTANCES_URL, json=registration())
    await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"code": "c", "state": state}
    )
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_USERNAME] == USERNAME
    await feed(hass, entry.runtime_data, TOPIC_PLACES, encode(PLACES))
    assert hass.states.get("binary_sensor.wxalerts_home_storm_cell_approaching")
    # The old selection carried over onto the place.
    assert hass.states.get("binary_sensor.wxalerts_home_tornado_watch")


async def test_a_030_entry_with_only_the_bucket_switch_keeps_the_defaults(
    hass, enable_custom_integrations
):
    from custom_components.wxalerts.events import DEFAULT_EVENT_KEYS

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        version=1,
        data={"zones": ["zone.home"], "locations": [], "enable_alerts": True},
        options={"phenomenon_sensors": True},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.options[CONF_EVENT_SENSORS] == list(DEFAULT_EVENT_KEYS)


async def test_an_entry_from_a_future_version_is_refused(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, version=CONFIG_VERSION + 1)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_an_entry_without_a_sign_in_asks_for_one(
    hass, mock_feed_client, enable_custom_integrations
):
    entry = make_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != "token"}
    )
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_a_missing_broker_login_is_registered_at_setup(
    hass, aioclient_mock, mock_feed_client, enable_custom_integrations
):
    entry = make_entry(**{CONF_PASSWORD: ""})
    mock_heartbeat(aioclient_mock)
    aioclient_mock.post(API_INSTANCES_URL, json=registration())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data[CONF_PASSWORD] == PASSWORD
    assert entry.state is ConfigEntryState.LOADED


async def test_an_unreachable_api_at_setup_retries(
    hass, aioclient_mock, mock_feed_client, enable_custom_integrations
):
    entry = make_entry(**{CONF_PASSWORD: ""})
    mock_heartbeat(aioclient_mock, status=503)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_stale_place_device_can_be_deleted_and_a_live_one_cannot(
    hass, config_entry, setup_integration
):
    from custom_components.wxalerts import async_remove_config_entry_device

    device_registry = dr.async_get(hass)
    live = device_registry.async_get_device(
        identifiers={(DOMAIN, f"{config_entry.entry_id}_place_101")}
    )
    gone = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, f"{config_entry.entry_id}_place_999")},
    )
    assert not await async_remove_config_entry_device(hass, config_entry, live)
    assert await async_remove_config_entry_device(hass, config_entry, gone)


# ===========================================================================
# Diagnostics
# ===========================================================================
async def test_diagnostics_redact_the_login_and_coordinates(
    hass, config_entry, setup_integration
):
    await feed(hass, setup_integration, TOPIC_STATUS, encode(status()))
    await feed(hass, setup_integration, storm_topic(101), encode(storm()))

    result = await async_get_config_entry_diagnostics(hass, config_entry)

    text = repr(result)
    assert PASSWORD not in text
    assert USERNAME not in text
    assert "30.6435" not in text
    assert ACCESS_TOKEN not in text and REFRESH_TOKEN not in text
    assert result["entry"]["data"][CONF_USERNAME] == "**REDACTED**"
    assert result["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"
    assert result["entry"]["data"]["token"] == "**REDACTED**"
    assert result["entry"]["data"]["instance_id"] == "**REDACTED**"
    assert result["entry"]["data"][CONF_PLACES][0]["lat"] == "**REDACTED**"
    assert result["subscriptions"][0] == {"topic": "wxalerts/u/**REDACTED**/#", "qos": 1}
    state = result["state"]
    assert state["connected"] is True
    assert state["place_count"] == 2
    assert state["counties"] == ["012113", "001001"]
    assert state["storms_per_place"]["101"][0]["rung"] == 40
    assert state["feed_online"] is True


async def test_a_revoked_instance_is_not_re_registered_at_setup(
    hass, aioclient_mock, mock_feed_client, enable_custom_integrations
):
    entry = make_entry(**{CONF_PASSWORD: ""})
    mock_heartbeat(aioclient_mock, active=False, reason="revoked")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert all(str(call[1]) != API_INSTANCES_URL for call in aioclient_mock.mock_calls)
