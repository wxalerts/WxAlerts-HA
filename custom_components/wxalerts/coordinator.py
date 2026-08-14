"""The WxAlerts feed coordinator.

One object owns the single MQTT connection to the wxalerts broker and all
alert/lightning state. Entities subscribe to it through dispatcher signals —
no entity ever holds its own MQTT client.

The MQTT transport lives behind ``FeedClient`` so "where messages come from"
is one swappable object: a future release can point it at a local broker
(bridge deployment) or grow a republish leg without touching entity code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiomqtt

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.util.ssl import get_default_context

from .const import (
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_GLM_PRECISION,
    CONF_GLM_WINDOW,
    CONF_LOCATIONS,
    DEFAULT_ENABLE_ALERTS,
    DEFAULT_ENABLE_GLM,
    DEFAULT_GLM_PRECISION,
    DEFAULT_GLM_WINDOW,
    DEFAULT_HOST,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DEFAULT_WS_PATH,
    EVENT_ALERT,
    LOC_GEOHASH,
    LOC_SAME,
    MAX_SUBSCRIPTIONS,
    SEVERITY_NONE,
    SEVERITY_ORDER,
    SIGNAL_ALERTS_UPDATED,
    SIGNAL_CONNECTION,
    SIGNAL_GLM_UPDATED,
    TOPIC_SAME_FMT,
)
from .geo import glm_topic_for_geohash

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff: the broker rate-limits new connections, and every fresh
# subscription replays the full retained set — never hot-loop.
_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 300.0
_BACKOFF_RESET_AFTER = 120.0  # connection considered stable after this

# Subscribing replays every live hazard as a retained message. That burst is
# current state, not news, and the feed's own guidance is not to notify on it
# — otherwise every HA restart re-announces every warning in effect. Alerts
# arriving within this window of a connection update entities but fire no
# event. The cost is that a hazard issued during those seconds is silent on
# the event bus; its binary_sensor still turns on.
_PRIMING_SECONDS = 5.0


@dataclass
class FeedConfig:
    """Everything FeedClient needs to reach a broker."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    ws_path: str = DEFAULT_WS_PATH
    username: str = DEFAULT_USERNAME
    password: str = DEFAULT_PASSWORD
    client_id: str = ""
    subscriptions: list[tuple[str, int]] = field(default_factory=list)
    # Building an SSL context reads the CA bundle off disk. Hand one in so
    # that never happens on the event loop; FeedClient builds its own in a
    # thread if this is None, so it stays usable outside Home Assistant.
    tls_context: ssl.SSLContext | None = None


class FeedClient:
    """Owns the MQTT connection and pushes every message to one handler.

    Deliberately knows nothing about alerts or lightning — swap this out to
    read from a local broker and the rest of the integration is unchanged.
    """

    def __init__(
        self,
        config: FeedConfig,
        on_message: Callable[[str, bytes], Awaitable[None]],
        on_connection: Callable[[bool], None],
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._on_connection = on_connection
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _tls_context(self) -> ssl.SSLContext:
        """The TLS context, built off the event loop and reused thereafter."""
        if self._config.tls_context is None:
            loop = asyncio.get_running_loop()
            self._config.tls_context = await loop.run_in_executor(
                None, ssl.create_default_context
            )
        return self._config.tls_context

    async def _run(self) -> None:
        backoff = _BACKOFF_INITIAL
        while not self._stopping:
            connected_at = 0.0
            try:
                tls_context = await self._tls_context()
                async with aiomqtt.Client(
                    hostname=self._config.host,
                    port=self._config.port,
                    username=self._config.username,
                    password=self._config.password,
                    identifier=self._config.client_id,
                    protocol=aiomqtt.ProtocolVersion.V5,
                    transport="websockets",
                    websocket_path=self._config.ws_path,
                    tls_context=tls_context,
                    keepalive=60,
                    clean_start=True,
                ) as client:
                    connected_at = time.monotonic()
                    self._on_connection(True)
                    for topic, qos in self._config.subscriptions:
                        await client.subscribe(topic, qos=qos)
                    # Reading the stream is also our liveness check: a held
                    # session with an unread socket does not detect death.
                    async for message in client.messages:
                        payload = message.payload
                        if isinstance(payload, str):
                            payload = payload.encode()
                        await self._on_message(str(message.topic), payload or b"")
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as err:
                _LOGGER.warning("MQTT connection lost: %s", err)
            except Exception:  # noqa: BLE001 - never let the loop die silently
                _LOGGER.exception("Unexpected error in feed loop")
            finally:
                self._on_connection(False)
            if self._stopping:
                return
            if connected_at and time.monotonic() - connected_at > _BACKOFF_RESET_AFTER:
                backoff = _BACKOFF_INITIAL
            _LOGGER.debug("Reconnecting in %.0f s", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


class WxAlertsCoordinator:
    """Holds alert and lightning state for every configured location."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.connected = False

        options = {**entry.data, **entry.options}
        self.locations: list[dict[str, Any]] = options.get(CONF_LOCATIONS, [])
        self.enable_alerts: bool = options.get(
            CONF_ENABLE_ALERTS, DEFAULT_ENABLE_ALERTS
        )
        self.enable_glm: bool = options.get(CONF_ENABLE_GLM, DEFAULT_ENABLE_GLM)
        self.glm_precision: int = int(
            options.get(CONF_GLM_PRECISION, DEFAULT_GLM_PRECISION)
        )
        self.glm_window_minutes: int = int(
            options.get(CONF_GLM_WINDOW, DEFAULT_GLM_WINDOW)
        )

        # Alert state, keyed county -> full topic -> payload. The ETN is the
        # last topic level, so two simultaneous hazards for one county keep
        # distinct keys and a second tornado warning never clobbers the first.
        self._alerts: dict[str, dict[str, dict[str, Any]]] = {}
        # Topics we have already fired the HA event for (events fire per new
        # hazard, not per update — CON/EXT on the same topic stays quiet).
        self._event_fired: set[str] = set()
        # Monotonic deadline for the retained-burst window; see _PRIMING_SECONDS.
        self._priming_until: float = 0.0

        # Lightning, keyed by subscribed geohash prefix -> recent flashes.
        self._flashes: dict[str, deque[dict[str, Any]]] = {}

        self._client: FeedClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def same_codes(self) -> list[str]:
        seen: list[str] = []
        for loc in self.locations:
            code = loc.get(LOC_SAME)
            if code and code not in seen:
                seen.append(code)
        return seen

    @property
    def glm_prefixes(self) -> list[str]:
        seen: list[str] = []
        for loc in self.locations:
            full = loc.get(LOC_GEOHASH) or ""
            prefix = full[: self.glm_precision]
            if prefix and prefix not in seen:
                seen.append(prefix)
        return seen

    def _build_subscriptions(self) -> list[tuple[str, int]]:
        subs: list[tuple[str, int]] = []
        if self.enable_alerts:
            for code in self.same_codes:
                subs.append((TOPIC_SAME_FMT.format(same=code), 1))
        if self.enable_glm:
            for prefix in self.glm_prefixes:
                subs.append((glm_topic_for_geohash(prefix), 0))
        if len(subs) > MAX_SUBSCRIPTIONS:
            # The broker caps subscriptions per client at 20. Keep alerts,
            # trim lightning boxes, and say so instead of failing silently.
            _LOGGER.warning(
                "Configuration needs %d subscriptions but the broker allows %d; "
                "dropping the last %d lightning subscriptions",
                len(subs),
                MAX_SUBSCRIPTIONS,
                len(subs) - MAX_SUBSCRIPTIONS,
            )
            subs = subs[:MAX_SUBSCRIPTIONS]
        return subs

    async def async_start(self) -> None:
        instance_id = await async_get_instance_id(self.hass)
        config = FeedConfig(
            client_id=f"wxha-{instance_id[:12]}-{self.entry.entry_id[:8]}",
            subscriptions=self._build_subscriptions(),
            # Home Assistant pre-warms this at import, so it costs nothing
            # here and never touches the disk from the event loop.
            tls_context=get_default_context(),
        )
        if not config.subscriptions:
            _LOGGER.warning("Nothing to subscribe to — no locations resolved")
            return
        self._client = FeedClient(config, self._handle_message, self._handle_connection)
        self._client.start()

    async def async_stop(self) -> None:
        if self._client:
            await self._client.stop()
            self._client = None

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    @callback
    def _handle_connection(self, connected: bool) -> None:
        self.connected = connected
        if connected:
            # A fresh subscription replays the retained set; hold events off
            # until it has landed.
            self._priming_until = time.monotonic() + _PRIMING_SECONDS
        async_dispatcher_send(
            self.hass, f"{SIGNAL_CONNECTION}_{self.entry.entry_id}", connected
        )

    async def _handle_message(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if topic.startswith("wxalerts/nws/v1/same/") and len(parts) >= 6:
            self._handle_alert(topic, parts[4], payload)
        elif topic.startswith("wxalerts/glm/v1/") and len(parts) == 8:
            self._handle_glm("".join(parts[3:8]), payload)

    def _handle_alert(self, topic: str, same: str, payload: bytes) -> None:
        county = self._alerts.setdefault(same, {})

        if not payload:
            # Tombstone: a zero-length retained message means this hazard is
            # over. Miss this and every warning ever seen stays on forever.
            if county.pop(topic, None) is not None:
                self._event_fired.discard(topic)
                self._dispatch_alerts(same)
            return

        try:
            alert = json.loads(payload)
        except ValueError:
            _LOGGER.warning("Undecodable alert payload on %s", topic)
            return

        is_new = topic not in county
        county[topic] = alert
        self._dispatch_alerts(same)

        # An HA event per *new* hazard preserves the distinction between "a
        # second tornado warning" and "the same one updated", which state
        # changes cannot. Hazards already in effect when we connected are
        # recorded as seen but announced to nobody.
        if is_new and topic not in self._event_fired:
            self._event_fired.add(topic)
            if time.monotonic() < self._priming_until:
                _LOGGER.debug("Retained hazard on %s; not firing an event", topic)
                return
            self.hass.bus.async_fire(
                EVENT_ALERT,
                {
                    "same": same,
                    "topic": topic,
                    "id": alert.get("id"),
                    "vtec": alert.get("vtec"),
                    "event": alert.get("event"),
                    "severity": alert.get("severity"),
                    "urgency": alert.get("urgency"),
                    "certainty": alert.get("certainty"),
                    "headline": alert.get("headline"),
                    "instruction": alert.get("instruction"),
                    "onset": alert.get("onset"),
                    "ends": alert.get("ends"),
                    "action": alert.get("action"),
                },
            )

    def _dispatch_alerts(self, same: str) -> None:
        async_dispatcher_send(
            self.hass, f"{SIGNAL_ALERTS_UPDATED}_{self.entry.entry_id}_{same}"
        )

    def _handle_glm(self, leaf_geohash: str, payload: bytes) -> None:
        if not payload:
            return  # GLM is stateless; nothing is retained or tombstoned
        try:
            burst = json.loads(payload)
        except ValueError:
            return
        flashes = burst.get("flashes") or []
        now = time.time()
        for prefix in self.glm_prefixes:
            if not leaf_geohash.startswith(prefix):
                continue
            bucket = self._flashes.setdefault(prefix, deque())
            for flash in flashes:
                bucket.append(
                    {
                        "received": now,
                        "time": flash.get("t"),
                        "latitude": flash.get("lat"),
                        "longitude": flash.get("lon"),
                        "energy_j": flash.get("energy_j"),
                        "geohash": leaf_geohash,
                    }
                )
            self._prune_flashes(prefix, now)
            async_dispatcher_send(
                self.hass, f"{SIGNAL_GLM_UPDATED}_{self.entry.entry_id}_{prefix}"
            )

    def _prune_flashes(self, prefix: str, now: float | None = None) -> None:
        now = now or time.time()
        cutoff = now - self.glm_window_minutes * 60
        bucket = self._flashes.get(prefix)
        if not bucket:
            return
        while bucket and bucket[0]["received"] < cutoff:
            bucket.popleft()

    # ------------------------------------------------------------------
    # State the entities read
    # ------------------------------------------------------------------

    def alerts_for(self, same: str) -> list[dict[str, Any]]:
        """Active alerts for a county, most severe first.

        Within one county each hazard occupies exactly one topic, so no
        dedup is needed here; alerts spanning several counties appear once
        per county tree, which is correct for per-county entities.
        """
        alerts = [
            alert
            for alert in self._alerts.get(same, {}).values()
            if alert.get("status", "active") == "active"
        ]

        def severity_rank(alert: dict[str, Any]) -> int:
            severity = alert.get("severity") or ""
            return (
                SEVERITY_ORDER.index(severity)
                if severity in SEVERITY_ORDER
                else len(SEVERITY_ORDER)
            )

        alerts.sort(key=severity_rank)
        return alerts

    def alert_topics_for(self, same: str) -> dict[str, dict[str, Any]]:
        """Active alerts keyed by their full topic (stable hazard identity)."""
        return {
            topic: alert
            for topic, alert in self._alerts.get(same, {}).items()
            if alert.get("status", "active") == "active"
        }

    def highest_severity(self, same: str) -> str:
        alerts = self.alerts_for(same)
        if not alerts:
            return SEVERITY_NONE
        severity = alerts[0].get("severity")
        return severity if severity in SEVERITY_ORDER else SEVERITY_NONE

    def has_phenomenon(self, same: str, phenomenon: str) -> bool:
        return any(
            alert.get("phenomena") == phenomenon for alert in self.alerts_for(same)
        )

    def flash_count(self, prefix: str) -> int:
        self._prune_flashes(prefix)
        return len(self._flashes.get(prefix, ()))

    def recent_flashes(self, prefix: str, limit: int = 25) -> list[dict[str, Any]]:
        self._prune_flashes(prefix)
        bucket = self._flashes.get(prefix)
        if not bucket:
            return []
        return list(bucket)[-limit:]

    def last_flash_time(self, prefix: str) -> datetime | None:
        flashes = self.recent_flashes(prefix, limit=1)
        if not flashes:
            return None
        raw = flashes[-1].get("time")
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                pass
        return datetime.fromtimestamp(flashes[-1]["received"], tz=timezone.utc)
