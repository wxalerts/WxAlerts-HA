"""The WxAlerts feed coordinator.

One object owns the single MQTT connection to the wxalerts broker and all
alert, place and storm cell state. Entities subscribe to it through dispatcher
signals; no entity ever holds its own MQTT client.

The MQTT transport lives behind ``FeedClient`` so "where messages come from"
is one swappable object.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import aiohttp
import aiomqtt
from aiomqtt.exceptions import MqttConnectError

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.util import dt as dt_util
from homeassistant.util.ssl import get_default_context

from .const import (
    CONF_PLACES,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_WS_PATH,
    DOMAIN,
    EPISODES_THAT_NOTIFY,
    EVENT_ALERT,
    EVENT_HAZARD,
    EVENT_STORM_CELL,
    HAZARD_BEGIN,
    HAZARD_END,
    MAX_SUBSCRIPTIONS,
    PLACE_ID,
    PLACE_LAT,
    PLACE_LON,
    PLACE_NAME,
    PLACE_SAME,
    PRICING_URL,
    REASON_OVER_LIMIT,
    REASON_PLAN_LAPSED,
    ISSUE_OVER_LIMIT,
    ISSUE_PLAN_LAPSED,
    ISSUE_REVOKED,
    HEARTBEAT_SECONDS,
    RELOGIN_COOLDOWN_SECONDS,
    RELOGIN_RETRY_SECONDS,
    RUNG_OPTIONS,
    SEVERITY_NONE,
    SEVERITY_ORDER,
    SIGNAL_ALERTS_UPDATED,
    SIGNAL_CONNECTION,
    SIGNAL_PLACE_UPDATED,
    SIGNAL_STORMS_UPDATED,
    SIGNAL_TICK,
    SIGNIFICANCE_ORDER,
    STATUS_ONLINE,
    STATUS_STALE_SECONDS,
    STATUS_UPDATED_AT,
    STORM_EPISODE,
    STORM_ETA_MIN,
    STORM_EXPIRE_SECONDS,
    STORM_FIELDS,
    STORM_ID,
    STORM_PLACE_ID,
    STORM_PLACE_NAME,
    STORM_RUNG,
    STORM_UPDATED_AT,
    THREAT_NONE,
    TICK_SECONDS,
    TOPIC_ALERT_ROOT,
    TOPIC_PLACES,
    TOPIC_SAME_FMT,
    TOPIC_STATUS,
    TOPIC_STORMS_PLACE,
    TOPIC_USER_PREFIX_FMT,
    TOPIC_USER_WILDCARD_FMT,
)
from .events import event_key_for, event_name_for
from .geo import NWS_POINTS_URL, normalize_same, same_from_county_url

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff: the broker rate-limits new connections, and every fresh
# subscription replays the full retained set, so never hot-loop.
_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 300.0
_BACKOFF_RESET_AFTER = 120.0  # connection considered stable after this

# Subscribing replays every live hazard and storm cell as a retained message.
# That burst is current state, not news: without this window every HA restart
# would re-announce every warning in effect. Messages arriving within it update
# entities but fire no event.
_PRIMING_SECONDS = 5.0

_USER_AGENT = "WxAlerts-HA (github.com/wxalerts/WxAlerts-HA)"


def _reason_value(code: Any) -> int | None:
    value = getattr(code, "value", code)
    return value if isinstance(value, int) else None


def is_auth_error(err: BaseException) -> bool:
    """True when a broker error is a credentials problem, not a network one.

    MQTT 5 reason codes 134 and 135 mean bad credentials or not authorised,
    whether in the CONNACK or in a disconnect that kicks a revoked login.
    The 3.1.1 return codes 4 and 5 mean the same only on a refused connect.
    """
    if not isinstance(err, aiomqtt.MqttCodeError):
        return False
    value = _reason_value(err.rc)
    if value in (134, 135):
        return True
    return isinstance(err, MqttConnectError) and value in (4, 5)


def _denied(codes: Any) -> bool:
    """True if a SUBACK refused any topic (MQTT 5 reason codes >= 0x80)."""
    for code in codes or ():
        value = _reason_value(code)
        if value is not None and value >= 0x80:
            return True
    return False


def _parse_time(raw: Any) -> float | None:
    """Epoch seconds from an ISO timestamp or a number; None if unusable."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        parsed = dt_util.parse_datetime(raw)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)
        return parsed.timestamp()
    return None


def _as_float(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _alert_rank(alert: dict[str, Any]) -> tuple[int, int]:
    """Sort key: most dangerous first.

    Severity alone is not enough to order a list a person reads top-down. NWS
    assigns it per product type, so a Tornado Watch and a Severe Thunderstorm
    Warning are both "Severe". Significance breaks the tie the way a
    forecaster would: a warning outranks a watch outranks an advisory.
    """
    severity = alert.get("severity") or ""
    significance = alert.get("significance") or ""
    return (
        SEVERITY_ORDER.index(severity)
        if severity in SEVERITY_ORDER
        else len(SEVERITY_ORDER),
        SIGNIFICANCE_ORDER.index(significance)
        if significance in SIGNIFICANCE_ORDER
        else len(SIGNIFICANCE_ORDER),
    )


def threat_for_rung(rung: Any) -> str:
    """The threat sensor's option for a rung.

    A rung the table does not know maps down to the nearest known rung below
    it, so a new tier on the server reads as its closest neighbour instead of
    an invalid enum state.
    """
    value = _as_float(rung)
    if value is None:
        return THREAT_NONE
    known = [r for r in RUNG_OPTIONS if r <= value]
    if not known:
        return THREAT_NONE
    return RUNG_OPTIONS[max(known)]


# ===========================================================================
# Places
# ===========================================================================
@dataclass(frozen=True)
class Place:
    """One of the person's WxAlerts places."""

    id: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    same: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The wire shape, which is also what the entry caches."""
        return {
            PLACE_ID: self.id,
            PLACE_NAME: self.name,
            PLACE_LAT: self.latitude,
            PLACE_LON: self.longitude,
            PLACE_SAME: self.same,
        }

    @property
    def coord_key(self) -> tuple[float, float] | None:
        if self.latitude is None or self.longitude is None:
            return None
        return (round(self.latitude, 4), round(self.longitude, 4))


def parse_places(raw: Any) -> dict[str, Place] | None:
    """Places keyed by id from the ``places`` payload, or None if malformed.

    A malformed list is ignored rather than read as "no places": taking it
    literally would delete every device the person has.
    """
    if not isinstance(raw, list):
        return None
    places: dict[str, Place] = {}
    for item in raw:
        if not isinstance(item, dict) or item.get(PLACE_ID) in (None, ""):
            continue
        place_id = str(item[PLACE_ID])
        places[place_id] = Place(
            id=place_id,
            name=str(item.get(PLACE_NAME) or place_id),
            latitude=_as_float(item.get(PLACE_LAT)),
            longitude=_as_float(item.get(PLACE_LON)),
            same=normalize_same(item.get(PLACE_SAME)),
        )
    return places


def place_device_identifier(entry_id: str, place_id: str) -> tuple[str, str]:
    """Device registry identifier for a place."""
    return (DOMAIN, f"{entry_id}_place_{place_id}")


def service_device_identifier(entry_id: str) -> tuple[str, str]:
    """Device registry identifier for the account-wide feed device."""
    return (DOMAIN, f"{entry_id}_service")


# ===========================================================================
# Transport
# ===========================================================================
@dataclass
class FeedConfig:
    """Everything FeedClient needs to reach a broker."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    ws_path: str = DEFAULT_WS_PATH
    username: str = ""
    password: str = ""
    client_id: str = ""
    subscriptions: list[tuple[str, int]] = field(default_factory=list)
    # Building an SSL context reads the CA bundle off disk. Hand one in so
    # that never happens on the event loop; FeedClient builds its own in a
    # thread if this is None, so it stays usable outside Home Assistant.
    tls_context: ssl.SSLContext | None = None


def _client_kwargs(config: FeedConfig, tls_context: ssl.SSLContext) -> dict[str, Any]:
    return {
        "hostname": config.host,
        "port": config.port,
        "username": config.username,
        "password": config.password,
        "identifier": config.client_id,
        "protocol": aiomqtt.ProtocolVersion.V5,
        "transport": "websockets",
        "websocket_path": config.ws_path,
        "tls_context": tls_context,
        "keepalive": 60,
        "clean_start": True,
    }


class FeedClient:
    """Owns the MQTT connection and pushes every message to one handler.

    Knows nothing about alerts or storms; swap this out to read from a local
    broker and the rest of the integration is unchanged.
    """

    def __init__(
        self,
        config: FeedConfig,
        on_message: Callable[[str, bytes], Awaitable[None]],
        on_connection: Callable[[bool], None],
        on_auth_failed: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._on_connection = on_connection
        self._on_auth_failed = on_auth_failed
        self._task: asyncio.Task | None = None
        self._stopping = False
        # The live client, so subscriptions can follow the places list without
        # tearing the session down. Reconnecting to change one topic would
        # replay every retained hazard and re-prime the event window.
        self._client: aiomqtt.Client | None = None

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def set_subscriptions(self, subscriptions: list[tuple[str, int]]) -> None:
        """Move the live session onto a new topic set.

        Subscribes to what is new before unsubscribing from what is gone, so a
        county that stays covered never has a gap. If the session is down this
        only records the new set; the next connect uses it.
        """
        previous = {topic for topic, _ in self._config.subscriptions}
        wanted = {topic for topic, _ in subscriptions}
        self._config.subscriptions = subscriptions

        client = self._client
        if client is None:
            return
        try:
            for topic, qos in subscriptions:
                if topic not in previous:
                    await client.subscribe(topic, qos=qos)
            for topic in previous - wanted:
                await client.unsubscribe(topic)
        except aiomqtt.MqttError as err:
            # The reconnect path re-subscribes from the stored set, so a failure
            # here costs a round trip, not correctness.
            _LOGGER.debug("Live subscription update failed: %s", err)

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
                    **_client_kwargs(self._config, tls_context)
                ) as client:
                    connected_at = time.monotonic()
                    self._client = client
                    self._on_connection(True)
                    for topic, qos in list(self._config.subscriptions):
                        codes = await client.subscribe(topic, qos=qos)
                        if _denied(codes):
                            _LOGGER.warning(
                                "The broker refused the subscription to %s", topic
                            )
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
                if is_auth_error(err):
                    # Retrying a rejected password only gets the client banned.
                    # Stop, and let Home Assistant ask for a new one.
                    _LOGGER.error(
                        "The WxAlerts broker rejected the login (%s); "
                        "re-authentication is needed",
                        err,
                    )
                    self._stopping = True
                    if self._on_auth_failed is not None:
                        self._on_auth_failed()
                else:
                    _LOGGER.warning("MQTT connection lost: %s", err)
            except Exception:  # noqa: BLE001 - never let the loop die silently
                _LOGGER.exception("Unexpected error in feed loop")
            finally:
                self._client = None
                self._on_connection(False)
            if self._stopping:
                return
            if connected_at and time.monotonic() - connected_at > _BACKOFF_RESET_AFTER:
                backoff = _BACKOFF_INITIAL
            _LOGGER.debug("Reconnecting in %.0f s", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


# ===========================================================================
# Coordinator
# ===========================================================================
class WxAlertsCoordinator:
    """Holds alert, place and storm cell state for one account."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.connected = False

        self.username: str = entry.data.get(CONF_USERNAME, "")
        self._password: str = entry.data.get(CONF_PASSWORD, "")
        self.topic_prefix = TOPIC_USER_PREFIX_FMT.format(username=self.username)
        # What the update listener compares against, so caching the places
        # list in entry data does not reload the entry on every change.
        self.options_snapshot: dict[str, Any] = dict(entry.options)

        self.places: dict[str, Place] = parse_places(entry.data.get(CONF_PLACES)) or {}
        # True once the broker has delivered a places list this run, as
        # opposed to the cached copy from the last run.
        self.places_received = False

        # Alert state, keyed county -> full topic -> payload. The ETN is the
        # last topic level, so two simultaneous hazards for one county keep
        # distinct keys and a second tornado warning never clobbers the first.
        self._alerts: dict[str, dict[str, dict[str, Any]]] = {}
        # Topics we have already fired the HA event for (events fire per new
        # hazard, not per update; CON/EXT on the same topic stays quiet).
        self._event_fired: set[str] = set()
        self._priming_until: float = 0.0
        # Which event types are currently in effect per county. Transitions of
        # this set, not of individual alerts, are what a "Tornado Warning
        # begins" trigger means.
        self._active_events: dict[str, set[str]] = {}

        # Storm cells, keyed place id -> storm id -> payload.
        self._storms: dict[str, dict[str, dict[str, Any]]] = {}
        # When each storm was last heard from, for expiry when updated_at is
        # missing or unparseable.
        self._storm_seen: dict[tuple[str, str], float] = {}
        # The (episode, rung) last announced per storm and place. A retained
        # replay carries the same pair and stays quiet; a real escalation
        # carries a new rung and fires.
        self._storm_announced: dict[tuple[str, str], tuple[Any, Any]] = {}

        # Feed heartbeat from the status topic.
        self._status_online: bool | None = None
        self._status_at: float | None = None
        self._status_received_at: float | None = None

        # SAME codes looked up from api.weather.gov for places that arrived
        # without one, keyed by rounded coordinates.
        self._resolved_same: dict[tuple[float, float], str] = {}
        self._same_attempted: set[tuple[float, float]] = set()

        self._place_listeners: list[Callable[[list[Place]], None]] = []
        self._client: FeedClient | None = None
        self._unsub_tick: Callable[[], None] | None = None
        self._unsub_heartbeat: Callable[[], None] | None = None
        # Set when the account reports this instance inactive.
        self.deactivated_reason: str | None = None

    # ===========================================================================
    # Lifecycle
    # ===========================================================================
    @property
    def same_codes(self) -> list[str]:
        seen: list[str] = []
        for place in self.places.values():
            if place.same and place.same not in seen:
                seen.append(place.same)
        return seen

    def place(self, place_id: str) -> Place | None:
        return self.places.get(place_id)

    @callback
    def add_place_listener(
        self, listener: Callable[[list[Place]], None]
    ) -> Callable[[], None]:
        """Be told about places that appear after setup."""
        self._place_listeners.append(listener)

        def _remove() -> None:
            if listener in self._place_listeners:
                self._place_listeners.remove(listener)

        return _remove

    def _build_subscriptions(self) -> list[tuple[str, int]]:
        subs: list[tuple[str, int]] = [
            (TOPIC_USER_WILDCARD_FMT.format(username=self.username), 1)
        ]
        for code in self.same_codes:
            subs.append((TOPIC_SAME_FMT.format(same=code), 1))
        if len(subs) > MAX_SUBSCRIPTIONS:
            # The broker caps subscriptions per client at 20. Keep the account
            # topics, trim counties, and say so instead of failing silently.
            _LOGGER.warning(
                "Your places span %d counties but the broker allows %d "
                "subscriptions; alerts for the last %d counties are not "
                "monitored",
                len(subs) - 1,
                MAX_SUBSCRIPTIONS,
                len(subs) - MAX_SUBSCRIPTIONS,
            )
            subs = subs[:MAX_SUBSCRIPTIONS]
        return subs

    async def async_start(self) -> None:
        self._unsub_tick = async_track_time_interval(
            self.hass, self._tick, timedelta(seconds=TICK_SECONDS)
        )
        instance_id = await async_get_instance_id(self.hass)
        config = FeedConfig(
            username=self.username,
            password=self._password,
            client_id=f"wxha-{instance_id[:12]}-{self.entry.entry_id[:8]}",
            subscriptions=self._build_subscriptions(),
            # Home Assistant pre-warms this at import, so it costs nothing
            # here and never touches the disk from the event loop.
            tls_context=get_default_context(),
        )
        self._client = FeedClient(
            config,
            self._handle_message,
            self._handle_connection,
            self._handle_auth_failed,
        )
        self._client.start()
        self._schedule_same_lookups()
        self._unsub_heartbeat = async_track_time_interval(
            self.hass, self._async_heartbeat, timedelta(seconds=HEARTBEAT_SECONDS)
        )
        self.entry.async_create_background_task(
            self.hass, self._async_heartbeat(), "wxalerts_heartbeat"
        )

    async def async_stop(self) -> None:
        if self._unsub_heartbeat:
            self._unsub_heartbeat()
            self._unsub_heartbeat = None
        if self._unsub_tick:
            self._unsub_tick()
            self._unsub_tick = None
        if self._client:
            await self._client.stop()
            self._client = None

    @property
    def subscriptions(self) -> list[tuple[str, int]]:
        """What the feed is subscribed to, for diagnostics."""
        return self._build_subscriptions()

    # ===========================================================================
    # Connection
    # ===========================================================================
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

    @callback
    def _handle_auth_failed(self) -> None:
        """The broker rejected the login: get a new one, or ask to sign in.

        A password rotated or revoked on the website, or one replaced by a
        re-registration elsewhere, is recovered by registering this instance
        again with the stored token. Only when that fails does Home Assistant
        ask the person to sign in again.
        """
        self.entry.async_create_background_task(
            self.hass, self._async_recover_login(), "wxalerts_relogin"
        )

    async def _async_recover_login(self) -> None:
        # Imported here: api imports the HA OAuth helpers, which the
        # transport tests have no need to load.
        from .api import (
            ApiAuthError,
            ApiUnavailable,
            InstanceDeactivated,
            InstanceLimit,
            SupporterRequired,
            async_refresh_login,
        )

        last = self.hass.data.setdefault(DOMAIN, {}).get(self.entry.entry_id, 0.0)
        if time.time() - last < RELOGIN_COOLDOWN_SECONDS:
            # A login fetched minutes ago was rejected too. Looping on
            # re-registration would only rotate passwords; ask instead.
            self.entry.async_start_reauth(self.hass)
            return
        try:
            await async_refresh_login(self.hass, self.entry)
        except InstanceDeactivated as err:
            self._handle_deactivated(err.reason)
            return
        except ApiUnavailable as err:
            _LOGGER.warning(
                "Could not reach wxalerts.org to renew the broker login (%s); "
                "retrying in %d s",
                err,
                RELOGIN_RETRY_SECONDS,
            )
            self.entry.async_on_unload(
                async_call_later(
                    self.hass, RELOGIN_RETRY_SECONDS, self._retry_recover
                )
            )
            return
        except (ApiAuthError, SupporterRequired, InstanceLimit) as err:
            _LOGGER.warning("WxAlerts needs you to sign in again: %s", err)
            self.entry.async_start_reauth(self.hass)
            return
        self.hass.data[DOMAIN][self.entry.entry_id] = time.time()
        _LOGGER.info("Renewed the WxAlerts broker login; reconnecting")
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    @callback
    def _handle_deactivated(self, reason: str) -> None:
        """The account says this instance is off. Never re-register over it.

        A revoke is the person's decision, so it is reported and the sign-in
        is offered, but nothing happens until they act. Over the plan's limit
        or a lapsed plan is reported with the way to upgrade; the heartbeat
        notices when the instance is active again.
        """
        self.deactivated_reason = reason
        if reason in (REASON_OVER_LIMIT, REASON_PLAN_LAPSED):
            issue = ISSUE_OVER_LIMIT if reason == REASON_OVER_LIMIT else ISSUE_PLAN_LAPSED
            _LOGGER.warning(
                "This Home Assistant is outside your WxAlerts plan (%s)", reason
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=issue,
                translation_placeholders={"pricing_url": PRICING_URL},
                learn_more_url=PRICING_URL,
            )
            return
        _LOGGER.warning("This Home Assistant was removed from your WxAlerts account")
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_REVOKED,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_REVOKED,
        )
        self.entry.async_start_reauth(self.hass)

    async def _async_heartbeat(self, _now: Any = None) -> None:
        """Tell the account this instance is alive, and read its standing.

        Runs at setup and every few hours. It is the website's only "last
        seen", and the way a revoke or a plan change reaches Home Assistant
        when the broker has not said anything yet.
        """
        from .api import ApiError, InstanceDeactivated, async_send_heartbeat

        try:
            await async_send_heartbeat(self.hass, self.entry)
        except InstanceDeactivated as err:
            self._handle_deactivated(err.reason)
            return
        except ApiError as err:
            _LOGGER.debug("WxAlerts heartbeat failed: %s", err)
            return
        for issue in (ISSUE_REVOKED, ISSUE_OVER_LIMIT, ISSUE_PLAN_LAPSED):
            ir.async_delete_issue(self.hass, DOMAIN, issue)
        if self.deactivated_reason is not None:
            # Active again (upgraded, or a slot freed): fetch a login.
            self.deactivated_reason = None
            if not self.connected:
                self._handle_auth_failed()

    @callback
    def _retry_recover(self, _now: Any) -> None:
        self._handle_auth_failed()

    @callback
    def _tick(self, _now: Any = None) -> None:
        """Re-evaluate time-based state with no messages arriving.

        Feed age, the ETA countdown and staleness all move with the clock, so
        without this they would freeze at whatever the last message said.
        """
        self._expire_storms()
        async_dispatcher_send(self.hass, f"{SIGNAL_TICK}_{self.entry.entry_id}")

    # ===========================================================================
    # Message routing
    # ===========================================================================
    async def _handle_message(self, topic: str, payload: bytes) -> None:
        if topic.startswith(TOPIC_ALERT_ROOT):
            parts = topic.split("/")
            if len(parts) >= 6:
                self._handle_alert(topic, parts[4], payload)
            return
        if not topic.startswith(self.topic_prefix):
            return
        rest = topic[len(self.topic_prefix):]
        if rest == TOPIC_PLACES:
            self._handle_places(payload)
        elif rest == TOPIC_STATUS:
            self._handle_status(payload)
        elif rest.startswith(TOPIC_STORMS_PLACE + "/"):
            tail = rest[len(TOPIC_STORMS_PLACE) + 1:].split("/")
            if len(tail) == 2 and all(tail):
                self._handle_storm(tail[0], tail[1], payload)

    # ===========================================================================
    # Places
    # ===========================================================================
    def _handle_places(self, payload: bytes) -> None:
        if not payload:
            # A cleared retained message says nothing about the places
            # themselves; keep what we have.
            return
        try:
            decoded = json.loads(payload)
        except ValueError:
            _LOGGER.warning("Undecodable places payload")
            return
        places = parse_places(decoded)
        if places is None:
            _LOGGER.warning("Places payload is not a list; ignoring it")
            return
        self.places_received = True
        self._apply_places(places)

    def _with_resolved_same(self, place: Place) -> Place:
        if place.same or place.coord_key is None:
            return place
        resolved = self._resolved_same.get(place.coord_key)
        if resolved is None:
            return place
        return Place(place.id, place.name, place.latitude, place.longitude, resolved)

    @callback
    def _apply_places(self, incoming: dict[str, Place]) -> None:
        new = {pid: self._with_resolved_same(p) for pid, p in incoming.items()}
        old = self.places
        old_same = set(self.same_codes)

        added = [place for pid, place in new.items() if pid not in old]
        removed = [pid for pid in old if pid not in new]
        changed = [place for pid, place in new.items() if pid in old and old[pid] != place]
        self.places = new

        device_registry = dr.async_get(self.hass)
        entry_id = self.entry.entry_id
        for place_id in removed:
            self._forget_place_storms(place_id)
            device = device_registry.async_get_device(
                identifiers={place_device_identifier(entry_id, place_id)}
            )
            if device is not None:
                device_registry.async_update_device(
                    device.id, remove_config_entry_id=entry_id
                )
        for place in changed:
            if old[place.id].name != place.name:
                device = device_registry.async_get_device(
                    identifiers={place_device_identifier(entry_id, place.id)}
                )
                if device is not None:
                    device_registry.async_update_device(
                        device.id, name=f"WxAlerts {place.name}"
                    )
            async_dispatcher_send(
                self.hass, f"{SIGNAL_PLACE_UPDATED}_{entry_id}_{place.id}"
            )
        if added:
            for listener in list(self._place_listeners):
                listener(added)

        new_same = set(self.same_codes)
        for same in old_same - new_same:
            # No place is in this county any more and its tree is about to be
            # unsubscribed, so no tombstone will ever arrive for what it holds.
            if self._alerts.pop(same, None):
                self._event_fired = {
                    topic
                    for topic in self._event_fired
                    if not topic.startswith(f"{TOPIC_ALERT_ROOT}{same}/")
                }
                self._dispatch_alerts(same)
        if new_same != old_same and self._client is not None:
            self.hass.async_create_task(
                self._client.set_subscriptions(self._build_subscriptions())
            )

        cached = [place.as_dict() for place in new.values()]
        if cached != self.entry.data.get(CONF_PLACES):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_PLACES: cached}
            )

        self._schedule_same_lookups()

    def _schedule_same_lookups(self) -> None:
        for place in self.places.values():
            key = place.coord_key
            if place.same or key is None or key in self._same_attempted:
                continue
            self._same_attempted.add(key)
            self.hass.async_create_task(self._async_resolve_same(place))

    async def _async_resolve_same(self, place: Place) -> None:
        """Look up a county for a place that arrived without one.

        Without a SAME code a place gets storm cells but no NWS alerts, so one
        request to api.weather.gov is worth making. Failing it costs the alerts
        for that place, never the setup.
        """
        session = async_get_clientsession(self.hass)
        url = NWS_POINTS_URL.format(
            lat=round(place.latitude, 4), lon=round(place.longitude, 4)
        )
        try:
            async with session.get(
                url,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/geo+json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status != 200:
                    _LOGGER.info(
                        "No county for place %s (api.weather.gov HTTP %s); it "
                        "gets storm cells but no NWS alerts",
                        place.name,
                        response.status,
                    )
                    return
                body = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.info("Could not look up a county for place %s: %s", place.name, err)
            return
        county_url = (body.get("properties") or {}).get("county")
        same = same_from_county_url(county_url) if county_url else None
        if not same or place.coord_key is None:
            return
        self._resolved_same[place.coord_key] = same
        current = self.places.get(place.id)
        if current is not None and not current.same:
            self._apply_places(dict(self.places))

    # ===========================================================================
    # Status heartbeat
    # ===========================================================================
    def _handle_status(self, payload: bytes) -> None:
        now = time.time()
        if not payload:
            self._status_online = None
            self._status_at = None
            self._status_received_at = None
        else:
            try:
                status = json.loads(payload)
            except ValueError:
                _LOGGER.warning("Undecodable status payload")
                return
            if not isinstance(status, dict):
                return
            self._status_online = status.get(STATUS_ONLINE) is not False
            # The heartbeat is retained, so its own timestamp is what tells a
            # live feed from a frozen message left by a server that died. A
            # clock running ahead of ours is clamped to now, never negative.
            written = _parse_time(status.get(STATUS_UPDATED_AT))
            self._status_at = min(written, now) if written is not None else now
            self._status_received_at = now
        async_dispatcher_send(self.hass, f"{SIGNAL_TICK}_{self.entry.entry_id}")

    @property
    def feed_age(self) -> float | None:
        """Seconds since the server's heartbeat was written; None if unheard."""
        if self._status_at is None:
            return None
        return max(0.0, time.time() - self._status_at)

    @property
    def feed_online(self) -> bool | None:
        return self._status_online

    @property
    def storms_available(self) -> bool:
        """Whether a storm cell reading can be trusted.

        A stale or offline feed reports unavailable rather than "no storm".
        A confident "off" during an outage looks exactly like calm weather.
        """
        if not self.connected:
            return False
        if self._status_online is False:
            return False
        age = self.feed_age
        if age is None:
            # No heartbeat heard yet: trust the connection.
            return True
        return age <= STATUS_STALE_SECONDS

    # ===========================================================================
    # Storm cells
    # ===========================================================================
    def _forget_place_storms(self, place_id: str) -> None:
        for storm_id in list(self._storms.pop(place_id, {})):
            self._storm_seen.pop((place_id, storm_id), None)
            self._storm_announced.pop((place_id, storm_id), None)

    def _dispatch_storms(self, place_id: str) -> None:
        async_dispatcher_send(
            self.hass, f"{SIGNAL_STORMS_UPDATED}_{self.entry.entry_id}_{place_id}"
        )

    def _handle_storm(self, place_id: str, storm_id: str, payload: bytes) -> None:
        storms = self._storms.setdefault(place_id, {})
        key = (place_id, storm_id)

        if not payload:
            # Cleared: the cell no longer threatens this place.
            self._storm_announced.pop(key, None)
            self._storm_seen.pop(key, None)
            if storms.pop(storm_id, None) is not None:
                self._dispatch_storms(place_id)
            return

        try:
            storm = json.loads(payload)
        except ValueError:
            _LOGGER.warning("Undecodable storm cell payload for place %s", place_id)
            return
        if not isinstance(storm, dict):
            return

        now = time.time()
        updated = _parse_time(storm.get(STORM_UPDATED_AT))
        if updated is not None and now - updated > STORM_EXPIRE_SECONDS:
            # A retained message the server never cleared. Reporting it would
            # announce a storm that passed long ago.
            _LOGGER.debug("Ignoring stale storm cell %s for %s", storm_id, place_id)
            if storms.pop(storm_id, None) is not None:
                self._dispatch_storms(place_id)
            return

        storm = {**storm, STORM_ID: storm.get(STORM_ID) or storm_id}
        storms[storm_id] = storm
        self._storm_seen[key] = now
        self._dispatch_storms(place_id)

        episode = storm.get(STORM_EPISODE)
        if episode not in EPISODES_THAT_NOTIFY:
            return
        signature = (episode, storm.get(STORM_RUNG))
        if self._storm_announced.get(key) == signature:
            return
        self._storm_announced[key] = signature
        if time.monotonic() < self._priming_until:
            _LOGGER.debug("Retained storm cell %s; not firing an event", storm_id)
            return
        place = self.places.get(place_id)
        data = {name: storm.get(name) for name in STORM_FIELDS}
        data[STORM_PLACE_ID] = place_id
        data[STORM_PLACE_NAME] = storm.get(STORM_PLACE_NAME) or (
            place.name if place else None
        )
        data[STORM_ETA_MIN] = self.storm_eta(storm)
        data["threat"] = threat_for_rung(storm.get(STORM_RUNG))
        self.hass.bus.async_fire(EVENT_STORM_CELL, data)

    def _storm_reference_time(self, place_id: str, storm: dict[str, Any]) -> float:
        updated = _parse_time(storm.get(STORM_UPDATED_AT))
        if updated is not None:
            return min(updated, time.time())
        return self._storm_seen.get((place_id, str(storm.get(STORM_ID))), time.time())

    def _expire_storms(self) -> None:
        now = time.time()
        for place_id, storms in list(self._storms.items()):
            stale = [
                storm_id
                for storm_id, storm in storms.items()
                if now - self._storm_reference_time(place_id, storm)
                > STORM_EXPIRE_SECONDS
            ]
            for storm_id in stale:
                _LOGGER.debug("Storm cell %s for %s expired", storm_id, place_id)
                storms.pop(storm_id, None)
                self._storm_seen.pop((place_id, storm_id), None)
                self._storm_announced.pop((place_id, storm_id), None)
            if stale:
                self._dispatch_storms(place_id)

    def storm_eta(self, storm: dict[str, Any] | None) -> float | None:
        """Minutes until arrival, counted down from when the server wrote it.

        The server publishes on its own cadence; between messages the arrival
        keeps getting closer, and a sensor that held the published number would
        be wrong by however long it has been since.
        """
        if not storm:
            return None
        eta = _as_float(storm.get(STORM_ETA_MIN))
        if eta is None:
            return None
        place_id = str(storm.get(STORM_PLACE_ID) or "")
        elapsed = (time.time() - self._storm_reference_time(place_id, storm)) / 60.0
        return round(max(0.0, eta - max(0.0, elapsed)), 1)

    def storms_for(self, place_id: str) -> list[dict[str, Any]]:
        """Storm cells threatening a place, most threatening first.

        Highest rung first, then soonest arrival: a tornado rung twenty minutes
        out matters more than a thunderstorm rung five minutes out.
        """
        storms = list(self._storms.get(place_id, {}).values())

        def _key(storm: dict[str, Any]) -> tuple[float, float]:
            rung = _as_float(storm.get(STORM_RUNG)) or 0.0
            eta = _as_float(storm.get(STORM_ETA_MIN))
            return (-rung, eta if eta is not None else float("inf"))

        storms.sort(key=_key)
        return storms

    def primary_storm(self, place_id: str) -> dict[str, Any] | None:
        storms = self.storms_for(place_id)
        return storms[0] if storms else None

    # ===========================================================================
    # Alerts
    # ===========================================================================
    def places_for_same(self, same: str) -> list[Place]:
        return [place for place in self.places.values() if place.same == same]

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
        if not isinstance(alert, dict):
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
            for place in self.places_for_same(same):
                self.hass.bus.async_fire(
                    EVENT_ALERT,
                    {
                        "place_id": place.id,
                        "place_name": place.name,
                        "same": same,
                        "topic": topic,
                        "id": alert.get("id"),
                        "vtec": alert.get("vtec"),
                        "event": alert.get("event"),
                        "event_type": event_key_for(alert),
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
            self.hass, f"{SIGNAL_ALERTS_UPDATED}_{self.entry.entry_id}", same
        )
        self._fire_hazard_transitions(same)

    def _fire_hazard_transitions(self, same: str) -> None:
        """Announce event types that just started or stopped being in effect.

        Entities are written first, by the dispatcher call above, so an
        automation woken by one of these events reads current entity state.

        A begin during the retained-replay window is suppressed for the same
        reason EVENT_ALERT suppresses one. Ends are never suppressed: the set
        starts empty on a restart and grows, so priming cannot produce a
        spurious end, and a real expiry landing in those seconds is worth
        delivering.
        """
        previous = self._active_events.get(same, set())
        current = {event_key_for(alert) for alert in self.alerts_for(same)}
        if current == previous:
            return
        self._active_events[same] = current

        priming = time.monotonic() < self._priming_until
        for key in current - previous:
            if priming:
                _LOGGER.debug("Retained hazard %s on %s; not firing", key, same)
                continue
            self._fire_hazard(same, key, HAZARD_BEGIN)
        for key in previous - current:
            self._fire_hazard(same, key, HAZARD_END)

    def _fire_hazard(self, same: str, key: str, action: str) -> None:
        # On an end there is no alert left to describe, so the payload carries
        # what the transition is about and nothing only a live product has.
        alert = next(
            (alert for alert in self.alerts_for(same) if event_key_for(alert) == key),
            None,
        )
        for place in self.places_for_same(same):
            self.hass.bus.async_fire(
                EVENT_HAZARD,
                {
                    "place_id": place.id,
                    "place_name": place.name,
                    "same": same,
                    "event_type": key,
                    "event": event_name_for(alert) if alert else key,
                    "action": action,
                    "severity": alert.get("severity") if alert else None,
                    "headline": alert.get("headline") if alert else None,
                    "ends": alert.get("ends") if alert else None,
                    "vtec": alert.get("vtec") if alert else None,
                },
            )

    # ===========================================================================
    # Alert state the entities read
    # ===========================================================================
    def alerts_for(self, same: str | None) -> list[dict[str, Any]]:
        """Active alerts for a county, most severe first."""
        if not same:
            return []
        alerts = [
            alert
            for alert in self._alerts.get(same, {}).values()
            if alert.get("status", "active") == "active"
        ]
        alerts.sort(key=_alert_rank)
        return alerts

    def alert_topics_for(self, same: str) -> dict[str, dict[str, Any]]:
        """Active alerts keyed by their full topic (stable hazard identity)."""
        return {
            topic: alert
            for topic, alert in self._alerts.get(same, {}).items()
            if alert.get("status", "active") == "active"
        }

    def highest_severity(self, same: str | None) -> str:
        alerts = self.alerts_for(same)
        if not alerts:
            return SEVERITY_NONE
        severity = alerts[0].get("severity")
        return severity if severity in SEVERITY_ORDER else SEVERITY_NONE

    def active_event_keys(self, same: str | None) -> set[str]:
        """Catalogue keys in effect for a county right now."""
        return {event_key_for(alert) for alert in self.alerts_for(same)}

    def alerts_for_event(self, same: str | None, key: str) -> list[dict[str, Any]]:
        """The live products behind one event type, most urgent first."""
        return [
            alert for alert in self.alerts_for(same) if event_key_for(alert) == key
        ]

    def has_event(self, same: str | None, key: str) -> bool:
        return any(event_key_for(alert) == key for alert in self.alerts_for(same))

    # ===========================================================================
    # Diagnostics
    # ===========================================================================
    def diagnostics(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "places_received": self.places_received,
            "place_count": len(self.places),
            "places": [
                {
                    "id": place.id,
                    "name": place.name,
                    "has_coordinates": place.coord_key is not None,
                    "same": place.same,
                }
                for place in self.places.values()
            ],
            "subscription_count": len(self._build_subscriptions()),
            "counties": self.same_codes,
            "alerts_per_county": {
                same: len(self.alerts_for(same)) for same in self._alerts
            },
            "storms_per_place": {
                place_id: [
                    {name: storm.get(name) for name in STORM_FIELDS}
                    for storm in self.storms_for(place_id)
                ]
                for place_id in self._storms
                if self._storms[place_id]
            },
            "feed_online": self._status_online,
            "feed_age_seconds": None
            if self.feed_age is None
            else round(self.feed_age),
            "storms_available": self.storms_available,
            "deactivated_reason": self.deactivated_reason,
        }
