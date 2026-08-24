"""Proximity arithmetic for the GLM flash stream.

The feed delivers flashes binned into geohash cells, which is the right shape
for a topic tree and the wrong shape for the question anyone actually asks —
"how close is the lightning to *me*". A cell is aligned to the globe, not to a
location, so cell membership answers a question about the grid rather than about
distance. Everything here converts the one into the other.

Kept separate from the coordinator because it is pure: flashes in, numbers out,
no Home Assistant and no MQTT. That is also what makes the trend logic testable
without a broker.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .const import (
    TREND_APPROACHING,
    TREND_DELTA_MI,
    TREND_MIN_INTERVAL,
    TREND_RECEDING,
    TREND_SAMPLES,
    TREND_STEADY,
    TREND_UNKNOWN,
)
from .geo import haversine_miles


@dataclass(frozen=True)
class Target:
    """Somewhere proximity is measured from: a configured zone or a person."""

    key: str  # stable identity — the zone or person entity id
    name: str
    latitude: float
    longitude: float
    kind: str = "zone"  # "zone" | "person"
    location: dict[str, Any] = field(default_factory=dict)


@dataclass
class Flash:
    """One GLM flash, reduced to what proximity needs."""

    when: float  # epoch seconds, from the flash's own timestamp
    latitude: float
    longitude: float
    energy_j: float | None = None


@dataclass
class Metrics:
    """Everything the lightning entities read, for one target."""

    nearest_mi: float | None = None
    rings: dict[int, int] = field(default_factory=dict)
    total: int = 0
    rate_per_min: float = 0.0
    last_strike_min: float | None = None
    trend: str = TREND_UNKNOWN
    nearby: bool = False
    last_flash_at: datetime | None = None


def parse_flash_time(raw: Any, fallback: float) -> float:
    """Epoch seconds for a flash's own timestamp.

    Windowing on the flash time rather than on arrival time is what makes a
    replayed or delayed batch land in the right window instead of pretending a
    ten-minute-old burst just happened.
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw:
        text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return fallback
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return fallback


class FlashStore:
    """A rolling window of flashes, shared by every target.

    One store rather than one per target: the same flash is usually inside
    several targets' rings, and copying it per target would multiply memory by
    the number of people being tracked for no gain.
    """

    def __init__(self, window_minutes: int) -> None:
        self.window_minutes = window_minutes
        self._flashes: deque[Flash] = deque()
        # The oldest flash time held, so the common case (nothing to drop) does
        # not have to walk the whole window on every burst.
        self._oldest: float | None = None

    def __len__(self) -> int:
        return len(self._flashes)

    def add(self, flashes: Iterable[Flash]) -> int:
        """Append a burst. Returns how many were kept."""
        kept = 0
        for flash in flashes:
            self._flashes.append(flash)
            if self._oldest is None or flash.when < self._oldest:
                self._oldest = flash.when
            kept += 1
        return kept

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - self.window_minutes * 60
        if not self._flashes or (self._oldest is not None and self._oldest >= cutoff):
            return
        # Bursts are sorted within a cell but not across cells, and a delayed
        # granule can arrive after a newer one, so the oldest flash is NOT
        # reliably at the left of the deque. Filter rather than popleft until
        # fresh — that shortcut silently keeps stale flashes alive behind a
        # newer one.
        self._flashes = deque(f for f in self._flashes if f.when >= cutoff)
        self._oldest = min((f.when for f in self._flashes), default=None)

    def flashes(self) -> tuple[Flash, ...]:
        return tuple(self._flashes)

    def latest(self) -> float | None:
        return max((f.when for f in self._flashes), default=None)


class TrendTracker:
    """Approaching / Receding / Steady, per target.

    Compares the first and last of several samples rather than consecutive
    ones, so a single flash on the far edge of a storm cannot flip the label.
    Samples are spaced in time as well as in count: the feed can deliver a dozen
    bursts in as many seconds, and five samples taken inside one granule would
    describe nothing.
    """

    def __init__(self) -> None:
        self._history: dict[str, deque[tuple[float, float]]] = {}

    def sample(self, key: str, nearest_mi: float | None, now: float) -> None:
        if nearest_mi is None:
            return
        history = self._history.setdefault(key, deque(maxlen=TREND_SAMPLES))
        if history and now - history[-1][0] < TREND_MIN_INTERVAL:
            return
        history.append((now, nearest_mi))

    def label(self, key: str) -> str:
        history = self._history.get(key)
        if not history or len(history) < 3:
            return TREND_UNKNOWN
        delta = history[-1][1] - history[0][1]
        if delta <= -TREND_DELTA_MI:
            return TREND_APPROACHING
        if delta >= TREND_DELTA_MI:
            return TREND_RECEDING
        return TREND_STEADY

    def forget(self, keys: set[str]) -> None:
        """Drop history for targets that no longer exist."""
        for key in list(self._history):
            if key not in keys:
                del self._history[key]


def compute(
    store: FlashStore,
    target: Target,
    *,
    radius_mi: float,
    alert_mi: float,
    rings: Iterable[int],
    now: float | None = None,
) -> Metrics:
    """Ring counts, nearest distance and rate for one target."""
    now = time.time() if now is None else now
    ring_list = sorted(r for r in rings if 0 < r <= radius_mi)
    metrics = Metrics(rings={r: 0 for r in ring_list})

    nearest: float | None = None
    newest: float | None = None
    total = 0

    for flash in store.flashes():
        distance = haversine_miles(
            target.latitude, target.longitude, flash.latitude, flash.longitude
        )
        if distance > radius_mi:
            continue
        total += 1
        if nearest is None or distance < nearest:
            nearest = distance
        if newest is None or flash.when > newest:
            newest = flash.when
        for ring in ring_list:
            if distance <= ring:
                metrics.rings[ring] += 1

    metrics.total = total
    metrics.nearest_mi = None if nearest is None else round(nearest, 1)
    metrics.nearby = nearest is not None and nearest <= alert_mi

    window_minutes = store.window_minutes or 1
    metrics.rate_per_min = round(total / window_minutes, 2)

    if newest is not None:
        metrics.last_strike_min = round(max(0.0, now - newest) / 60.0, 1)
        metrics.last_flash_at = datetime.fromtimestamp(newest, tz=timezone.utc)

    return metrics
