"""Constants for the WxAlerts integration."""

DOMAIN = "wxalerts"

# WxAlerts MQTT broker
MQTT_HOST = "mqtt.wxalerts.org"
MQTT_PORT = 8883
MQTT_USERNAME = "wxalerts"
MQTT_PASSWORD = "wxalerts"
MQTT_TLS = True
MQTT_KEEPALIVE = 60
MQTT_RECONNECT_DELAY = 5

# WxAlerts REST API
API_BASE_URL = "https://api.wxalerts.org"
API_STATES_ENDPOINT = "/alerts/zone/states"
API_COUNTIES_ENDPOINT = "/alerts/zone/counties"
API_SEARCH_ENDPOINT = "/alerts/zone/search"

# Topic pattern
TOPIC_PATTERN = "alerts/nws/{state}/{zone}"
TOPIC_WILDCARD = "alerts/nws/#"

# Config keys
CONF_ZONES = "zones"
CONF_STATE = "state"
CONF_COUNTY = "county"
CONF_ZONE_ID = "zone_id"
CONF_ZONE_NAME = "zone_name"

# Severity ordering for worst-case calculation
SEVERITY_ORDER = {
    "Extreme": 4,
    "Severe": 3,
    "Moderate": 2,
    "Minor": 1,
    "Unknown": 0,
}

URGENCY_ORDER = {
    "Immediate": 3,
    "Expected": 2,
    "Future": 1,
    "Past": 0,
    "Unknown": 0,
}
