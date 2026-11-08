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

# Where a Free account, or one out of Home Assistant slots, goes to upgrade.
PRICING_URL = "https://wxalerts.org/pricing/"

# ===========================================================================
# Sign-in (authentik, OAuth2 authorization code with PKCE)
# ===========================================================================
# Public client: no secret, so it is safe to compile in. Issued by the
# authentik application "wxalerts-home-assistant".
HA_OAUTH_CLIENT_ID = "5094664ecabab38b86e8c7153b821f07e994096b"
# authentik serves one authorize and one token endpoint for every
# application; the per-application issuer is
# https://auth.wxalerts.org/application/o/<slug>/. Confirmed against
# .well-known/openid-configuration of the wxalerts-home-assistant issuer.
OAUTH_ISSUER_BASE = "https://auth.wxalerts.org/application/o/"
OAUTH_AUTHORIZE_URL = f"{OAUTH_ISSUER_BASE}authorize/"
OAUTH_TOKEN_URL = f"{OAUTH_ISSUER_BASE}token/"
OAUTH_SCOPES = "openid profile email offline_access"

# Account API: registers this instance and returns its broker login.
API_BASE_URL = "https://api.wxalerts.org"
API_INSTANCES_URL = f"{API_BASE_URL}/integrations/home-assistant/instances"
API_HEARTBEAT_URL_FMT = f"{API_INSTANCES_URL}/{{instance_id}}/heartbeat"
API_TIMEOUT_SECONDS = 20
# The heartbeat is the website's only "last seen" for an instance.
HEARTBEAT_SECONDS = 6 * 3600
# deactivated_reason values on the instance entry.
REASON_REVOKED = "revoked"
REASON_OVER_LIMIT = "over_limit"
REASON_PLAN_LAPSED = "plan_lapsed"
# Repair issue ids.
ISSUE_REVOKED = "instance_revoked"
ISSUE_OVER_LIMIT = "instance_over_limit"
ISSUE_PLAN_LAPSED = "instance_plan_lapsed"
# A broker login refreshed this recently that is rejected again goes straight
# to a new sign-in rather than looping on re-registration.
RELOGIN_COOLDOWN_SECONDS = 600
# Retry delay when the API cannot be reached during a login refresh.
RELOGIN_RETRY_SECONDS = 300

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
# Present with online false when the server ends the session on purpose:
# "revoked", "over_limit" or "plan_lapsed".
STATUS_REASON = "reason"

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
# The OAuth token as Home Assistant's OAuth2 helper stores it.
CONF_TOKEN = "token"
# Home Assistant's installation id, as registered with the API.
CONF_INSTANCE_ID = "instance_id"
# Cached copy of the last places list, so entities exist at start-up before
# the broker has replayed the retained topic.
CONF_PLACES = "places"
# Catalogue keys the user picked; one binary sensor per key per place.
CONF_EVENT_SENSORS = "event_sensors"
# Legacy: a single switch for the six hardcoded phenomenon buckets (0.3.x).
# Read only by the 0.x migration, never written.
CONF_PHENOMENON_SENSORS = "phenomenon_sensors"

# Config entry schema version. 1 was every 0.x release (zones, lightning, the
# public login); 2 is places, a wxalerts.org sign-in and a per-instance
# broker login.
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
