"""Proximity arithmetic, with no Home Assistant and no broker in the way.

The trend label is the piece most worth testing here: it is the only part of
the lightning set that depends on history rather than on the current window, so
it is the only part that can be wrong in a way a single snapshot cannot show.
"""

from __future__ import annotations

import pytest

from custom_components.wxalerts.const import (
    TREND_APPROACHING,
    TREND_RECEDING,
    TREND_STEADY,
    TREND_UNKNOWN,
)
from custom_components.wxalerts.lightning import (
    Flash,
    FlashStore,
    Target,
    TrendTracker,
    compute,
    parse_flash_time,
)

HOME = Target(key="zone.home", name="Home", latitude=30.6435, longitude=-87.0545)
NOW = 1786000000.0


def store_with(*flashes: Flash, window: int = 30) -> FlashStore:
    store = FlashStore(window)
    store.add(flashes)
    return store


def flash_at(distance_north_deg: float, when: float = NOW) -> Flash:
    return Flash(
        when=when, latitude=30.6435 + distance_north_deg, longitude=-87.0545
    )


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def test_iso_timestamps_are_parsed():
    assert parse_flash_time("2026-08-13T23:57:44.695584+00:00", 0.0) == pytest.approx(
        1786665464.695584
    )


def test_a_trailing_z_is_accepted():
    """The feed emits offsets, but a Z suffix is the other legal spelling and
    costs nothing to accept."""
    with_z = parse_flash_time("2026-08-13T23:57:44.695584Z", 0.0)
    with_offset = parse_flash_time("2026-08-13T23:57:44.695584+00:00", 0.0)
    assert with_z == with_offset


def test_a_naive_timestamp_is_read_as_utc():
    """Reading it as local time would move a flash by hours, which at these
    window lengths means dropping it entirely."""
    naive = parse_flash_time("2026-08-13T23:57:44.695584", 0.0)
    aware = parse_flash_time("2026-08-13T23:57:44.695584+00:00", 0.0)
    assert naive == aware


def test_an_epoch_number_is_taken_as_is():
    assert parse_flash_time(1786000000.0, 0.0) == 1786000000.0


@pytest.mark.parametrize("raw", ["", "not a time", None, {}, []])
def test_an_unusable_timestamp_falls_back(raw):
    assert parse_flash_time(raw, 1234.0) == 1234.0


# ---------------------------------------------------------------------------
# The rolling window
# ---------------------------------------------------------------------------


def test_flashes_age_out_on_their_own_time():
    store = store_with(flash_at(0.0, NOW - 60), flash_at(0.0, NOW - 3000), window=30)
    store.prune(NOW)

    assert len(store) == 1


def test_a_late_arriving_old_flash_does_not_get_a_fresh_window():
    """Windowing on arrival would keep a 40-minute-old flash alive for another
    30 minutes, reporting a storm that has already passed."""
    store = FlashStore(30)
    store.add([flash_at(0.0, NOW - 2400)])
    store.prune(NOW)

    assert len(store) == 0


def test_out_of_order_arrivals_are_pruned_correctly():
    """Bursts are sorted within a cell but not across them, so the oldest entry
    is not reliably at the front of the queue."""
    store = FlashStore(30)
    store.add([flash_at(0.0, NOW - 3000), flash_at(0.0, NOW - 10)])
    store.prune(NOW)

    assert len(store) == 1


# ---------------------------------------------------------------------------
# Rings
# ---------------------------------------------------------------------------


def test_rings_are_cumulative_not_exclusive():
    """A strike two miles away is inside the five-mile ring and the ten, and a
    dashboard that subtracted them would be double-counting."""
    store = store_with(flash_at(0.03))  # ~2 miles north
    metrics = compute(store, HOME, radius_mi=30, alert_mi=10, rings=(5, 10, 30), now=NOW)

    assert metrics.rings == {5: 1, 10: 1, 30: 1}


def test_nothing_seen_is_unknown_not_the_radius():
    metrics = compute(FlashStore(30), HOME, radius_mi=30, alert_mi=10, rings=(10,), now=NOW)

    assert metrics.nearest_mi is None
    assert metrics.nearby is False
    assert metrics.total == 0


def test_a_flash_beyond_the_radius_is_not_counted_at_all():
    store = store_with(flash_at(1.0))  # ~69 miles north
    metrics = compute(store, HOME, radius_mi=30, alert_mi=10, rings=(30,), now=NOW)

    assert metrics.total == 0
    assert metrics.nearest_mi is None


def test_rate_is_over_the_window_not_since_the_first_flash():
    store = store_with(*[flash_at(0.01, NOW - 10) for _ in range(30)], window=10)
    metrics = compute(store, HOME, radius_mi=30, alert_mi=10, rings=(10,), now=NOW)

    assert metrics.rate_per_min == pytest.approx(3.0)


def test_minutes_since_the_last_strike_uses_the_newest_flash():
    store = store_with(flash_at(0.01, NOW - 600), flash_at(0.02, NOW - 120))
    metrics = compute(store, HOME, radius_mi=30, alert_mi=10, rings=(10,), now=NOW)

    assert metrics.last_strike_min == pytest.approx(2.0, abs=0.1)


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def test_trend_is_unknown_until_there_is_history():
    trend = TrendTracker()
    trend.sample("zone.home", 20.0, NOW)
    trend.sample("zone.home", 18.0, NOW + 60)

    assert trend.label("zone.home") == TREND_UNKNOWN


def test_a_storm_closing_in_reads_as_approaching():
    trend = TrendTracker()
    for index, distance in enumerate((25.0, 20.0, 14.0, 9.0)):
        trend.sample("zone.home", distance, NOW + index * 60)

    assert trend.label("zone.home") == TREND_APPROACHING


def test_a_storm_moving_off_reads_as_receding():
    trend = TrendTracker()
    for index, distance in enumerate((4.0, 9.0, 15.0, 22.0)):
        trend.sample("zone.home", distance, NOW + index * 60)

    assert trend.label("zone.home") == TREND_RECEDING


def test_one_far_edge_flash_does_not_flip_the_label():
    """Comparing consecutive samples would call this receding. Comparing the
    first against the last does not, which is the whole reason for the depth."""
    trend = TrendTracker()
    for index, distance in enumerate((12.0, 11.5, 29.0, 11.0, 11.2)):
        trend.sample("zone.home", distance, NOW + index * 60)

    assert trend.label("zone.home") == TREND_STEADY


def test_samples_taken_too_close_together_are_dropped():
    """The feed can deliver a dozen bursts in as many seconds; five samples
    inside one granule would describe nothing at all."""
    trend = TrendTracker()
    for index in range(10):
        trend.sample("zone.home", 20.0 - index, NOW + index)

    assert trend.label("zone.home") == TREND_UNKNOWN


def test_a_target_with_no_reading_does_not_pollute_its_history():
    trend = TrendTracker()
    trend.sample("zone.home", 10.0, NOW)
    trend.sample("zone.home", None, NOW + 60)
    trend.sample("zone.home", 9.5, NOW + 120)

    assert trend.label("zone.home") == TREND_UNKNOWN  # only two real samples


def test_forgetting_a_target_drops_its_history():
    trend = TrendTracker()
    for index, distance in enumerate((25.0, 20.0, 14.0)):
        trend.sample("person.gone", distance, NOW + index * 60)
    assert trend.label("person.gone") == TREND_APPROACHING

    trend.forget({"zone.home"})

    assert trend.label("person.gone") == TREND_UNKNOWN


def test_targets_keep_separate_histories():
    trend = TrendTracker()
    for index, (near, far) in enumerate([(25.0, 4.0), (20.0, 9.0), (14.0, 15.0)]):
        trend.sample("zone.home", near, NOW + index * 60)
        trend.sample("person.clint", far, NOW + index * 60)

    assert trend.label("zone.home") == TREND_APPROACHING
    assert trend.label("person.clint") == TREND_RECEDING
