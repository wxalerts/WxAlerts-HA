# WxAlerts for Home Assistant

[![HACS Custom Repository](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub Release](https://img.shields.io/github/release/wxalerts/WxAlerts-HA.svg)](https://github.com/wxalerts/WxAlerts-HA/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Real-time NWS weather alert binary sensors for Home Assistant, powered by the [WxAlerts](https://wxalerts.org) MQTT broker.

WxAlerts polls the NWS API every 60 seconds and pushes alerts to MQTT the moment they're issued or updated — no polling from your HA instance required.

---

## Features

- **Real-time push alerts** via MQTT — no NWS API polling from your side
- **One binary sensor per monitored zone** — `on` when alerts are active, `off` when clear
- **Full alert attributes** — event type, severity, urgency, onset, expires, and more
- **Multi-alert support** — multiple simultaneous alerts per zone are all tracked
- **Worst-case attributes** — `worst_severity` and `worst_event` for easy automation triggers
- **Auto-expiry** — alerts prune themselves when their `expires` timestamp passes
- **Independent MQTT connection** — does not conflict with HA's built-in MQTT integration
- **Multi-zone, multi-state** — monitor any combination of NWS UGC zones

---

## Installation

### Via HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations**
3. Click the three-dot menu → **Custom repositories**
4. Add `https://github.com/wxalerts/WxAlerts-HA` with category **Integration**
5. Search for **WxAlerts** and install
6. Restart Home Assistant

### Manual

1. Download the latest release
2. Copy `custom_components/wxalerts/` to your HA `config/custom_components/` directory
3. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **WxAlerts**
3. Follow the setup flow:

### Step 1 — Select State
Choose the state you want to monitor alerts for.

### Step 2 — Select County / Zones
- Leave the county field blank to see all zones for the state, or type a county name to fuzzy search
- Select one or more zones from the dropdown
- Check **Add zones from another state** to repeat the flow for additional states

### Adding More Zones Later
Go to **Settings → Devices & Services → WxAlerts → Configure** to add or remove zones.

---

## Entities

Each monitored zone creates one `binary_sensor` entity:

| Entity | State | Description |
|--------|-------|-------------|
| `binary_sensor.wxalerts_flz202` | `on` / `off` | Active alert state for zone FLZ202 |

### Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `zone_id` | string | NWS UGC zone code (e.g. `FLZ202`) |
| `zone_name` | string | Human-readable zone name |
| `alert_count` | int | Number of currently active alerts |
| `alerts` | list | Full list of active alert objects |
| `worst_severity` | string | Highest severity among active alerts |
| `worst_urgency` | string | Highest urgency among active alerts |
| `worst_event` | string | Event name of the worst active alert |

### Alert Object Fields

Each entry in the `alerts` list contains:

| Field | Description |
|-------|-------------|
| `nws_id` | Unique NWS alert identifier |
| `event` | Alert type (e.g. "Tornado Warning") |
| `area` | Affected area description |
| `severity` | `Extreme`, `Severe`, `Moderate`, `Minor`, or `Unknown` |
| `urgency` | `Immediate`, `Expected`, `Future`, `Past`, or `Unknown` |
| `certainty` | `Observed`, `Likely`, `Possible`, `Unlikely`, or `Unknown` |
| `onset` | ISO 8601 start time |
| `expires` | ISO 8601 expiry time |

---

## Automation Examples

### Notify on any active alert

```yaml
automation:
  - alias: "WxAlerts — Notify on Active Alert"
    trigger:
      - platform: state
        entity_id: binary_sensor.wxalerts_flz202
        to: "on"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "⚠️ {{ state_attr('binary_sensor.wxalerts_flz202', 'worst_event') }}"
          message: >
            {{ state_attr('binary_sensor.wxalerts_flz202', 'worst_severity') }} severity alert active
            for {{ state_attr('binary_sensor.wxalerts_flz202', 'zone_name') }}.
          data:
            tag: "wxalerts-flz202"
```

### Only notify on Severe or Extreme alerts

```yaml
automation:
  - alias: "WxAlerts — Severe Alert Notification"
    trigger:
      - platform: state
        entity_id: binary_sensor.wxalerts_flz202
        to: "on"
    condition:
      - condition: template
        value_template: >
          {{ state_attr('binary_sensor.wxalerts_flz202', 'worst_severity')
             in ['Severe', 'Extreme'] }}
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "🚨 {{ state_attr('binary_sensor.wxalerts_flz202', 'worst_event') }}"
          message: >
            {{ state_attr('binary_sensor.wxalerts_flz202', 'worst_severity') }} —
            {{ state_attr('binary_sensor.wxalerts_flz202', 'zone_name') }}
```

### All-clear notification

```yaml
automation:
  - alias: "WxAlerts — All Clear"
    trigger:
      - platform: state
        entity_id: binary_sensor.wxalerts_flz202
        to: "off"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "✅ All Clear — {{ state_attr('binary_sensor.wxalerts_flz202', 'zone_name') }}"
          message: "All weather alerts have expired or been cancelled."
          data:
            tag: "wxalerts-flz202"
```

---

## Finding Your NWS Zone

Not sure which zone you're in? Use the setup flow's county search, or look up your zone at [alerts.weather.gov](https://alerts.weather.gov).

---

## About WxAlerts

[WxAlerts](https://wxalerts.org) is a nonprofit open-source weather alerting platform based in Milton, FL. It enhances open-source tools with real-time NWS alert delivery via MQTT, REST, and Meshtastic.

- MQTT broker: `mqtt.wxalerts.org:8883` (TLS)
- Public credentials: username `wxalerts` / password `wxalerts`
- Developer portal: [wxalerts.org/dev-portal](https://wxalerts.org/dev-portal)

---

## Contributing

Issues and PRs welcome at [github.com/wxalerts/WxAlerts-HA](https://github.com/wxalerts/WxAlerts-HA).

## License

MIT — see [LICENSE](LICENSE)
