# Changelog

## 0.3.0

Lightning becomes a proximity feed rather than a box counter.

**Breaking.** The single `Lightning flashes` sensor per zone is replaced by a
proximity set per target, on its own `Lightning — <name>` device: nearest
strike, ring counts at 5/10/20/30 miles, strike rate, minutes since the last
strike, a trend label, and a `Lightning within <n>mi` binary sensor with
`device_class: safety`. The old entity has no successor and will show as
unavailable; remove it. Dashboard cards and automations pointing at it need
repointing.

The `Lightning box size` option is gone, replaced by a **radius** and a
**seek-shelter distance**. The subscription is now derived from the radius
instead of being chosen by hand.

- **The box was never centred on you.** Geohash cells are aligned to the globe,
  so the cell containing a location sits wherever the grid puts it. A station
  4 miles from its cell's southern edge was blind to strikes past that edge —
  well inside the ten-mile ring, and typically the direction weather arrives
  from. Coverage is now the set of cells the radius actually reaches, and each
  flash is filtered by true great-circle distance rather than by cell
  membership.
- **The window is measured on each flash's own timestamp**, not on when it
  arrived. A delayed or replayed granule now lands in the window it belongs to
  instead of looking like it just happened.
- **A stale feed reports `unavailable`, not zero.** The integration subscribes
  to the ingest's retained heartbeat and goes unavailable when the feed ages
  out. A confident zero during an outage is indistinguishable from calm
  weather, and an outage is most likely exactly when it matters.
- **People can be targets.** Enable *Also track people* and every `person` with
  a GPS fix gets their own lightning device, re-measured as they move. Coverage
  follows them without dropping the broker session; someone with no fix is
  skipped rather than reported at a stale position.
- **The window starts full, not empty.** Nothing on the flash tree is retained,
  so starting during a storm meant reporting calm and ramping up over the whole
  window. The integration now asks the feed's history endpoint once on connect.
  Where that endpoint is not routed it logs one line and fills from live
  flashes — a slower ramp, never a setup failure.
- Trimming coverage to fit the broker's subscription cap is logged with what
  was dropped, because a dropped cell is a direction the sensor cannot see.

## 0.2.1

Fixes entity identity, found while testing the 0.2.0 install end to end.

- The lightning sensor was identified by its geohash prefix, which is
  derived from the box-size **option**. Changing the box size therefore
  orphaned the sensor and created a `_2` duplicate alongside it, silently
  breaking any dashboard card or automation pointing at the original. It is
  now identified by the zone, which does not move.
- Every configured zone now gets a lightning sensor. Zones were previously
  deduplicated by geohash box, so a second zone sharing a box with the first
  got no lightning sensor at all. Two zones in one box report the same count.
- A zone with no county no longer keys its **device** on the geohash prefix
  either, for the same reason.

Anyone who installed 0.2.0 should remove and re-add the integration; the
lightning entity's identity changed, so the old one lingers as unavailable.

## 0.2.0

A rewrite. The integration is now configured from Home Assistant **zones**
rather than hand-entered NWS zone codes, and it consumes the county
(`same/`) and lightning (`glm/`) topic trees instead of the older
alert-per-zone scheme. Entities, configuration and event payloads from
0.1.0 do not carry over; remove the old integration before installing.

New:

- Zones are resolved once at setup — county SAME code via `api.weather.gov`,
  and a geohash box computed locally. Never polled afterwards.
- GOES-19 GLM lightning: a flash counter per zone over a rolling window.
- `geo_location` map markers per live hazard, carrying the alert polygon.
- Per-phenomenon binary sensors (tornado, severe thunderstorm, flash flood
  enabled by default; flood, winter storm and heat available).
- A `wxalerts_alert` event per new hazard, with the full payload.
- Options flow: change zones, toggle either feed, tune the lightning box
  size and window.

Correctness:

- Hazard endings are handled as MQTT tombstones, so an alert clears when
  the NWS cancels it instead of lingering forever.
- `status` and tombstones are trusted over local clock arithmetic;
  `expires` is CAP's "expect an update by" deadline, not the hazard's end.
- Hazards already in effect at connect populate entities but fire no
  event, so a restart no longer re-announces every live warning.
- Alert geometry is excluded from the recorder: a county-union polygon
  exceeds the 16 KB attribute cap, which otherwise drops every attribute
  on the entity.
- Entity state updates run on the event loop. Previously the dispatcher
  ran them in an executor thread, where `async_write_ha_state` raises and
  no entity ever updated.
- The TLS context is built off the event loop, and reused across
  reconnects rather than rebuilt per attempt.
- Exponential backoff on reconnect, because every resubscribe replays the
  full retained set.

Requires Home Assistant 2024.12.0 or newer.

## 0.1.0

Initial release: one binary sensor per NWS UGC zone, fed by a direct
paho-mqtt connection.
