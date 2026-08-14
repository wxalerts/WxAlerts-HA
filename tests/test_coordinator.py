"""Coordinator: topic routing, tombstones, events, severity, lightning.

This is where the feed's two documented traps live — retained messages and
zero-length tombstones — so they get explicit tests rather than being implied
by an entity assertion.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.wxalerts.const import (
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_GLM_PRECISION,
    CONF_GLM_WINDOW,
    CONF_LOCATIONS,
    DOMAIN,
    EVENT_ALERT,
    MAX_SUBSCRIPTIONS,
    SEVERITY_NONE,
)
from custom_components.wxalerts.coordinator import WxAlertsCoordinator

from .const import (
    GLM_BURST,
    HEAT_ALERT,
    LOCATION_AUTAUGA,
    LOCATION_OFFSHORE,
    LOCATION_SANTA_ROSA,
    SAME_AUTAUGA,
    SAME_SANTA_ROSA,
    SEVERE_ALERT,
    TOMBSTONE,
    TOPIC_GLM_LEAF,
    TOPIC_HEAT,
    TOPIC_SEVERE,
    TOPIC_TORNADO,
    TORNADO_ALERT,
    encode,
)


def make_coordinator(hass, locations=None, **options) -> WxAlertsCoordinator:
    """A coordinator with no MQTT client — messages are fed in by hand."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCATIONS: locations
            if locations is not None
            else [LOCATION_SANTA_ROSA],
            CONF_ENABLE_ALERTS: options.pop(CONF_ENABLE_ALERTS, True),
            CONF_ENABLE_GLM: options.pop(CONF_ENABLE_GLM, True),
            **options,
        },
        entry_id="testentry",
    )
    entry.add_to_hass(hass)
    return WxAlertsCoordinator(hass, entry)


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


async def test_subscribes_to_county_and_lightning_topics(hass):
    coordinator = make_coordinator(
        hass, [LOCATION_SANTA_ROSA, LOCATION_AUTAUGA], **{CONF_GLM_PRECISION: 3}
    )
    subs = coordinator._build_subscriptions()

    assert subs == [
        (f"wxalerts/nws/v1/same/{SAME_SANTA_ROSA}/#", 1),
        (f"wxalerts/nws/v1/same/{SAME_AUTAUGA}/#", 1),
        ("wxalerts/glm/v1/d/j/6/#", 0),
        ("wxalerts/glm/v1/d/j/f/#", 0),
    ]


async def test_alert_subscriptions_use_qos1_and_lightning_qos0(hass):
    """Alerts are retained state and must not be dropped; lightning is QoS 0
    by design — a redelivered flash batch is worthless."""
    coordinator = make_coordinator(hass)
    for topic, qos in coordinator._build_subscriptions():
        assert qos == (1 if "/nws/" in topic else 0)


async def test_never_subscribes_to_a_whole_root(hass):
    """``same/#`` or ``glm/v1/#`` would pull thousands of retained messages."""
    coordinator = make_coordinator(hass, [LOCATION_SANTA_ROSA, LOCATION_AUTAUGA])
    for topic, _qos in coordinator._build_subscriptions():
        assert topic not in ("wxalerts/nws/v1/same/#", "wxalerts/glm/v1/#")
        assert not topic.endswith("v1/#")


async def test_disabling_alerts_drops_county_subscriptions(hass):
    coordinator = make_coordinator(hass, **{CONF_ENABLE_ALERTS: False})
    topics = [topic for topic, _ in coordinator._build_subscriptions()]
    assert all("/nws/" not in topic for topic in topics)
    assert topics


async def test_disabling_lightning_drops_glm_subscriptions(hass):
    coordinator = make_coordinator(hass, **{CONF_ENABLE_GLM: False})
    topics = [topic for topic, _ in coordinator._build_subscriptions()]
    assert all("/glm/" not in topic for topic in topics)
    assert topics


async def test_zone_without_a_county_still_gets_lightning(hass):
    """Offshore and non-US zones have no SAME code but do have a geohash."""
    coordinator = make_coordinator(hass, [LOCATION_OFFSHORE], **{CONF_GLM_PRECISION: 3})
    topics = [topic for topic, _ in coordinator._build_subscriptions()]

    assert coordinator.same_codes == []
    assert topics == ["wxalerts/glm/v1/d/j/1/#"]


async def test_duplicate_counties_subscribe_once(hass):
    """Two zones in one county must not cost two subscriptions."""
    second_zone_same_county = {**LOCATION_SANTA_ROSA, "zone_entity_id": "zone.work"}
    coordinator = make_coordinator(
        hass, [LOCATION_SANTA_ROSA, second_zone_same_county]
    )

    assert coordinator.same_codes == [SAME_SANTA_ROSA]
    assert len(coordinator._build_subscriptions()) == 2  # one county, one box


async def test_subscription_cap_trims_lightning_and_keeps_alerts(hass):
    """The broker allows 20 per client. Alerts are the life-safety feed, so
    lightning boxes are what gets dropped."""
    locations = [
        {
            **LOCATION_SANTA_ROSA,
            "zone_entity_id": f"zone.z{index}",
            "same": f"01{index:04d}",
            # Distinct geohash boxes so none of them dedup away.
            "geohash": f"d{'bcdefghjkmnpqrstuvwx'[index]}6n7",
        }
        for index in range(14)
    ]
    coordinator = make_coordinator(hass, locations, **{CONF_GLM_PRECISION: 3})

    with patch.object(
        coordinator, "_build_subscriptions", wraps=coordinator._build_subscriptions
    ):
        subs = coordinator._build_subscriptions()

    assert len(subs) == MAX_SUBSCRIPTIONS
    alert_subs = [topic for topic, _ in subs if "/nws/" in topic]
    assert len(alert_subs) == 14, "every county must survive the trim"


# ---------------------------------------------------------------------------
# Topic routing
# ---------------------------------------------------------------------------


async def test_alert_message_is_stored_under_its_county(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))

    alerts = coordinator.alerts_for(SAME_SANTA_ROSA)
    assert [alert["event"] for alert in alerts] == ["Tornado Warning"]


async def test_lightning_message_is_stored_under_its_box(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))

    assert coordinator.flash_count("dj6") == 2


@pytest.mark.parametrize(
    "topic",
    [
        # Text products are filed under the office, not a county.
        "wxalerts/nws/v1/alert/KMOB/Messages/abc123",
        # The office alert tree carries the same hazard; consuming it too
        # would double-count every warning.
        "wxalerts/nws/v1/alert/KMOB/TO/W/0012",
        "wxalerts/nws/v1/same/012113",  # truncated, no ETN
        "wxalerts/glm/v1/d/j/6",  # not a leaf cell
        "something/else/entirely",
        "",
    ],
)
async def test_unrelated_topics_are_ignored(hass, topic):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(topic, encode(TORNADO_ALERT))

    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []
    assert coordinator.flash_count("dj6") == 0


async def test_undecodable_payload_does_not_raise_or_store(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, b"{not json")
    await coordinator._handle_message(TOPIC_GLM_LEAF, b"<html>502</html>")

    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []
    assert coordinator.flash_count("dj6") == 0


# ---------------------------------------------------------------------------
# Tombstones — "the single most likely bug in a first implementation"
# ---------------------------------------------------------------------------


async def test_tombstone_removes_the_alert(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    assert coordinator.alerts_for(SAME_SANTA_ROSA)

    await coordinator._handle_message(TOPIC_TORNADO, TOMBSTONE)

    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []
    assert coordinator.highest_severity(SAME_SANTA_ROSA) == SEVERITY_NONE


async def test_tombstone_removes_only_the_hazard_it_names(hass):
    """Two live hazards in one county: ending one must not end the other."""
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await coordinator._handle_message(TOPIC_SEVERE, encode(SEVERE_ALERT))
    assert len(coordinator.alerts_for(SAME_SANTA_ROSA)) == 2

    await coordinator._handle_message(TOPIC_TORNADO, TOMBSTONE)

    remaining = coordinator.alerts_for(SAME_SANTA_ROSA)
    assert [alert["event"] for alert in remaining] == ["Severe Thunderstorm Warning"]


async def test_tombstone_for_an_unknown_topic_is_harmless(hass):
    """Retained tombstones can arrive for hazards this client never saw."""
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, TOMBSTONE)

    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []


async def test_expired_status_is_not_reported_as_active(hass):
    """``status`` is trusted over local clock arithmetic."""
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(
        TOPIC_TORNADO, encode({**TORNADO_ALERT, "status": "expired"})
    )

    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []
    assert coordinator.highest_severity(SAME_SANTA_ROSA) == SEVERITY_NONE


async def test_cancelled_status_is_not_reported_as_active(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(
        TOPIC_TORNADO, encode({**TORNADO_ALERT, "status": "cancelled"})
    )

    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []


async def test_alert_updated_to_cancelled_clears_without_a_tombstone(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await coordinator._handle_message(
        TOPIC_TORNADO, encode({**TORNADO_ALERT, "action": "CAN", "status": "cancelled"})
    )

    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def test_new_hazard_fires_one_event(hass):
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_ALERT)

    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["event"] == "Tornado Warning"
    assert data["severity"] == "Extreme"
    assert data["same"] == SAME_SANTA_ROSA
    assert data["vtec"] == "KMOB.TO.W.0012.2026"
    assert data["headline"] == TORNADO_ALERT["headline"]
    assert data["instruction"] == TORNADO_ALERT["instruction"]
    assert data["action"] == "NEW"


async def test_updating_the_same_hazard_does_not_re_fire(hass):
    """A CON/EXT on the same topic is the same tornado, not a second one."""
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_ALERT)

    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await coordinator._handle_message(
        TOPIC_TORNADO, encode({**TORNADO_ALERT, "action": "CON"})
    )
    await coordinator._handle_message(
        TOPIC_TORNADO, encode({**TORNADO_ALERT, "action": "EXT"})
    )
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_a_second_hazard_fires_its_own_event(hass):
    """Distinct ETNs are distinct hazards even in the same county."""
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_ALERT)

    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await coordinator._handle_message(TOPIC_SEVERE, encode(SEVERE_ALERT))
    await hass.async_block_till_done()

    assert [event.data["event"] for event in events] == [
        "Tornado Warning",
        "Severe Thunderstorm Warning",
    ]


async def test_a_hazard_reissued_after_ending_fires_again(hass):
    """The tombstone clears the fired-once bookkeeping, so an ETN that comes
    back around is announced rather than silently swallowed."""
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_ALERT)

    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await coordinator._handle_message(TOPIC_TORNADO, TOMBSTONE)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await hass.async_block_till_done()

    assert len(events) == 2


# ---------------------------------------------------------------------------
# Severity and phenomena
# ---------------------------------------------------------------------------


async def test_alerts_are_ordered_most_severe_first(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_SEVERE, encode(SEVERE_ALERT))
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))

    assert [alert["severity"] for alert in coordinator.alerts_for(SAME_SANTA_ROSA)] == [
        "Extreme",
        "Severe",
    ]
    assert coordinator.highest_severity(SAME_SANTA_ROSA) == "Extreme"


async def test_highest_severity_with_nothing_live(hass):
    coordinator = make_coordinator(hass)
    assert coordinator.highest_severity(SAME_SANTA_ROSA) == SEVERITY_NONE


async def test_unknown_severity_sorts_last_and_reports_none(hass):
    """An alert with a severity outside the CAP set must not be presented as
    the county's headline severity."""
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(
        TOPIC_TORNADO, encode({**TORNADO_ALERT, "severity": "Unknown"})
    )

    assert coordinator.alerts_for(SAME_SANTA_ROSA)  # still a live alert
    assert coordinator.highest_severity(SAME_SANTA_ROSA) == SEVERITY_NONE


async def test_has_phenomenon(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))

    assert coordinator.has_phenomenon(SAME_SANTA_ROSA, "TO") is True
    assert coordinator.has_phenomenon(SAME_SANTA_ROSA, "SV") is False


async def test_phenomenon_clears_with_the_tombstone(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await coordinator._handle_message(TOPIC_TORNADO, TOMBSTONE)

    assert coordinator.has_phenomenon(SAME_SANTA_ROSA, "TO") is False


async def test_counties_are_independent(hass):
    coordinator = make_coordinator(hass, [LOCATION_SANTA_ROSA, LOCATION_AUTAUGA])
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await coordinator._handle_message(TOPIC_HEAT, encode(HEAT_ALERT))

    assert coordinator.highest_severity(SAME_SANTA_ROSA) == "Extreme"
    assert coordinator.highest_severity(SAME_AUTAUGA) == "Moderate"
    assert coordinator.has_phenomenon(SAME_AUTAUGA, "TO") is False


# ---------------------------------------------------------------------------
# Lightning
# ---------------------------------------------------------------------------


async def test_flashes_accumulate_across_bursts(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))

    assert coordinator.flash_count("dj6") == 4


async def test_flashes_outside_the_configured_box_are_ignored(hass):
    """Subscriptions are per-box, but a wildcard reconfigure or a shared
    connection could deliver a neighbouring cell."""
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3})
    await coordinator._handle_message("wxalerts/glm/v1/9/x/j/5/e", encode(GLM_BURST))

    assert coordinator.flash_count("dj6") == 0


async def test_flash_details_are_carried_through(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))

    flash = coordinator.recent_flashes("dj6")[0]
    assert flash["latitude"] == 30.6103
    assert flash["longitude"] == -87.0547
    assert flash["energy_j"] == 1.21e-13
    assert flash["geohash"] == "dj6n7"
    assert flash["time"] == "2026-08-13T23:57:39.195468+00:00"


async def test_flashes_age_out_of_the_window(hass):
    coordinator = make_coordinator(
        hass, **{CONF_GLM_PRECISION: 3, CONF_GLM_WINDOW: 15}
    )
    with patch("custom_components.wxalerts.coordinator.time.time", return_value=1000.0):
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))
        assert coordinator.flash_count("dj6") == 2

    # 14 minutes later: still inside the window.
    with patch("custom_components.wxalerts.coordinator.time.time", return_value=1840.0):
        assert coordinator.flash_count("dj6") == 2

    # 16 minutes later: aged out.
    with patch("custom_components.wxalerts.coordinator.time.time", return_value=1961.0):
        assert coordinator.flash_count("dj6") == 0


async def test_window_length_is_configurable(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3, CONF_GLM_WINDOW: 60})
    with patch("custom_components.wxalerts.coordinator.time.time", return_value=1000.0):
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))

    # 30 minutes on — outside a 15-minute window, inside a 60-minute one.
    with patch("custom_components.wxalerts.coordinator.time.time", return_value=2800.0):
        assert coordinator.flash_count("dj6") == 2


async def test_recent_flashes_is_capped_and_newest_last(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3})
    for index in range(20):
        burst = {
            **GLM_BURST,
            "flashes": [{**GLM_BURST["flashes"][0], "lat": float(index)}],
        }
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(burst))

    recent = coordinator.recent_flashes("dj6", limit=5)
    assert len(recent) == 5
    assert [flash["latitude"] for flash in recent] == [15.0, 16.0, 17.0, 18.0, 19.0]


async def test_empty_lightning_payload_is_not_a_tombstone(hass):
    """Nothing on the GLM tree is retained, so an empty payload is noise —
    it must not clear the rolling count."""
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))
    await coordinator._handle_message(TOPIC_GLM_LEAF, TOMBSTONE)

    assert coordinator.flash_count("dj6") == 2


async def test_burst_with_no_flashes_is_harmless(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3})
    await coordinator._handle_message(
        TOPIC_GLM_LEAF, encode({**GLM_BURST, "count": 0, "flashes": []})
    )

    assert coordinator.flash_count("dj6") == 0
    assert coordinator.last_flash_time("dj6") is None


async def test_last_flash_time_uses_the_flash_timestamp(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))

    last = coordinator.last_flash_time("dj6")
    assert last is not None
    assert last.isoformat() == "2026-08-13T23:57:44.695584+00:00"


async def test_last_flash_time_falls_back_to_receipt_time(hass):
    """A malformed ``t`` must not blow up the sensor's attributes."""
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 3})
    burst = {**GLM_BURST, "flashes": [{**GLM_BURST["flashes"][0], "t": "not a time"}]}
    with patch("custom_components.wxalerts.coordinator.time.time", return_value=1000.0):
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(burst))
        last = coordinator.last_flash_time("dj6")

    assert last is not None
    assert last.timestamp() == 1000.0


async def test_lightning_precision_selects_the_box(hass):
    """Precision 5 is a single leaf cell; a neighbouring leaf in the same
    county box must not count towards it."""
    coordinator = make_coordinator(hass, **{CONF_GLM_PRECISION: 5})
    assert coordinator.glm_prefixes == ["dj6n7"]

    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))
    await coordinator._handle_message("wxalerts/glm/v1/d/j/6/n/8", encode(GLM_BURST))

    assert coordinator.flash_count("dj6n7") == 2


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_start_derives_a_unique_client_id(hass, mock_feed_client):
    """Two clients sharing an ID kick each other off in a loop."""
    coordinator = make_coordinator(hass)
    await coordinator.async_start()

    config = mock_feed_client.call_args[0][0]
    assert config.client_id
    assert config.client_id.startswith("wxha-")
    assert coordinator.entry.entry_id[:8] in config.client_id
    mock_feed_client.return_value.start.assert_called_once()


async def test_start_is_skipped_when_there_is_nothing_to_subscribe_to(
    hass, mock_feed_client
):
    coordinator = make_coordinator(
        hass, [], **{CONF_ENABLE_ALERTS: True, CONF_ENABLE_GLM: True}
    )
    await coordinator.async_start()

    mock_feed_client.assert_not_called()


async def test_stop_closes_the_client(hass, mock_feed_client):
    coordinator = make_coordinator(hass)
    await coordinator.async_start()
    await coordinator.async_stop()

    mock_feed_client.return_value.stop.assert_awaited_once()


async def test_stop_without_a_client_does_not_raise(hass):
    coordinator = make_coordinator(hass)
    await coordinator.async_stop()


async def test_connection_state_is_published(hass, mock_feed_client):
    coordinator = make_coordinator(hass)
    assert coordinator.connected is False

    coordinator._handle_connection(True)
    assert coordinator.connected is True

    coordinator._handle_connection(False)
    assert coordinator.connected is False
