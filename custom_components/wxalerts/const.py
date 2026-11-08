"""Constants for the WxAlerts integration."""

from __future__ import annotations

DOMAIN = "wxalerts"

# ===========================================================================
# Broker
# ===========================================================================
DEFAULT_HOST = "mqtt.wxalerts.org"
DEFAULT_PORT = 443
DEFAULT_WS_PATH = "/mqtt"
BROKER_URL = f"wss://{DEFAULT_HOST}{DEFAULT_WS_PATH}"

# The shared read-only login. It still reaches the public alert topics, but it
# has no account behind it, so it cannot carry places or storm cells. Entered
# in the config flow it gets the Supporter upsell rather than a confusing
# "no places" result.
PUBLIC_USERNAME = "wxalerts"

# Where a Free account goes to upgrade. Shown in the config flow error.
PRICING_URL = "https://wxalerts.org/pricing/"
ACCOUNT_URL = "https://wxalerts.org/account/"

# How long the config flow waits for the retained places list after a good
# login. A miss is not an error: the running integration picks it up later.
PLACES_WAIT_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 20.0

# Broker enforces 20 subscriptions per client; leave headroom.
MAX_SUBSCRIPTIONS = 18

# ===========================================================================
# Topic contract
#
# Every topic name and payload field this integration reads lives in this
# block, so a rename on the server is a one-line change here.
# ===========================================================================
# Public NWS alert tree, one subtree per county SAME code. Unchanged from 0.x.
TOPIC_SAME_FMT = "wxalerts/nws/v1/same/{same}/#"
TOPIC_ALERT_ROOT = "wxalerts/nws/v1/same/"

# Per-user tree. One wildcard subscription covers places, status and storm
# cells, which keeps the subscription budget for county alert trees.
TOPIC_USER_PREFIX_FMT = "wxalerts/u/{username}/"
TOPIC_USER_WILDCARD_FMT = "wxalerts/u/{username}/#"
# Suffixes below the per-user prefix.
TOPIC_PLACES = "places"
TOPIC_STATUS = "status"
# storms/place/<place_id>/<storm_id>
TOPIC_STORMS_PLACE = "storms/place"

# places: retained JSON list of objects with these keys. "same" is optional.
PLACE_ID = "id"
PLACE_NAME = "name"
PLACE_LAT = "lat"
PLACE_LON = "lon"
PLACE_SAME = "same"

# status: retained heartbeat.
STATUS_ONLINE = "online"
STATUS_UPDATED_AT = "updated_at"

# storms/place/<place_id>/<storm_id>: retained while the cell threatens the
# place; an empty retained message clears it.
STORM_ID = "storm_id"
STORM_PLACE_ID = "place_id"
STORM_PLACE_NAME = "place_name"
STORM_EPISODE = "episode"
STORM_ETA_MIN = "eta_min"
STORM_RUNG = "rung"
STORM_RUNG_LABEL = "rung_label"
STORM_PROB_SEVERE = "prob_severe"
STORM_PROB_TOR = "prob_tor"
STORM_PROB_HAIL = "prob_hail"
STORM_PROB_WIND = "prob_wind"
STORM_SPEED_KMH = "speed_kmh"
STORM_BEARING_DEG = "bearing_deg"
STORM_DISTANCE_KM = "distance_km"
STORM_ISSUED_AT = "issued_at"
STORM_UPDATED_AT = "updated_at"
STORM_FIELDS: tuple[str, ...] = (
    STORM_ID,
    STORM_PLACE_ID,
    STORM_PLACE_NAME,
    STORM_EPISODE,
    STORM_ETA_MIN,
    STORM_RUNG,
    STORM_RUNG_LABEL,
    STORM_PROB_SEVERE,
    STORM_PROB_TOR,
    STORM_PROB_HAIL,
    STORM_PROB_WIND,
    STORM_SPEED_KMH,
    STORM_BEARING_DEG,
    STORM_DISTANCE_KM,
    STORM_ISSUED_AT,
    STORM_UPDATED_AT,
)

EPISODE_OPEN = "open"
EPISODE_ESCALATE = "escalate"
EPISODE_UPDATE = "update"
# Episodes that fire the wxalerts_storm_cell event.
EPISODES_THAT_NOTIFY = (EPISODE_OPEN, EPISODE_ESCALATE)

# The WXA/SA ladder (api services/rungs.py). The threat sensor is an enum, so
# every rung the server can send needs a stable option. A rung this table does
# not know maps down to the nearest one below it rather than breaking the enum.
THREAT_NONE = "none"
RUNG_OPTIONS: dict[int, str] = {
    10: "wind_outlook",
    20: "high_wind_detected",
    30: "thunderstorm_outlook",
    40: "thunderstorm_risk",
    50: "thunderstorm_detected",
    60: "tornado_outlook",
    70: "tornado_risk",
    75: "tornado_risk_high",
    80: "tornado_detected",
    85: "tornado_confirmed",
}
THREAT_OPTIONS: list[str] = [THREAT_NONE, *RUNG_OPTIONS.values()]

# ===========================================================================
# Timers and staleness
# ===========================================================================
# How often time-based state is re-evaluated with no messages arriving: feed
# age, the storm ETA countdown and staleness.
TICK_SECONDS = 30
# The status heartbeat comes every few minutes. Past this, storm cell entities
# go unavailable rather than reporting a calm "off" from a feed that stopped.
STATUS_STALE_SECONDS = 900
# A storm cell that has not been updated for this long is dropped, so a missed
# clear cannot leave a storm approaching forever.
STORM_EXPIRE_SECONDS = 1800

# ===========================================================================
# Config entry / options keys
# ===========================================================================
# Cached copy of the last places list, so entities exist at start-up before
# the broker has replayed the retained topic.
CONF_PLACES = "places"
# Catalogue keys the user picked; one binary sensor per key per place.
CONF_EVENT_SENSORS = "event_sensors"
# Legacy: a single switch for the six hardcoded phenomenon buckets (0.3.x).
# Read only by the 0.x migration, never written.
CONF_PHENOMENON_SENSORS = "phenomenon_sensors"

# Config entry schema version. 1 was every 0.x release (zones, lightning, the
# public login); 2 is places and a per-user broker login.
CONFIG_VERSION = 2

# ===========================================================================
# Events / dispatcher signals
# ===========================================================================
# Fired once per newly issued hazard, per place it affects.
EVENT_ALERT = "wxalerts_alert"
# Fired when an event TYPE starts or stops being in effect for a place.
# Distinct from EVENT_ALERT: a second tornado warning while one is already
# live is a new alert but not a new hazard type, and a device trigger that
# fired on it would announce a tornado that was already being announced.
EVENT_HAZARD = "wxalerts_hazard"
HAZARD_BEGIN = "begin"
HAZARD_END = "end"
# Fired when a storm cell episode opens or escalates for a place.
EVENT_STORM_CELL = "wxalerts_storm_cell"

SIGNAL_ALERTS_UPDATED = f"{DOMAIN}_alerts_updated"  # + _{entry_id}_{same}
SIGNAL_STORMS_UPDATED = f"{DOMAIN}_storms_updated"  # + _{entry_id}_{place_id}
SIGNAL_PLACE_UPDATED = f"{DOMAIN}_place_updated"  # + _{entry_id}_{place_id}
SIGNAL_CONNECTION = f"{DOMAIN}_connection"  # + _{entry_id}
SIGNAL_TICK = f"{DOMAIN}_tick"  # + _{entry_id}

# ===========================================================================
# Alert semantics
# ===========================================================================
SEVERITY_ORDER = ["Extreme", "Severe", "Moderate", "Minor"]
SEVERITY_NONE = "None"

# Tie-break within one severity. NWS assigns severity per product type, so a
# Tornado Watch and a Severe Thunderstorm Warning are both "Severe"; without
# this the top-alert sensor would pick between them by dictionary order.
SIGNIFICANCE_ORDER = ["W", "A", "Y", "S", "F", "O", "N"]
