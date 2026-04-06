"""Independent MQTT client for WxAlerts integration."""
from __future__ import annotations

import json
import logging
import ssl
import threading
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from .const import (
    MQTT_HOST,
    MQTT_KEEPALIVE,
    MQTT_PASSWORD,
    MQTT_PORT,
    MQTT_RECONNECT_DELAY,
    MQTT_USERNAME,
)

_LOGGER = logging.getLogger(__name__)


class WxAlertsMQTTClient:
    """Manages a persistent MQTT connection to mqtt.wxalerts.org."""

    def __init__(self) -> None:
        self._client: mqtt.Client | None = None
        self._callbacks: dict[str, Callable[[str, dict | None], None]] = {}
        self._subscribed_topics: set[str] = set()
        self._lock = threading.Lock()
        self._connected = False

    def start(self) -> None:
        """Connect to the WxAlerts MQTT broker."""
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        tls_context = ssl.create_default_context()
        self._client.tls_set_context(tls_context)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        self._client.reconnect_delay_set(min_delay=1, max_delay=MQTT_RECONNECT_DELAY)
        self._client.connect_async(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
        self._client.loop_start()
        _LOGGER.debug("WxAlerts MQTT client started")

    def stop(self) -> None:
        """Disconnect and clean up."""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        self._connected = False
        _LOGGER.debug("WxAlerts MQTT client stopped")

    def subscribe(
        self,
        topic: str,
        callback: Callable[[str, dict | None], None],
    ) -> None:
        """Subscribe to a topic and register a callback."""
        with self._lock:
            self._callbacks[topic] = callback
            self._subscribed_topics.add(topic)
            if self._connected and self._client:
                self._client.subscribe(topic, qos=1)
                _LOGGER.debug("Subscribed to topic: %s", topic)

    def unsubscribe(self, topic: str) -> None:
        """Unsubscribe from a topic."""
        with self._lock:
            self._callbacks.pop(topic, None)
            self._subscribed_topics.discard(topic)
            if self._connected and self._client:
                self._client.unsubscribe(topic)
                _LOGGER.debug("Unsubscribed from topic: %s", topic)

    @property
    def connected(self) -> bool:
        """Return connection state."""
        return self._connected

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Handle successful connection — resubscribe all topics."""
        if reason_code == 0:
            self._connected = True
            _LOGGER.info("Connected to WxAlerts MQTT broker")
            with self._lock:
                for topic in self._subscribed_topics:
                    client.subscribe(topic, qos=1)
                    _LOGGER.debug("Resubscribed to: %s", topic)
        else:
            _LOGGER.error(
                "WxAlerts MQTT connection failed with reason code: %s", reason_code
            )

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Handle disconnection."""
        self._connected = False
        if reason_code != 0:
            _LOGGER.warning(
                "WxAlerts MQTT disconnected unexpectedly (code: %s), will retry",
                reason_code,
            )

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        """Dispatch incoming messages to registered callbacks."""
        topic = message.topic
        payload_bytes = message.payload

        # Tombstone = empty retained message
        if not payload_bytes:
            payload = None
        else:
            try:
                payload = json.loads(payload_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                _LOGGER.warning("Invalid JSON on topic %s, ignoring", topic)
                return

        # Match topic to registered callbacks
        with self._lock:
            callbacks = dict(self._callbacks)

        for registered_topic, callback in callbacks.items():
            if mqtt.topic_matches_sub(registered_topic, topic):
                try:
                    callback(topic, payload)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Error in callback for topic %s", topic)
