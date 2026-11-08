"""Diagnostics for a WxAlerts entry.

The OAuth token signs in to the account, the broker password opens its topic
tree and the username names it, so all three are redacted, along with the
instance id and place coordinates. Topics are reported
with the username replaced, which keeps their shape readable in a bug report.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import WxAlertsConfigEntry
from .const import CONF_INSTANCE_ID, CONF_TOKEN, PLACE_LAT, PLACE_LON

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_TOKEN,
    CONF_INSTANCE_ID,
    PLACE_LAT,
    PLACE_LON,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WxAlertsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    username = coordinator.username

    def _scrub(topic: str) -> str:
        return topic.replace(username, REDACTED) if username else topic

    return {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "subscriptions": [
            {"topic": _scrub(topic), "qos": qos}
            for topic, qos in coordinator.subscriptions
        ],
        "state": coordinator.diagnostics(),
    }
