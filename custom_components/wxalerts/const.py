"""Constants for the WxAlerts integration."""

from __future__ import annotations

DOMAIN = "wxalerts"

# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------
DEFAULT_HOST = "mqtt.wxalerts.org"
DEFAULT_PORT = 443
DEFAULT_WS_PATH = "/mqtt"
DEFAULT_USERNAME = "wxalerts"
DEFAULT_PASSWORD = "wxalerts"

TOPIC_SAME_FMT = "wxalerts/nws/v1/same/{same}/#"
TOPIC_GLM_ROOT = "wxalerts/glm/v1"

# Broker enforces 20 subscriptions per client; leave headroom.
MAX_SUBSCRIPTIONS = 18

# ---------------------------------------------------------------------------
# Config entry / options keys
# ---------------------------------------------------------------------------
CONF_LOCATIONS = "locations"  # list of resolved location dicts
CONF_ZONES = "zones"  # zone entity_ids picked in the flow
CONF_ENABLE_ALERTS = "enable_alerts"
CONF_ENABLE_GLM = "enable_glm"
CONF_GLM_PRECISION = "glm_precision"  # geohash chars used for subscription
CONF_GLM_WINDOW = "glm_window_minutes"
CONF_PHENOMENON_SENSORS = "phenomenon_sensors"

# Per-location dict keys (stored in the config entry)
LOC_ZONE_ENTITY = "zone_entity_id"
LOC_NAME = "name"
LOC_LAT = "latitude"
LOC_LON = "longitude"
LOC_SAME = "same"
LOC_COUNTY = "county"
LOC_STATE = "state"
LOC_GEOHASH = "geohash"

DEFAULT_ENABLE_ALERTS = True
DEFAULT_ENABLE_GLM = True
# 4 geohash characters ~= a 39 km x 19.5 km box — county scale.
DEFAULT_GLM_PRECISION = 4
DEFAULT_GLM_WINDOW = 15
DEFAULT_PHENOMENON_SENSORS = True

# ---------------------------------------------------------------------------
# Events / dispatcher signals
# ---------------------------------------------------------------------------
EVENT_ALERT = "wxalerts_alert"

SIGNAL_ALERTS_UPDATED = f"{DOMAIN}_alerts_updated"  # + _{entry_id}_{same}
SIGNAL_GLM_UPDATED = f"{DOMAIN}_glm_updated"  # + _{entry_id}_{geohash}
SIGNAL_CONNECTION = f"{DOMAIN}_connection"  # + _{entry_id}

# ---------------------------------------------------------------------------
# Alert semantics
# ---------------------------------------------------------------------------
SEVERITY_ORDER = ["Extreme", "Severe", "Moderate", "Minor"]
SEVERITY_NONE = "None"

# Phenomena for the optional per-phenomenon binary sensors.
PHENOMENON_SENSOR_TYPES: dict[str, str] = {
    "TO": "Tornado",
    "SV": "Severe Thunderstorm",
    "FF": "Flash Flood",
    "FA": "Flood",
    "WS": "Winter Storm",
    "HT": "Heat",
}

# ---------------------------------------------------------------------------
# NWS point metadata API (used once, during config flow, to resolve a zone
# to its county UGC — never polled at runtime).
# ---------------------------------------------------------------------------
NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"

# State/territory postal abbreviation -> FIPS code, for converting a county
# UGC ("FLC113") into a SAME code ("0" + state FIPS + county number).
STATE_FIPS: dict[str, str] = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
    "AS": "60", "GU": "66", "MP": "69", "PR": "72", "VI": "78",
}
