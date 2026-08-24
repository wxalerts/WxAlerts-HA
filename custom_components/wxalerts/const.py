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
# Retained heartbeat from the lightning ingest. Four levels, so it can never be
# confused with a flash burst (which is always eight).
TOPIC_GLM_STATUS = "wxalerts/glm/v1/status"

# Broker enforces 20 subscriptions per client; leave headroom.
MAX_SUBSCRIPTIONS = 18

# ---------------------------------------------------------------------------
# Config entry / options keys
# ---------------------------------------------------------------------------
CONF_LOCATIONS = "locations"  # list of resolved location dicts
CONF_ZONES = "zones"  # zone entity_ids picked in the flow
CONF_ENABLE_ALERTS = "enable_alerts"
CONF_ENABLE_GLM = "enable_glm"
CONF_GLM_PRECISION = "glm_precision"  # legacy: geohash chars, migrated to radius
CONF_GLM_RADIUS = "glm_radius_mi"  # outer ring, and what the subscription covers
CONF_GLM_ALERT = "glm_alert_mi"  # drives the safety binary sensor
CONF_GLM_WINDOW = "glm_window_minutes"
CONF_GLM_PERSONS = "glm_track_persons"
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
# 30 miles is the outer ring the rate and trend are measured over; 10 miles is
# the NWS "when thunder roars" seek-shelter distance and the one that fires the
# safety sensor. Neither is a box size — the subscription is derived from them.
DEFAULT_GLM_RADIUS = 30
DEFAULT_GLM_ALERT = 10
DEFAULT_GLM_WINDOW = 30
DEFAULT_GLM_PERSONS = False
DEFAULT_PHENOMENON_SENSORS = True

# Ring counts published as their own sensors, in miles. Rings at or beyond the
# configured radius are dropped rather than reported as a smaller circle.
RING_MILES = (5, 10, 20, 30)

# Nearest-distance history depth and minimum spacing for the trend label.
# Comparing first against last over several samples means one flash on the far
# edge of a cell cannot flip "Approaching", the way consecutive samples would.
TREND_SAMPLES = 5
TREND_MIN_INTERVAL = 40.0
TREND_APPROACHING = "Approaching"
TREND_RECEDING = "Receding"
TREND_STEADY = "Steady"
TREND_UNKNOWN = "Unknown"
TREND_OPTIONS = [TREND_APPROACHING, TREND_RECEDING, TREND_STEADY, TREND_UNKNOWN]
# A nearest-strike distance has to move by more than this between the first and
# last sample to count as approaching or receding rather than steady.
TREND_DELTA_MI = 1.0

# Feed age past which lightning entities go unavailable rather than reporting a
# calm zero. A confident zero during an outage is the dangerous failure mode:
# absence of data has to look different from absence of lightning.
GLM_STALE_SECONDS = 600

# ---------------------------------------------------------------------------
# Events / dispatcher signals
# ---------------------------------------------------------------------------
EVENT_ALERT = "wxalerts_alert"

SIGNAL_ALERTS_UPDATED = f"{DOMAIN}_alerts_updated"  # + _{entry_id}_{same}
SIGNAL_GLM_UPDATED = f"{DOMAIN}_glm_updated"  # + _{entry_id}_{target_key}
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
