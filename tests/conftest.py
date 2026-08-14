"""Shared fixtures.

Every test that reaches the coordinator patches ``FeedClient`` out — no test
in this suite opens a socket. The live-feed check is a separate script under
``scripts/``, deliberately not collected by pytest.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wxalerts.const import (
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_GLM_PRECISION,
    CONF_GLM_WINDOW,
    CONF_LOCATIONS,
    CONF_ZONES,
    DOMAIN,
)

from .const import LOCATION_AUTAUGA, LOCATION_SANTA_ROSA

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def mock_feed_client():
    """Replace the MQTT transport with a recorder.

    Yields the patched class; ``mock_feed_client.call_args`` carries the
    FeedConfig the coordinator built, which is how the subscription tests
    inspect what would have been subscribed.
    """
    with patch(
        "custom_components.wxalerts.coordinator.FeedClient", autospec=True
    ) as client_cls:
        instance = client_cls.return_value
        instance.start = MagicMock()
        instance.stop = AsyncMock()
        yield client_cls


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A two-county entry: Santa Rosa FL and Autauga AL."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="WxAlerts",
        unique_id=DOMAIN,
        data={
            CONF_ZONES: ["zone.home", "zone.cabin"],
            CONF_LOCATIONS: [LOCATION_SANTA_ROSA, LOCATION_AUTAUGA],
            CONF_ENABLE_ALERTS: True,
            CONF_ENABLE_GLM: True,
        },
        options={
            CONF_GLM_PRECISION: 3,
            CONF_GLM_WINDOW: 15,
        },
    )


@pytest.fixture
async def setup_integration(
    hass, config_entry, mock_feed_client, enable_custom_integrations
):
    """Set the integration up with a stubbed feed and return the coordinator.

    The coordinator is marked connected, because every entity in this
    integration reports ``available`` from the MQTT connection state and would
    otherwise render as ``unavailable``.
    """
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data
    coordinator._handle_connection(True)
    await hass.async_block_till_done()
    return coordinator
