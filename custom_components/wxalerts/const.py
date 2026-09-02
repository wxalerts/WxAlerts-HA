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
CONF_GLM_BACKFILL = "glm_backfill_url"
# Legacy: a single switch for the six hardcoded phenomenon buckets. Read
# only during migration, never written.
CONF_PHENOMENON_SENSORS = "phenomenon_sensors"
# Catalogue keys the user picked; one binary sensor per key per location.
CONF_EVENT_SENSORS = "event_sensors"

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
# Blank derives it from the broker host. The endpoint may not be routed on
# every deployment, so a failed backfill is a debug line and a slower ramp,
# never a setup failure.
DEFAULT_GLM_BACKFILL = ""
BACKFILL_PATH = "/glm/flashes"
BACKFILL_TIMEOUT = 20

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
# Fired once per newly issued hazard, carrying that one product.
EVENT_ALERT = "wxalerts_alert"
# Fired when an event TYPE starts or stops being in effect for a location.
# Distinct from EVENT_ALERT: a second tornado warning while one is already
# live is a new alert but not a new hazard type, and a device trigger that
# fired on it would announce a tornado that was already being announced.
EVENT_HAZARD = "wxalerts_hazard"
HAZARD_BEGIN = "begin"
HAZARD_END = "end"

SIGNAL_ALERTS_UPDATED = f"{DOMAIN}_alerts_updated"  # + _{entry_id}_{same}
SIGNAL_GLM_UPDATED = f"{DOMAIN}_glm_updated"  # + _{entry_id}_{target_key}
SIGNAL_CONNECTION = f"{DOMAIN}_connection"  # + _{entry_id}

# ---------------------------------------------------------------------------
# Alert semantics
# ---------------------------------------------------------------------------
SEVERITY_ORDER = ["Extreme", "Severe", "Moderate", "Minor"]
SEVERITY_NONE = "None"

# Tie-break within one severity. NWS assigns severity per product type, so a
# Tornado Watch and a Severe Thunderstorm Warning are both "Severe" — without
# this the top-alert sensor would pick between them by dictionary order.
SIGNIFICANCE_ORDER = ["W", "A", "Y", "S", "F", "O", "N"]

# The six phenomenon buckets shipped through 0.3.x, mapped to the catalogue
# keys that replace them. Only used to clean their orphaned entities out of the
# registry on upgrade; see async_migrate_entry.
LEGACY_PHENOMENON_CODES: tuple[str, ...] = ("TO", "SV", "FF", "FA", "WS", "HT")

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
