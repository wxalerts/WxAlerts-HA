"""Diagnostics for a WxAlerts entry.

The OAuth token signs in to the account, the broker password opens its topic
tree and the username (or, on V2, the account id and instance key) names it, so
these are redacted, along with the
instance id and place coordinates. Topics are reported
with the username replaced, which keeps their shape readable in a bug report.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import WxAlertsConfigEntry
from .const import (
    CONF_INSTANCE_ID,
    CONF_TOKEN,
    CONF_V2_INSTANCE,
    CONF_V2_USER_ID,
    PLACE_LAT,
    PLACE_LON,
)

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_TOKEN,
    CONF_INSTANCE_ID,
    CONF_V2_USER_ID,
    CONF_V2_INSTANCE,
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
        if username:
            topic = topic.replace(username, REDACTED)
        if coordinator.v2:
            # V2 topics name the account id and the instance key instead.
            topic = topic.replace(f"/{coordinator.v2_user_id}/", f"/{REDACTED}/")
            topic = topic.replace(f"/{coordinator.v2_instance}/", f"/{REDACTED}/")
        return topic

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
