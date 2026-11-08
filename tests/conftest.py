"""Shared fixtures.

Every test that reaches the coordinator patches ``FeedClient`` out; no test
in this suite opens a socket.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wxalerts.const import (
    CONF_EVENT_SENSORS,
    CONF_PLACES,
    CONFIG_VERSION,
    DOMAIN,
)
from custom_components.wxalerts.coordinator import WxAlertsCoordinator
from custom_components.wxalerts.events import DEFAULT_EVENT_KEYS

from .const import (
    ACCESS_TOKEN,
    CACHED_PLACES,
    INSTANCE_ID,
    PASSWORD,
    REFRESH_TOKEN,
    USERNAME,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def no_same_lookup():
    """Stub the api.weather.gov county lookup out of every test by default.

    It runs for places that arrive without a SAME code and is the one thing
    besides the broker that reaches for a socket. Yields the mock, so a test
    can assert it was called; the test of the real request keeps the original.
    """
    original = WxAlertsCoordinator._async_resolve_same
    with patch.object(
        WxAlertsCoordinator, "_async_resolve_same", new_callable=AsyncMock
    ) as lookup:
        lookup.original = original
        yield lookup


@pytest.fixture(autouse=True)
def no_heartbeat():
    """Stub the account heartbeat out of every test by default; it runs at
    setup and would otherwise need the API mocked everywhere. Yields the mock;
    tests of the real call use ``no_heartbeat.original``."""
    original = WxAlertsCoordinator._async_heartbeat
    with patch.object(
        WxAlertsCoordinator, "_async_heartbeat", new_callable=AsyncMock
    ) as heartbeat:
        heartbeat.original = original
        yield heartbeat


@pytest.fixture(autouse=True)
def oauth_registered(hass):
    """Entries are set up with the sign-in implementation available, as
    async_setup_entry registers it in production."""
    from custom_components.wxalerts.api import async_register_oauth

    async_register_oauth(hass)


@pytest.fixture
def mock_feed_client():
    """Replace the MQTT transport with a recorder.

    ``mock_feed_client.call_args`` carries the FeedConfig the coordinator
    built, which is how the subscription tests inspect what would have been
    subscribed.
    """
    with patch(
        "custom_components.wxalerts.coordinator.FeedClient", autospec=True
    ) as client_cls:
        instance = client_cls.return_value
        instance.start = MagicMock()
        instance.stop = AsyncMock()
        instance.set_subscriptions = AsyncMock()
        yield client_cls


def make_token(expires_in: float = 3600) -> dict:
    return {
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "expires_at": time.time() + expires_in,
    }


def make_entry(places=None, options=None, **data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="WxAlerts",
        unique_id=DOMAIN,
        version=CONFIG_VERSION,
        data={
            "auth_implementation": DOMAIN,
            "token": make_token(),
            "instance_id": INSTANCE_ID,
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_PLACES: CACHED_PLACES if places is None else places,
            **data,
        },
        options={CONF_EVENT_SENSORS: list(DEFAULT_EVENT_KEYS)}
        if options is None
        else options,
    )


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Two places: Home in Santa Rosa FL and Cabin in Autauga AL."""
    return make_entry()


async def start(hass, entry):
    """Set an entry up with a stubbed feed, connected and past priming."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    coordinator._handle_connection(True)
    coordinator._priming_until = 0.0
    await hass.async_block_till_done()
    return coordinator


@pytest.fixture
async def setup_integration(
    hass, config_entry, mock_feed_client, enable_custom_integrations
):
    """Set the integration up and return the coordinator.

    The coordinator is marked connected, because every entity reports
    ``available`` from the MQTT connection state, and priming is cleared so
    events fire as they would after the retained replay.
    """
    return await start(hass, config_entry)


async def feed(hass, coordinator, topic, payload):
    """Deliver one MQTT message and let the entities settle."""
    await coordinator._handle_message(topic, payload)
    await hass.async_block_till_done()
