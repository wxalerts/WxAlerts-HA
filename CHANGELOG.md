# Changelog

## 1.0.0

WxAlerts moves from Home Assistant zones to the places on your WxAlerts
account, and from lightning to storm cell alerts. Home Assistant is now a
Supporter feature: sign in from Home Assistant and each instance gets its own
broker login.

**Breaking.** Upgrading removes every 0.x entity and device and asks you to
sign in to wxalerts.org. See *Upgrading from 0.x* in the README.

- **Sign in from Home Assistant.** Adding the integration opens a wxalerts.org
  sign-in in the browser (OAuth2 with PKCE, through My Home Assistant). The
  integration then registers this instance with the API and stores its own
  broker login; nothing is copied or pasted. Free accounts get a short note
  that Home Assistant needs the Supporter plan, and a full instance quota a
  note to revoke one or add a Top-up, both with a link to pricing.
- **Self-healing login.** If the broker rejects the login, the integration
  refreshes the sign-in and registers again for a fresh one, and only asks
  you to sign in when that fails.
- **Places from the account.** One device per place, read from the retained
  `wxalerts/u/<username>/places` topic. Places added, renamed, moved or
  deleted on wxalerts.org follow live, with no restart; a deleted place takes
  its device with it. The last list is cached so entities exist at start-up.
  A place without a county code gets one looked up from `api.weather.gov`.
- **Alerts per place.** Active alert, highest severity, alert count, top alert,
  per-type binary sensors, map markers, the `wxalerts_alert` and
  `wxalerts_hazard` events and the device triggers all work per place. Two
  places in one county are two devices. Events now carry `place_id` and
  `place_name`.
- **Storm cell alerts.** Per place: *Storm cell approaching* (safety), *Storm
  cell ETA* (counts down between updates) and *Storm cell threat* (enum by
  rung), the `wxalerts_storm_cell` event on open and escalate, and matching
  device triggers. A storm that was never cleared expires after 30 minutes.
- **Feed age.** An account-wide *Feed age* sensor from the server's status
  heartbeat, and a *Connected* sensor. Storm cell entities go unavailable when
  the heartbeat is 15 minutes old or reports offline. A 30 second timer keeps
  time-based state moving with no messages arriving.
- **Diagnostics** download, with the sign-in token, broker login, instance id
  and place coordinates redacted.
- **Removed:** GOES GLM lightning (every sensor, device, option, the history
  backfill and person tracking), zone selection, and the alerts and lightning
  toggles.
- **Fixes:** the config entry is versioned and migrated (`async_migrate_entry`)
  instead of claiming a migration that did not exist; platforms use
  `AddConfigEntryEntitiesCallback`; the manifest declares
  `single_config_entry`.
- Requires Home Assistant 2025.2.0 or newer.

## 0.4.0

Alerts become selectable by NWS event type instead of by phenomenon bucket.

**Breaking.** The six per-phenomenon binary sensors (Tornado, Severe
Thunderstorm, Flash Flood, Flood, Winter Storm, Heat) are gone, along with the
`phenomenon_sensors` toggle that switched them on. They matched on the VTEC
phenomenon alone, so a Tornado Watch and a Tornado Warning both turned on one
`Tornado alert` sensor, a take-cover automation could not tell the two apart.
Their registry entries are removed automatically on upgrade rather than left
sitting as unavailable, so an automation still pointed at one breaks visibly
now instead of silently never firing again. Repoint them at the new sensors.

- **All 122 NWS event types are selectable**, in options, as binary sensors:
  `binary_sensor.wxalerts_home_tornado_warning`,
  `binary_sensor.wxalerts_home_blizzard_warning`, and so on. An upgraded entry
  and a fresh install both start with the hazards that can kill someone asleep:
  Tornado, Severe Thunderstorm, Flash Flood, Extreme Wind, Hurricane,
  Blizzard, Ice Storm, Snow Squall and Tsunami warnings.
- **Device triggers for the whole catalogue, with no configuration.** Each
  county device offers every event type as a *begins* and an *ends* trigger in
  the automation editor. Nothing is created in the entity registry, so the
  types you have not selected are still automatable.
- **New `wxalerts_hazard` event**, fired when an event type starts or stops
  being in effect for a county. Distinct from `wxalerts_alert`, which fires per
  product: two simultaneous tornado warnings are two alerts but one hazard, and
  a take-cover automation should run once. Like `wxalerts_alert`, a *begin* is
  suppressed during the retained-message replay so a restart re-announces
  nothing; an *end* is never suppressed, because the set starts empty on a
  restart and cannot produce a spurious one.
- **New `Top alert` sensor** per county: the most urgent hazard in effect, by
  name. Ranking now breaks a severity tie on significance, NWS gives a Tornado
  Watch and a Severe Thunderstorm Warning the same severity, so severity alone
  ordered them by arrival.
- **Names are canonical everywhere.** The two feed paths word the same hazard
  differently, the VTEC path builds "Fire Weather Warning" where NWS says "Red
  Flag Warning", "Marine Warning" where NWS says "Special Marine Warning", so
  matching resolves the VTEC pair first and falls back to the name only for
  CAP-only products, which have no VTEC and carry the canonical spelling
  already. Map markers and the alert list report the canonical name.
- Retired VTEC codes fold into their replacements: `WC.Y` reads as Cold Weather
  Advisory, `WC.W`/`EC.W` as Extreme Cold Warning, `EH.W` as Extreme Heat
  Warning. An office that has not adopted the rename is not invisible.

## 0.3.0

Lightning becomes a proximity feed rather than a box counter.

**Breaking.** The single `Lightning flashes` sensor per zone is replaced by a
proximity set per target, on its own "Lightning <name>" device: nearest
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
  4 miles from its cell's southern edge was blind to strikes past that edge -
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
  flashes, a slower ramp, never a setup failure.
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

- Zones are resolved once at setup, county SAME code via `api.weather.gov`,
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
