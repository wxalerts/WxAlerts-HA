# WxAlerts for Home Assistant

[![HACS Custom Repository](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/release/wxalerts/WxAlerts-HA.svg)](https://github.com/wxalerts/WxAlerts-HA/releases)
[![Validate](https://github.com/wxalerts/WxAlerts-HA/actions/workflows/validate.yml/badge.svg)](https://github.com/wxalerts/WxAlerts-HA/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Live **NWS alerts** and **storm cell alerts** for the places on your
[wxalerts.org](https://wxalerts.org) account. Push, not polling: alerts
arrive the moment they are published and clear the moment they end.

Home Assistant is a **Supporter** feature. See
[wxalerts.org/pricing](https://wxalerts.org/pricing/).

> Requires Home Assistant **2025.2.0** or newer.

## What you get

Every place saved on your WxAlerts account becomes a device in Home
Assistant, named `WxAlerts <place>`. Add, rename or delete a place on
wxalerts.org and Home Assistant follows within seconds, with no restart.

Each place gets:

- **NWS alerts** for its county: an active alert sensor, the highest
  severity, a count with the full alert list, the top alert, and a binary
  sensor for each alert type you pick.
- **Storm cell alerts**: when a storm cell's forecast path threatens the
  place, a safety sensor turns on with the cell's arrival time, threat rung,
  probabilities, speed, bearing and distance.
- **Device triggers** for every NWS alert type and for storm cell episodes,
  ready in the automation editor with nothing to configure.

## Installation

### 1. Create the Home Assistant login on wxalerts.org

1. Sign in at [wxalerts.org](https://wxalerts.org) with a Supporter account.
2. Open **Account**, then **Home Assistant**, and create the login.
3. Copy the username (it starts with `ha-`) and the password. The password
   is shown once; you can rotate it there at any time.

### 2. Install with HACS

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/wxalerts/WxAlerts-HA` with category
   **Integration**.
3. Install **WxAlerts** and restart Home Assistant.

Manual install: copy `custom_components/wxalerts` into your
`config/custom_components` folder and restart.

### 3. Add the integration

**Settings**, **Devices & services**, **Add integration**, **WxAlerts**.
Enter the username and password from step 1. The integration connects to
`wss://mqtt.wxalerts.org/mqtt` to check them, then creates one device per
place.

A Free account sees a short note that Home Assistant needs the Supporter
plan, with a link to upgrade.

## Entities and events

Entity ids below are for a place named Home.

### Per place

- `binary_sensor.wxalerts_home_storm_cell_approaching`: on while a storm cell
  threatens the place. `device_class: safety`. Attributes: `storm_id`,
  `episode`, `eta_min`, `rung`, `rung_label`, `threat`, `prob_severe`,
  `prob_tor`, `prob_hail`, `prob_wind`, `speed_kmh`, `bearing_deg`,
  `distance_km`, `updated_at`, `storm_count`, `feed_age_seconds`.
- `sensor.wxalerts_home_storm_cell_eta`: minutes until the most threatening
  cell arrives. Counts down between updates. Unknown with no storm.
- `sensor.wxalerts_home_storm_cell_threat`: the threat rung as an enum:
  `none`, `thunderstorm_risk`, `thunderstorm_detected`, `tornado_risk`,
  `tornado_risk_high`, `tornado_detected`, `tornado_confirmed` (plus the
  outlook and wind rungs if the server sends them).
- `binary_sensor.wxalerts_home_active_alert`: on when any NWS alert is live
  for the place's county. `device_class: safety`.
- `sensor.wxalerts_home_highest_severity`: `Extreme`, `Severe`, `Moderate`,
  `Minor` or `None`.
- `sensor.wxalerts_home_active_alerts`: the count, with the full alert list
  (event type, headline, instruction, times) in attributes for Markdown
  cards.
- `sensor.wxalerts_home_top_alert`: the most urgent alert by name, ranked by
  severity and then by significance, so a warning outranks a watch.
- `binary_sensor.wxalerts_home_tornado_warning` and one more per alert type
  you select in the integration options. A fresh install selects the
  warnings that can harm someone asleep: Tornado, Severe Thunderstorm, Flash
  Flood, Extreme Wind, Hurricane, Blizzard, Ice Storm, Snow Squall and
  Tsunami.

When several storm cells threaten one place, the one with the highest rung
drives these entities, and the soonest arrival breaks a tie.

A place with no county (offshore, outside the US) gets storm cells; its
alert entities show as unavailable.

### Account wide (device `WxAlerts`)

- `sensor.wxalerts_feed_age`: seconds since the WxAlerts server last
  reported in. Keeps rising on its own if the server goes quiet.
- `binary_sensor.wxalerts_connected`: the broker connection.

If the feed stops reporting for 15 minutes, or reports itself offline, the
storm cell entities turn unavailable rather than claiming the sky is clear.

### Map markers

`geo_location` entities, one per live alert with a shape, at the polygon's
centre. The polygon itself is the `geometry` attribute. They vanish when the
alert ends.

### Events

- `wxalerts_storm_cell`: a storm cell episode opened or escalated for a
  place. Data: `episode` (`open` or `escalate`), `place_id`, `place_name`,
  `storm_id`, `eta_min`, `rung`, `rung_label`, `threat`, the four
  probabilities, `speed_kmh`, `bearing_deg`, `distance_km`, `issued_at`,
  `updated_at`.
- `wxalerts_alert`: a new NWS alert for a place. Data includes `place_id`,
  `place_name`, `event`, `event_type`, `severity`, `headline`,
  `instruction`, `onset`, `ends`.
- `wxalerts_hazard`: an alert type began or ended for a place. Data:
  `place_id`, `place_name`, `event_type`, `event`, `action` (`begin` or
  `end`), `severity`, `headline`, `ends`.

None of these fire for what is already in effect when Home Assistant starts
or reconnects, so a restart never re-announces a warning.

### Device triggers

Pick a place device in the automation editor. Every NWS alert type is listed
as *begins* and *ends*, plus **Storm cell approaching** and **Storm cell
threat escalates**. Nothing is added to the entity registry for the types you
did not select as sensors.

## Automation examples

### Storm cell approaching

```yaml
automation:
  - alias: "Storm cell heading for home"
    trigger:
      - platform: event
        event_type: wxalerts_storm_cell
        event_data:
          place_name: Home
    action:
      - service: notify.mobile_app_phone
        data:
          title: "{{ trigger.event.data.rung_label }}"
          message: >
            A storm cell is about {{ trigger.event.data.eta_min | round(0) }}
            minutes from {{ trigger.event.data.place_name }}, moving at
            {{ trigger.event.data.speed_kmh | round(0) }} km/h.
```

Or on the sensor, which also covers a storm that was already approaching
when Home Assistant started:

```yaml
automation:
  - alias: "Close the garage when a storm cell approaches"
    trigger:
      - platform: state
        entity_id: binary_sensor.wxalerts_home_storm_cell_approaching
        from: "off"
        to: "on"
    action:
      - service: cover.close_cover
        target:
          entity_id: cover.garage_door
```

Trigger `from: "off"`: a stale feed makes the sensor unavailable, and
`unavailable` to `on` means the feed recovered, not that a new storm arrived.

### Life-safety warnings

```yaml
automation:
  - alias: "Tornado warning at home"
    trigger:
      - platform: state
        entity_id: binary_sensor.wxalerts_home_tornado_warning
        from: "off"
        to: "on"
    action:
      - service: notify.mobile_app_phone
        data:
          title: "Tornado Warning"
          message: >
            {{ state_attr('binary_sensor.wxalerts_home_tornado_warning',
            'alerts')[0].instruction }}
          data:
            push:
              interruption-level: critical
      - service: light.turn_on
        target:
          entity_id: light.hallway
        data:
          color_name: red
```

Every warning for any place, in one automation:

```yaml
automation:
  - alias: "Any warning for any place"
    trigger:
      - platform: event
        event_type: wxalerts_hazard
        event_data:
          action: begin
    condition:
      - "{{ trigger.event.data.event_type.endswith('_warning') }}"
    action:
      - service: notify.mobile_app_phone
        data:
          title: "{{ trigger.event.data.event }}"
          message: "{{ trigger.event.data.place_name }}: {{ trigger.event.data.headline }}"
```

## Options

**Settings**, **Devices & services**, **WxAlerts**, **Configure**: pick the
alert types that get a binary sensor on each place. Everything else stays
available as a device trigger. Places are managed on wxalerts.org.

## Upgrading from 0.x

1.0 is a breaking release.

- **Places replace Home Assistant zones.** Save the places you care about on
  wxalerts.org; they arrive as devices on their own.
- **Lightning is removed** in favour of storm cell alerts. All lightning
  sensors, the lightning devices, person tracking and the lightning options
  are gone.
- **A Supporter login replaces the shared public login.**

What happens on upgrade:

1. The entry is migrated and every 0.x entity and device (county, zone and
   lightning) is removed from the registry, so nothing lingers as
   unavailable.
2. Your alert type selection is kept.
3. Home Assistant shows a **re-authenticate** prompt for WxAlerts. Enter the
   Home Assistant login from wxalerts.org (Account, Home Assistant) and your
   places appear.

Automations that pointed at 0.x entities need repointing: the new ids are
`binary_sensor.wxalerts_<place>_...` per place. Automations on the
`wxalerts_alert` and `wxalerts_hazard` events now get `place_id` and
`place_name` instead of `location` and `zone`.

## Troubleshooting

- **"That login did not work"**: check the username and password under
  Account, Home Assistant on wxalerts.org, or rotate the password there and
  enter the new one. Home Assistant needs the Supporter plan.
- **Re-authenticate prompt after it was working**: the password was rotated
  or revoked, or the Supporter plan lapsed. Enter the current login.
- **No devices after setup**: the account has no saved places yet. Add one on
  wxalerts.org and it appears within seconds.
- **Alert entities unavailable for a place**: the place has no US county
  (offshore or outside the US). Storm cells still work.
- **Storm cell entities unavailable**: check `sensor.wxalerts_feed_age` and
  `binary_sensor.wxalerts_connected`. Unavailable means the feed has not
  reported for 15 minutes or Home Assistant is offline, never that the sky
  is clear.
- **Some counties have no alerts**: the broker allows 20 subscriptions per
  client, which covers 17 distinct counties. A warning in the log names how
  many were left out.
- **Diagnostics**: on the integration page, open the menu and choose
  **Download diagnostics**. The username, password and place coordinates are
  redacted.

## How it connects

One MQTT v5 connection over websockets to `wss://mqtt.wxalerts.org/mqtt`
(port 443), signed in with your Home Assistant login. It subscribes to your
account's topics (`wxalerts/u/<username>/#`: places, storm cells and the
feed status) and to the public alert tree of each county your places are in.
Everything is retained, so every entity is correct the moment Home Assistant
starts, with no polling.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest            # the full suite, no network
WXALERTS_HA_USERNAME=ha-... WXALERTS_HA_PASSWORD=... \
  .venv/bin/python -m pytest -m live  # against the production broker
```

The default run fakes the MQTT transport, so it is deterministic and offline.
`FeedClient` in `coordinator.py` is the whole MQTT surface; every topic name
and payload field lives in the topic contract block of `const.py`.

Leave `custom_components/wxalerts/brand/icon.png` where it is. Home Assistant
2026.3 and later serve a custom integration's icon straight out of that
folder, and HACS validation fails without it.

## Contributing

Issues and pull requests are welcome at
[github.com/wxalerts/WxAlerts-HA](https://github.com/wxalerts/WxAlerts-HA).
CI runs hassfest, HACS validation and the test suite.

## License

MIT. See [LICENSE](LICENSE).
