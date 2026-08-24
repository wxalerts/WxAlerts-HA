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
    CONF_GLM_ALERT,
    CONF_GLM_PERSONS,
    CONF_GLM_RADIUS,
    CONF_GLM_WINDOW,
    CONF_LOCATIONS,
    DOMAIN,
    EVENT_ALERT,
    MAX_SUBSCRIPTIONS,
    SEVERITY_NONE,
    TOPIC_GLM_STATUS,
)
from custom_components.wxalerts.coordinator import WxAlertsCoordinator

from .const import (
    GLM_BURST,
    GLM_BURST_EPOCH,
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
    fresh_burst,
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
    coordinator = WxAlertsCoordinator(hass, entry)
    # Setup resolves targets before the platforms are forwarded; a coordinator
    # that never did would plan no coverage and subscribe to nothing.
    coordinator.prepare()
    return coordinator


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


async def test_subscribes_to_county_and_lightning_topics(hass):
    coordinator = make_coordinator(
        hass, [LOCATION_SANTA_ROSA, LOCATION_AUTAUGA], **{CONF_GLM_RADIUS: 30}
    )
    topics = [topic for topic, _ in coordinator._build_subscriptions()]

    assert topics[:3] == [
        f"wxalerts/nws/v1/same/{SAME_SANTA_ROSA}/#",
        f"wxalerts/nws/v1/same/{SAME_AUTAUGA}/#",
        TOPIC_GLM_STATUS,
    ]
    # Both zones are covered, and each takes more than the one cell it sits in.
    assert "wxalerts/glm/v1/d/j/6/#" in topics
    assert "wxalerts/glm/v1/d/j/f/#" in topics
    assert len(topics) > 5


async def test_alert_subscriptions_use_qos1_and_lightning_qos0(hass):
    """Alerts are retained state and must not be dropped; flash bursts are
    QoS 0 by design — a redelivered batch is worthless.

    The heartbeat is the exception: it is retained state about whether the feed
    is alive, and losing it silently is the failure it exists to prevent.
    """
    coordinator = make_coordinator(hass)
    for topic, qos in coordinator._build_subscriptions():
        if "/nws/" in topic or topic == TOPIC_GLM_STATUS:
            assert qos == 1
        else:
            assert qos == 0


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
    """Offshore and non-US zones have no SAME code but still have coordinates."""
    coordinator = make_coordinator(hass, [LOCATION_OFFSHORE], **{CONF_GLM_RADIUS: 30})
    topics = [topic for topic, _ in coordinator._build_subscriptions()]

    assert coordinator.same_codes == []
    assert TOPIC_GLM_STATUS in topics
    assert any(topic.startswith("wxalerts/glm/v1/d/j/") for topic in topics)


async def test_duplicate_counties_subscribe_once(hass):
    """Two zones in one county must not cost two subscriptions."""
    second_zone_same_county = {**LOCATION_SANTA_ROSA, "zone_entity_id": "zone.work"}
    coordinator = make_coordinator(
        hass, [LOCATION_SANTA_ROSA, second_zone_same_county]
    )

    assert coordinator.same_codes == [SAME_SANTA_ROSA]
    topics = [topic for topic, _ in coordinator._build_subscriptions()]
    assert len([t for t in topics if "/nws/" in t]) == 1
    # Both zones share a position, so they share coverage rather than doubling it.
    assert len(topics) == len(set(topics))


async def test_subscription_cap_trims_lightning_and_keeps_alerts(hass):
    """The broker allows 20 per client. Alerts are the life-safety feed, so
    lightning boxes are what gets dropped."""
    locations = [
        {
            **LOCATION_SANTA_ROSA,
            "zone_entity_id": f"zone.z{index}",
            "same": f"01{index:04d}",
            # Spread far apart so none of the coverage overlaps.
            "latitude": 25.0 + index * 2.0,
            "longitude": -100.0 + index * 2.0,
        }
        for index in range(14)
    ]
    coordinator = make_coordinator(hass, locations, **{CONF_GLM_RADIUS: 30})
    subs = coordinator._build_subscriptions()

    assert len(subs) == MAX_SUBSCRIPTIONS
    alert_subs = [topic for topic, _ in subs if "/nws/" in topic]
    assert len(alert_subs) == 14, "every county must survive the trim"
    assert coordinator.glm_dropped_cells > 0, "the trim must be reported, not silent"


# ---------------------------------------------------------------------------
# Topic routing
# ---------------------------------------------------------------------------


async def test_alert_message_is_stored_under_its_county(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))

    alerts = coordinator.alerts_for(SAME_SANTA_ROSA)
    assert [alert["event"] for alert in alerts] == ["Tornado Warning"]


async def test_lightning_message_lands_in_the_window(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(fresh_burst()))

    assert coordinator.glm_window_flashes == 2
    assert coordinator.metrics_for("zone.home").total == 2


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
    assert coordinator.glm_window_flashes == 0


async def test_undecodable_payload_does_not_raise_or_store(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(TOPIC_TORNADO, b"{not json")
    await coordinator._handle_message(TOPIC_GLM_LEAF, b"<html>502</html>")

    assert coordinator.alerts_for(SAME_SANTA_ROSA) == []
    assert coordinator.glm_window_flashes == 0


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


async def test_the_retained_burst_fires_no_events(hass):
    """Subscribing replays every live hazard. Announcing those would mean a
    tornado warning re-notifying on every Home Assistant restart."""
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_ALERT)

    coordinator._handle_connection(True)  # opens the priming window
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await coordinator._handle_message(TOPIC_SEVERE, encode(SEVERE_ALERT))
    await hass.async_block_till_done()

    assert events == []
    # ...but the state is fully populated, which is the point of retention.
    assert len(coordinator.alerts_for(SAME_SANTA_ROSA)) == 2
    assert coordinator.highest_severity(SAME_SANTA_ROSA) == "Extreme"


async def test_a_hazard_after_the_burst_fires_normally(hass):
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_ALERT)

    coordinator._handle_connection(True)
    await coordinator._handle_message(TOPIC_SEVERE, encode(SEVERE_ALERT))  # retained

    # The burst has landed; anything after this is genuinely new.
    coordinator._priming_until = 0.0
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await hass.async_block_till_done()

    assert [event.data["event"] for event in events] == ["Tornado Warning"]


async def test_a_retained_hazard_is_not_announced_later_as_new(hass):
    """Suppressed-at-connect must still count as seen, or the next CON update
    would announce it after the window closes."""
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_ALERT)

    coordinator._handle_connection(True)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    coordinator._priming_until = 0.0
    await coordinator._handle_message(
        TOPIC_TORNADO, encode({**TORNADO_ALERT, "action": "CON"})
    )
    await hass.async_block_till_done()

    assert events == []


async def test_reconnecting_does_not_re_announce_live_hazards(hass):
    """A dropped connection replays the whole retained set on resubscribe."""
    coordinator = make_coordinator(hass)
    events = async_capture_events(hass, EVENT_ALERT)

    coordinator._handle_connection(True)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    coordinator._priming_until = 0.0

    coordinator._handle_connection(False)
    coordinator._handle_connection(True)
    await coordinator._handle_message(TOPIC_TORNADO, encode(TORNADO_ALERT))
    await hass.async_block_till_done()

    assert events == []


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
# Lightning proximity
# ---------------------------------------------------------------------------

# The fixture burst sits ~2.3 miles south of zone.home, which is what makes it
# useful: it is inside every ring, so a ring that reports zero is broken rather
# than merely quiet.


async def test_flashes_accumulate_across_bursts(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(fresh_burst()))
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(fresh_burst()))

    assert coordinator.metrics_for("zone.home").total == 4


async def test_distance_is_measured_not_inferred_from_the_cell(hass):
    """The whole point of the rework: rings are radial, not box membership."""
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(fresh_burst()))

    metrics = coordinator.metrics_for("zone.home")
    assert metrics.nearest_mi == pytest.approx(2.3, abs=0.2)
    assert metrics.rings[5] == 2
    assert metrics.rings[10] == 2
    assert metrics.rings[30] == 2


async def test_flashes_beyond_the_radius_do_not_count(hass):
    """A cell can be much larger than the ring, so cell membership alone would
    report a strike 60 miles away as if it were overhead."""
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 10})
    far = fresh_burst(lat=31.5, lon=-87.05)
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(far))

    metrics = coordinator.metrics_for("zone.home")
    assert metrics.total == 0
    assert metrics.nearest_mi is None, "no lightning is not the same as lightning at the edge"


async def test_the_ring_reaches_outside_the_containing_cell(hass):
    """A location near its cell's edge must still be covered on that side.

    zone.home sits about 4 miles from the southern edge of its precision-4
    cell, so a single-cell subscription is blind to a strike 8 miles south —
    which is well inside the seek-shelter ring.
    """
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    topics = [topic for topic, _ in coordinator._build_subscriptions()]
    glm_topics = [topic for topic in topics if "/glm/v1/" in topic and topic != TOPIC_GLM_STATUS]

    assert len(glm_topics) > 1, "one cell can never cover a radius it is smaller than"


async def test_nearby_flag_follows_the_alert_distance(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_ALERT: 1})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(fresh_burst()))
    assert coordinator.metrics_for("zone.home").nearby is False

    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_ALERT: 10})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(fresh_burst()))
    assert coordinator.metrics_for("zone.home").nearby is True


async def test_rings_beyond_the_radius_are_dropped(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 10})
    assert coordinator.glm_rings == (5, 10)


async def test_flash_details_are_carried_through(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    # Stand in the capture's own moment so the verbatim payload is inside the
    # window; the point of this test is that its fields survive the round trip.
    with patch(
        "custom_components.wxalerts.coordinator.time.time",
        return_value=GLM_BURST_EPOCH + 5,
    ):
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))

        # Nearest first, so this is the second flash in the capture — the closer
    # of the two to zone.home.
    flash = coordinator.recent_flashes("zone.home")[0]

    assert flash["latitude"] == 30.6110
    assert flash["longitude"] == -87.0530
    assert flash["energy_j"] == 3.28e-13
    assert flash["time"] == "2026-08-13T23:57:44.695584+00:00"
    assert flash["distance_mi"] == pytest.approx(2.2, abs=0.2)


async def test_flashes_age_out_on_their_own_timestamp(hass):
    """Windowing on arrival time would put a replayed burst in the wrong window
    and, worse, keep a ten-minute-old flash alive for a full fresh window."""
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_WINDOW: 15})
    burst_epoch = 1786000000.0
    burst = {
        **GLM_BURST,
        "flashes": [
            {**GLM_BURST["flashes"][0], "t": burst_epoch},
            {**GLM_BURST["flashes"][1], "t": burst_epoch},
        ],
    }

    with patch(
        "custom_components.wxalerts.coordinator.time.time", return_value=burst_epoch
    ):
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(burst))
        assert coordinator.metrics_for("zone.home").total == 2

    # 14 minutes after the flash: still inside the window.
    with patch("custom_components.wxalerts.lightning.time.time", return_value=burst_epoch + 840):
        coordinator._recompute_all(burst_epoch + 840)
    assert coordinator.metrics_for("zone.home").total == 2

    # 16 minutes after: aged out.
    coordinator._recompute_all(burst_epoch + 961)
    assert coordinator.metrics_for("zone.home").total == 0


async def test_window_length_is_configurable(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_WINDOW: 60})
    burst_epoch = 1786000000.0
    burst = {
        **GLM_BURST,
        "flashes": [{**GLM_BURST["flashes"][0], "t": burst_epoch}],
    }
    with patch(
        "custom_components.wxalerts.coordinator.time.time", return_value=burst_epoch
    ):
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(burst))

    # 30 minutes on — outside a 15-minute window, inside a 60-minute one.
    coordinator._recompute_all(burst_epoch + 1800)
    assert coordinator.metrics_for("zone.home").total == 1


async def test_rate_is_flashes_per_minute_over_the_window(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_WINDOW: 10})
    for _ in range(10):
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(fresh_burst()))

    # 20 flashes over a 10-minute window.
    assert coordinator.metrics_for("zone.home").rate_per_min == pytest.approx(2.0)


async def test_closest_flashes_are_nearest_first(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    for offset in (0.30, 0.02, 0.15):
        burst = fresh_burst(lat=30.6435 - offset, lon=-87.0545)
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(burst))

    distances = [f["distance_mi"] for f in coordinator.recent_flashes("zone.home")]
    assert distances == sorted(distances)


# ---------------------------------------------------------------------------
# Staleness — a confident zero during an outage is the dangerous failure
# ---------------------------------------------------------------------------


async def test_lightning_is_available_before_anything_arrives(hass):
    """A genuinely quiet cell produces no messages, so silence at startup
    cannot mean the feed is dead."""
    coordinator = make_coordinator(hass)
    coordinator._handle_connection(True)
    assert coordinator.glm_available is True


async def test_a_stale_heartbeat_makes_lightning_unavailable(hass):
    coordinator = make_coordinator(hass)
    coordinator._handle_connection(True)

    status = {"healthy": True, "last_granule_age_seconds": 20}
    coordinator._handle_glm_status(encode(status))
    assert coordinator.glm_available is True

    stale = {"healthy": True, "last_granule_age_seconds": 3600}
    coordinator._handle_glm_status(encode(stale))
    assert coordinator.glm_available is False


async def test_an_unhealthy_feed_makes_lightning_unavailable(hass):
    """The ingest knows whether NOAA is still delivering; we do not."""
    coordinator = make_coordinator(hass)
    coordinator._handle_connection(True)
    coordinator._handle_glm_status(encode({"healthy": False}))

    assert coordinator.glm_available is False


async def test_a_dropped_connection_makes_lightning_unavailable(hass):
    coordinator = make_coordinator(hass)
    coordinator._handle_connection(True)
    coordinator._handle_glm_status(encode({"healthy": True, "last_granule_age_seconds": 5}))
    coordinator._handle_connection(False)

    assert coordinator.glm_available is False


async def test_heartbeat_topic_is_not_parsed_as_a_flash_burst(hass):
    coordinator = make_coordinator(hass)
    await coordinator._handle_message(
        TOPIC_GLM_STATUS, encode({"healthy": True, "last_granule_age_seconds": 1})
    )
    assert coordinator.glm_window_flashes == 0



async def test_empty_lightning_payload_is_not_a_tombstone(hass):
    """Nothing on the GLM tree is retained, so an empty payload is noise —
    it must not clear the rolling window."""
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(fresh_burst()))
    await coordinator._handle_message(TOPIC_GLM_LEAF, TOMBSTONE)

    assert coordinator.metrics_for("zone.home").total == 2


async def test_burst_with_no_flashes_is_harmless(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    await coordinator._handle_message(
        TOPIC_GLM_LEAF, encode({**GLM_BURST, "count": 0, "flashes": []})
    )

    metrics = coordinator.metrics_for("zone.home")
    assert metrics.total == 0
    assert metrics.last_flash_at is None


async def test_last_flash_uses_the_flash_timestamp(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    with patch(
        "custom_components.wxalerts.coordinator.time.time",
        return_value=GLM_BURST_EPOCH + 5,
    ):
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(GLM_BURST))

    last = coordinator.metrics_for("zone.home").last_flash_at
    assert last is not None
    assert last.isoformat() == "2026-08-13T23:57:44.695584+00:00"


async def test_an_unparseable_timestamp_falls_back_to_receipt_time(hass):
    """A malformed ``t`` must not blow up the sensor or silently window the
    flash into 1970."""
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    burst = {**GLM_BURST, "flashes": [{**GLM_BURST["flashes"][0], "t": "not a time"}]}
    with patch(
        "custom_components.wxalerts.coordinator.time.time", return_value=1786000000.0
    ):
        await coordinator._handle_message(TOPIC_GLM_LEAF, encode(burst))

    last = coordinator.metrics_for("zone.home").last_flash_at
    assert last is not None
    assert last.timestamp() == 1786000000.0


async def test_a_flash_missing_coordinates_is_skipped_not_fatal(hass):
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})
    burst = {
        **GLM_BURST,
        "flashes": [{"t": GLM_BURST["flashes"][0]["t"], "energy_j": 1e-13}],
    }
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(burst))

    assert coordinator.glm_window_flashes == 0


async def test_coverage_precision_is_derived_from_the_radius(hass):
    """A small radius can be covered by finer cells, which means less traffic;
    a large one has to fall back to coarser ones to fit the subscription cap."""
    tight = make_coordinator(hass, **{CONF_GLM_RADIUS: 5})
    wide = make_coordinator(hass, **{CONF_GLM_RADIUS: 50})

    assert tight.glm_precision > wide.glm_precision


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


# ---------------------------------------------------------------------------
# Person targets
# ---------------------------------------------------------------------------


def set_person(hass, entity_id, lat, lon, name="Somebody"):
    hass.states.async_set(
        entity_id,
        "home",
        {"latitude": lat, "longitude": lon, "friendly_name": name},
    )


async def test_persons_are_not_tracked_unless_asked(hass):
    set_person(hass, "person.clint", 30.65, -87.05, "Clint")
    coordinator = make_coordinator(hass, **{CONF_GLM_RADIUS: 30})

    assert [t.key for t in coordinator.targets] == ["zone.home"]


async def test_a_person_with_a_fix_becomes_a_target(hass):
    set_person(hass, "person.clint", 30.65, -87.05, "Clint")
    coordinator = make_coordinator(
        hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_PERSONS: True}
    )

    keys = [t.key for t in coordinator.targets]
    assert "person.clint" in keys
    person = coordinator.target("person.clint")
    assert person.kind == "person"
    assert person.name == "Clint"


async def test_a_person_without_a_fix_is_skipped(hass):
    """Reporting them at a stale or absent position is worse than not
    reporting them at all."""
    hass.states.async_set("person.nofix", "home", {"friendly_name": "No Fix"})
    coordinator = make_coordinator(
        hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_PERSONS: True}
    )

    assert [t.key for t in coordinator.targets] == ["zone.home"]


async def test_a_person_far_away_extends_the_coverage(hass):
    """Someone two states away needs their own cells, or their sensor would
    read a confident zero using the house's coverage."""
    set_person(hass, "person.away", 40.0, -105.0, "Away")
    coordinator = make_coordinator(
        hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_PERSONS: True}
    )

    topics = [topic for topic, _ in coordinator._build_subscriptions()]
    assert any(topic.startswith("wxalerts/glm/v1/9") for topic in topics)


async def test_moving_within_coverage_does_not_resubscribe(hass, mock_feed_client):
    set_person(hass, "person.clint", 30.65, -87.05, "Clint")
    coordinator = make_coordinator(
        hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_PERSONS: True}
    )
    await coordinator.async_start()
    before = list(coordinator._cells)

    set_person(hass, "person.clint", 30.66, -87.06, "Clint")
    await hass.async_block_till_done()

    assert coordinator._cells == before
    mock_feed_client.return_value.set_subscriptions.assert_not_called()


async def test_moving_out_of_coverage_resubscribes(hass, mock_feed_client):
    set_person(hass, "person.clint", 30.65, -87.05, "Clint")
    coordinator = make_coordinator(
        hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_PERSONS: True}
    )
    await coordinator.async_start()

    set_person(hass, "person.clint", 40.0, -105.0, "Clint")
    await hass.async_block_till_done()

    mock_feed_client.return_value.set_subscriptions.assert_called()


async def test_a_person_who_moves_is_measured_from_their_new_position(hass):
    set_person(hass, "person.clint", 30.6435, -87.0545, "Clint")
    coordinator = make_coordinator(
        hass, **{CONF_GLM_RADIUS: 30, CONF_GLM_PERSONS: True}
    )
    await coordinator.async_start()
    await coordinator._handle_message(TOPIC_GLM_LEAF, encode(fresh_burst()))
    assert coordinator.metrics_for("person.clint").nearby is True

    # 40 miles north: the same flashes, a different answer.
    set_person(hass, "person.clint", 31.22, -87.0545, "Clint")
    await hass.async_block_till_done()

    assert coordinator.metrics_for("person.clint").nearby is False
