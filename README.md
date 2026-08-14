# WxAlerts for Home Assistant

[![HACS Custom Repository](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/release/wxalerts/WxAlerts-HA.svg)](https://github.com/wxalerts/WxAlerts-HA/releases)
[![Validate](https://github.com/wxalerts/WxAlerts-HA/actions/workflows/validate.yml/badge.svg)](https://github.com/wxalerts/WxAlerts-HA/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Live **NWS weather alerts** and **GOES GLM lightning** from the
[wxalerts.org](https://wxalerts.org) MQTT feed, mapped onto your Home
Assistant zones. Push, not polling: alerts arrive the moment they are
published and disappear the moment they end.

> Requires Home Assistant **2024.12.0** or newer.

## What you get

Pick one or more Home Assistant zones (Home, the cabin, grandma's house).
Each zone is resolved once, during setup, to:

- its **county SAME code** (the same six-digit code a weather radio uses),
  via a single call to `api.weather.gov` — never polled again afterward
- a **geohash box** for GLM lightning, computed locally from the zone's
  coordinates

Both feeds are optional toggles — run alerts only, lightning only, or both.

### Entities per county

| Entity | Description |
|---|---|
| `binary_sensor` — Active alert | On when any hazard is live. `device_class: safety`. |
| `sensor` — Highest severity | `Extreme` / `Severe` / `Moderate` / `Minor` / `None`. |
| `sensor` — Active alerts | Count, with the full alert list (headline, instruction, times, geometry source) in attributes for Markdown cards. |
| `binary_sensor` — per phenomenon | Tornado, Severe Thunderstorm, Flash Flood on by default; Flood, Winter Storm, Heat available but disabled. Toggle the whole set in options. |
| `geo_location` — map markers | One marker per live hazard at its polygon centroid; the full GeoJSON polygon is an attribute. Markers vanish when the hazard ends. |

### Entities per lightning box

| Entity | Description |
|---|---|
| `sensor` — Lightning flashes | GLM flashes in the zone's geohash box over a rolling window (default 15 min). Flash *rate* is the meaningful severe-weather signal — GLM cannot distinguish cloud-to-ground from intra-cloud, so a big energy number does not mean a dangerous strike. |

### Events

Every **new** hazard fires a `wxalerts_alert` event on the Home Assistant
bus with the county, event type, severity, headline, and instruction.
Trigger automations on the event, not on state changes — the event
preserves the difference between "a second tornado warning" and "the same
one updated", which a state machine cannot.

Hazards already in effect when the integration connects arrive as retained
messages and fire **no** event: they populate every entity, but they are
current state rather than news, and announcing them would re-notify you of
every live warning each time Home Assistant restarts. The flip side is
that a hazard issued during the first few seconds of a connection updates
the sensors without firing an event.

```yaml
automation:
  - alias: Tornado warning announcement
    trigger:
      - platform: event
        event_type: wxalerts_alert
        event_data:
          event: Tornado Warning
    action:
      - service: notify.mobile_app_phone
        data:
          title: "{{ trigger.event.data.headline }}"
          message: "{{ trigger.event.data.instruction }}"
          data:
            push:
              interruption-level: critical
```

A simpler automation keyed on the binary sensor:

```yaml
automation:
  - alias: Any alert light
    trigger:
      - platform: state
        entity_id: binary_sensor.wxalerts_home_active_alert
        to: "on"
    action:
      - service: light.turn_on
        target: { entity_id: light.alert_beacon }
        data: { color_name: red }
```

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**
2. Add `https://github.com/wxalerts/WxAlerts-HA`, type **Integration**
3. Install **WxAlerts**, restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → WxAlerts
5. Pick your zones, toggle alerts/lightning, done

### Manual

Copy `custom_components/wxalerts/` into your config's
`custom_components/` and restart.

## Options

Settings → Devices & Services → WxAlerts → **Configure**:

- change the watched zones (they are re-resolved on save)
- enable/disable NWS alerts and GLM lightning independently
- lightning box size: geohash characters, 3 ≈ regional (~150 km),
  4 ≈ county (~39 km), 5 ≈ neighborhood (~4.9 km)
- lightning rolling window (minutes)
- per-phenomenon binary sensors on/off

## How it connects

The integration holds **one** MQTT v5 connection over websockets to
`wss://mqtt.wxalerts.org/mqtt` (port 443, public and read-only — the
credential `wxalerts`/`wxalerts` is deliberately published, and the broker
denies publish on every topic). It subscribes only to the counties and
geohash boxes you configured — the retained alert set repopulates every
entity immediately on startup or reconnect, with no polling and no
"unknown" gap.

Hazard endings arrive as MQTT tombstones (empty retained payloads), so an
alert clears the moment the NWS cancels it or it runs out of time —
`status` and tombstones are trusted over local clock arithmetic.

### Running against your own broker

If you already bridge the wxalerts topics into a local Mosquitto/EMQX
(one upstream connection serving your whole house), the MQTT client in
this integration is a single swappable object (`FeedClient` in
`coordinator.py`) — a host/port override option is the planned path for a
`local_push` deployment. Bridge stanza reference lives in the
[wxalerts docs](https://wxalerts.org).

## Notes and limits

- Marine and offshore zones have no SAME code, so they never produce
  county alerts. A coastal zone still gets lightning.
- Non-US zones get lightning only (GOES-East field of view permitting).
- The broker allows 20 subscriptions per client; roughly 9 counties plus
  9 lightning boxes. The integration trims lightning boxes first and logs
  a warning if you exceed it.
- Alert geometry is simplified for the wire (~550 m tolerance on county
  unions); `geometry_source` on each alert tells you whether the shape is
  a tight storm polygon or a coarse county union.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest            # the full suite, no network
.venv/bin/python -m pytest -m live    # against the production broker
```

The default run fakes the MQTT transport, so it is deterministic and
offline. The `live` tests are deselected by default: they dial the real
broker to check what a fake cannot — TLS, websockets, MQTT v5, and that
live payloads are still shaped the way the entities assume. They are
weather-dependent, so a county with nothing live skips rather than fails.

`FeedClient` in `coordinator.py` is the whole MQTT surface. Everything
above it is fed by `_handle_message(topic, payload)`, which is what the
tests drive.

## Data source

NOAA/NWS alerts and GOES-19 GLM lightning, redistributed by
[WxAlerts.org](https://wxalerts.org) — a nonprofit open-source weather
alerting platform. The feed is public NOAA data; do not rely on any single
delivery path for life-safety decisions — a weather radio has no
dependencies.

## Contributing

Issues and pull requests are welcome at
[github.com/wxalerts/WxAlerts-HA](https://github.com/wxalerts/WxAlerts-HA).
Please run the test suite before opening a PR; CI runs hassfest, HACS
validation and the same tests.

## License

MIT — see [LICENSE](LICENSE)
