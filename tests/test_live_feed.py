"""End-to-end against the production feed. Deselected by default.

    WXALERTS_HA_USERNAME=ha-... WXALERTS_HA_PASSWORD=... pytest -m live

The credentials are a broker login for one Home Assistant instance (from the
website's advanced "create credentials manually" option).

Everything else in this suite fakes the MQTT layer, which cannot tell you
that TLS, websockets, MQTT v5 or the login still work. These tests dial
``wss://mqtt.wxalerts.org/mqtt`` for real with a Supporter account's Home
Assistant login and drive the whole integration from it. They are subject to
the weather: nothing live is a valid result and is reported rather than failed.

Read-only throughout. The broker denies publish on every topic.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_socket

from custom_components.wxalerts.const import DEFAULT_HOST

from .conftest import make_entry

pytestmark = pytest.mark.live

USERNAME = os.environ.get("WXALERTS_HA_USERNAME", "")
PASSWORD = os.environ.get("WXALERTS_HA_PASSWORD", "")
CONNECT_TIMEOUT = 30
RETAINED_SETTLE = 8


async def wait_for(predicate, timeout: float, interval: float = 0.25) -> bool:
    for _ in range(int(timeout / interval)):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.fixture
def allow_the_internet():
    """The HA test harness pins every socket to 127.0.0.1; these tests are
    the one place that has to leave the box."""
    if not USERNAME or not PASSWORD:
        pytest.skip("set WXALERTS_HA_USERNAME and WXALERTS_HA_PASSWORD")
    pytest_socket._remove_restrictions()
    yield


@pytest.fixture
async def live_integration(hass, enable_custom_integrations, allow_the_internet):
    entry = make_entry(places=[], username=USERNAME, password=PASSWORD)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    assert await wait_for(lambda: coordinator.connected, CONNECT_TIMEOUT), (
        f"never connected to {DEFAULT_HOST}"
    )
    await asyncio.sleep(RETAINED_SETTLE)
    await hass.async_block_till_done()
    yield coordinator
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_places_status_and_alerts_arrive(hass, live_integration):
    coordinator = live_integration
    print(
        f"\nlive: {len(coordinator.places)} place(s), "
        f"feed age {coordinator.feed_age}, "
        f"counties {coordinator.same_codes}"
    )
    if not coordinator.places_received:
        pytest.skip("no retained places list on the account yet")
    for place in coordinator.places.values():
        alerts = coordinator.alerts_for(place.same)
        storms = coordinator.storms_for(place.id)
        print(f"  {place.name}: {len(alerts)} alert(s), {len(storms)} storm cell(s)")
        for alert in alerts:
            assert isinstance(alert.get("event"), str), alert
