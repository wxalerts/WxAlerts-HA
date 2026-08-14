"""End-to-end against the production feed. Deselected by default.

    pytest -m live

Everything else in this suite fakes the MQTT layer, which cannot tell you
that TLS, websockets, MQTT v5 or the credential still work — only that the
code around them does. These tests dial ``wss://mqtt.wxalerts.org/mqtt`` for
real and drive the whole integration from it, so they are subject to the
weather: a county with nothing live is a valid result and is reported rather
than failed.

Read-only throughout. The broker denies publish on every topic.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_socket
from homeassistant.const import STATE_UNAVAILABLE
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wxalerts.const import (
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_GLM_PRECISION,
    CONF_GLM_WINDOW,
    CONF_LOCATIONS,
    CONF_ZONES,
    DEFAULT_HOST,
    DOMAIN,
)

from .const import LOCATION_AUTAUGA, LOCATION_SANTA_ROSA

pytestmark = pytest.mark.live

CONNECT_TIMEOUT = 30
RETAINED_SETTLE = 8


async def wait_for(predicate, timeout: float, interval: float = 0.25) -> bool:
    """Poll until true or out of time; live feeds have no deterministic tick."""
    for _ in range(int(timeout / interval)):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.fixture
def live_entry() -> MockConfigEntry:
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
        # Precision 3 is a wide box, so lightning somewhere in the Gulf is
        # likely enough to be worth reporting on.
        options={CONF_GLM_PRECISION: 3, CONF_GLM_WINDOW: 15},
    )


@pytest.fixture
def allow_the_internet():
    """The HA test harness pins every socket to 127.0.0.1; these tests are
    the one place that has to leave the box."""
    pytest_socket._remove_restrictions()
    yield


@pytest.fixture
async def live_integration(hass, live_entry, enable_custom_integrations, allow_the_internet):
    live_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(live_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = live_entry.runtime_data
    connected = await wait_for(lambda: coordinator.connected, CONNECT_TIMEOUT)
    assert connected, f"never connected to {DEFAULT_HOST}"

    # Let the retained set land before anything is asserted on.
    await asyncio.sleep(RETAINED_SETTLE)
    await hass.async_block_till_done()

    yield coordinator

    await hass.config_entries.async_unload(live_entry.entry_id)
    await hass.async_block_till_done()


async def test_connects_to_the_production_broker(live_integration):
    """TLS, websockets, MQTT v5 and the published read-only credential."""
    assert live_integration.connected is True


async def test_retained_alerts_repopulate_on_connect(hass, live_integration):
    """The whole no-polling promise: entities are correct immediately after
    startup because the county topics are retained."""
    alerts = live_integration.alerts_for("012113") + live_integration.alerts_for(
        "001001"
    )
    print(f"\nlive: {len(alerts)} retained alert(s) across the two test counties")

    for alert in alerts:
        # Whatever arrived must be shaped the way the entities assume.
        assert isinstance(alert.get("event"), str), alert
        assert alert.get("status") == "active"
        assert "same" in alert
        print(
            f"  {alert.get('event')!r} "
            f"severity={alert.get('severity')} "
            f"phen={alert.get('phenomena')} "
            f"geom={alert.get('geometry_source')} "
            f"ends={alert.get('ends')}"
        )

    if not alerts:
        pytest.skip("no live hazards in either test county right now")


async def test_entities_reflect_the_live_feed(hass, live_integration):
    """The user-visible end of the same data."""
    for entity_id in (
        "binary_sensor.wxalerts_home_active_alert",
        "sensor.wxalerts_home_highest_severity",
        "sensor.wxalerts_home_active_alerts",
        "sensor.wxalerts_home_lightning_flashes",
    ):
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.state != STATE_UNAVAILABLE, entity_id
        print(f"\n{entity_id} = {state.state}")

    count = hass.states.get("sensor.wxalerts_home_active_alerts")
    severity = hass.states.get("sensor.wxalerts_home_highest_severity")
    active = hass.states.get("binary_sensor.wxalerts_home_active_alert")

    # The three must agree with each other whatever the weather is doing.
    assert int(count.state) == len(live_integration.alerts_for("012113"))
    assert (active.state == "on") == (int(count.state) > 0)
    if int(count.state) == 0:
        assert severity.state == "None"
    else:
        assert severity.state in ("Extreme", "Severe", "Moderate", "Minor", "None")


async def test_severity_sensor_never_reports_an_unlisted_option(hass, live_integration):
    """Live severities must stay inside the enum the sensor declares."""
    state = hass.states.get("sensor.wxalerts_home_highest_severity")
    assert state.state in state.attributes["options"]


async def test_live_alert_geometry_is_usable(hass, live_integration):
    """Whatever geometry the feed sends must yield a marker or be honestly
    absent — a centroid that comes back None is why the marker is skipped."""
    from custom_components.wxalerts.geo import geometry_centroid

    checked = 0
    for same in ("012113", "001001"):
        for alert in live_integration.alerts_for(same):
            geometry = alert.get("geometry")
            centroid = geometry_centroid(geometry)
            if geometry:
                assert centroid is not None, f"undrawable geometry: {alert.get('event')}"
                lat, lon = centroid
                assert -90 <= lat <= 90 and -180 <= lon <= 180
                checked += 1

    print(f"\nlive: {checked} alert geometr(ies) produced a usable centroid")
    if checked == 0:
        pytest.skip("no live alert carried geometry")


async def test_live_lightning(hass, live_integration):
    """GLM is weather-dependent, so this reports rather than demands."""
    await asyncio.sleep(20)  # a granule is 20 s
    await hass.async_block_till_done()

    state = hass.states.get("sensor.wxalerts_home_lightning_flashes")
    flashes = int(state.state)
    print(f"\nlive: {flashes} flash(es) in box {state.attributes['geohash']}")

    assert flashes >= 0
    for flash in live_integration.recent_flashes(state.attributes["geohash"]):
        assert -90 <= flash["latitude"] <= 90
        assert -180 <= flash["longitude"] <= 180

    if flashes == 0:
        pytest.skip("no lightning in the configured box right now")
