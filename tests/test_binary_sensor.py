"""Tests for WxAlerts binary sensor."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.wxalerts.binary_sensor import WxAlertsZoneSensor
from custom_components.wxalerts.const import DOMAIN


MOCK_ZONE = {
    "zone_id": "FLZ202",
    "zone_name": "Santa Rosa Coastal",
    "state": "FL",
}

MOCK_ALERT = {
    "nws_id": "urn:oid:2.49.0.1.840.0.abc123.001.1",
    "event": "Rip Current Statement",
    "area": "Santa Rosa Coastal",
    "severity": "Moderate",
    "urgency": "Expected",
    "certainty": "Likely",
    "onset": "2026-04-08T01:00:00-05:00",
    "expires": "2099-01-01T00:00:00+00:00",  # Far future so it won't prune
}

MOCK_ALERT_2 = {
    "nws_id": "urn:oid:2.49.0.1.840.0.def456.001.1",
    "event": "Coastal Flood Watch",
    "area": "Santa Rosa Coastal",
    "severity": "Severe",
    "urgency": "Immediate",
    "certainty": "Likely",
    "onset": "2026-04-08T01:00:00-05:00",
    "expires": "2099-01-01T00:00:00+00:00",
}


def _make_sensor() -> WxAlertsZoneSensor:
    hass = MagicMock()
    mqtt_client = MagicMock()
    sensor = WxAlertsZoneSensor(hass, mqtt_client, MOCK_ZONE)
    sensor.schedule_update_ha_state = MagicMock()  # no real HA event loop in tests
    return sensor


def test_initial_state_is_off():
    sensor = _make_sensor()
    assert sensor.is_on is False


def test_alert_turns_sensor_on():
    sensor = _make_sensor()
    sensor._on_mqtt_message("alerts/nws/FL/FLZ202", MOCK_ALERT)
    assert sensor.is_on is True


def test_tombstone_turns_sensor_off():
    sensor = _make_sensor()
    sensor._on_mqtt_message("alerts/nws/FL/FLZ202", MOCK_ALERT)
    assert sensor.is_on is True
    sensor._on_mqtt_message("alerts/nws/FL/FLZ202", None)
    assert sensor.is_on is False


def test_multiple_alerts_accumulated():
    sensor = _make_sensor()
    sensor._on_mqtt_message("alerts/nws/FL/FLZ202", MOCK_ALERT)
    sensor._on_mqtt_message("alerts/nws/FL/FLZ202", MOCK_ALERT_2)
    attrs = sensor.extra_state_attributes
    assert attrs["alert_count"] == 2


def test_worst_severity_calculated():
    sensor = _make_sensor()
    sensor._on_mqtt_message("alerts/nws/FL/FLZ202", MOCK_ALERT)
    sensor._on_mqtt_message("alerts/nws/FL/FLZ202", MOCK_ALERT_2)
    attrs = sensor.extra_state_attributes
    assert attrs["worst_severity"] == "Severe"
    assert attrs["worst_event"] == "Coastal Flood Watch"


def test_duplicate_alert_updates_not_duplicates():
    sensor = _make_sensor()
    sensor._on_mqtt_message("alerts/nws/FL/FLZ202", MOCK_ALERT)
    updated = {**MOCK_ALERT, "severity": "Severe"}
    sensor._on_mqtt_message("alerts/nws/FL/FLZ202", updated)
    attrs = sensor.extra_state_attributes
    assert attrs["alert_count"] == 1
    assert attrs["alerts"][0]["severity"] == "Severe"


def test_unique_id():
    sensor = _make_sensor()
    assert sensor.unique_id == "wxalerts_flz202"


def test_empty_attributes_when_no_alerts():
    sensor = _make_sensor()
    attrs = sensor.extra_state_attributes
    assert attrs["alert_count"] == 0
    assert attrs["alerts"] == []
    assert attrs["worst_severity"] is None
