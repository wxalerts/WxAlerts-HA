# Changelog

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
