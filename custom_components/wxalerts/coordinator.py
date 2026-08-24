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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp
import aiomqtt

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.util.ssl import get_default_context

from .const import (
    BACKFILL_PATH,
    BACKFILL_TIMEOUT,
    CONF_ENABLE_ALERTS,
    CONF_ENABLE_GLM,
    CONF_GLM_ALERT,
    CONF_GLM_BACKFILL,
    CONF_GLM_PERSONS,
    CONF_GLM_RADIUS,
    CONF_GLM_WINDOW,
    CONF_LOCATIONS,
    DEFAULT_ENABLE_ALERTS,
    DEFAULT_ENABLE_GLM,
    DEFAULT_GLM_ALERT,
    DEFAULT_GLM_BACKFILL,
    DEFAULT_GLM_PERSONS,
    DEFAULT_GLM_RADIUS,
    DEFAULT_GLM_WINDOW,
    DEFAULT_HOST,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DEFAULT_WS_PATH,
    EVENT_ALERT,
    GLM_STALE_SECONDS,
    LOC_LAT,
    LOC_LON,
    LOC_NAME,
    LOC_SAME,
    LOC_ZONE_ENTITY,
    MAX_SUBSCRIPTIONS,
    RING_MILES,
    SEVERITY_NONE,
    SEVERITY_ORDER,
    SIGNAL_ALERTS_UPDATED,
    SIGNAL_CONNECTION,
    SIGNAL_GLM_UPDATED,
    TOPIC_GLM_STATUS,
    TOPIC_SAME_FMT,
)
from .geo import glm_topic_for_geohash, haversine_miles, plan_coverage
from .lightning import (
    Flash,
    FlashStore,
    Metrics,
    Target,
    TrendTracker,
    compute,
    parse_flash_time,
)

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
        # The live client, so subscriptions can follow a moving target without
        # tearing the session down. Reconnecting to change one topic would
        # replay every retained hazard and re-prime the event window.
        self._client: aiomqtt.Client | None = None

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def set_subscriptions(self, subscriptions: list[tuple[str, int]]) -> None:
        """Move the live session onto a new topic set.

        Subscribes to what is new before unsubscribing from what is gone, so a
        target that stays covered never has a gap. If the session is down this
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
                    self._client = client
                    self._on_connection(True)
                    for topic, qos in list(self._config.subscriptions):
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
                self._client = None
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
        self.glm_radius_mi: float = float(
            options.get(CONF_GLM_RADIUS, DEFAULT_GLM_RADIUS)
        )
        self.glm_alert_mi: float = float(options.get(CONF_GLM_ALERT, DEFAULT_GLM_ALERT))
        self.glm_window_minutes: int = int(
            options.get(CONF_GLM_WINDOW, DEFAULT_GLM_WINDOW)
        )
        self.glm_track_persons: bool = bool(
            options.get(CONF_GLM_PERSONS, DEFAULT_GLM_PERSONS)
        )
        self.glm_backfill_url: str = (
            options.get(CONF_GLM_BACKFILL, DEFAULT_GLM_BACKFILL) or ""
        ).strip()
        self.glm_rings: tuple[int, ...] = tuple(
            ring for ring in RING_MILES if ring <= self.glm_radius_mi
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

        # One shared window of flashes; every target measures its own distance
        # against it rather than keeping a private copy of the same storm.
        self._store = FlashStore(self.glm_window_minutes)
        self._trend = TrendTracker()
        self._metrics: dict[str, Metrics] = {}
        self._targets: dict[str, Target] = {}
        self._cells: list[str] = []
        self.glm_precision: int = 0  # chosen by plan_coverage, not configured
        self.glm_dropped_cells: int = 0
        # Feed liveness, from the ingest's retained heartbeat when it publishes
        # one and from flash arrivals otherwise.
        self._glm_heartbeat: float | None = None
        self._glm_last_message: float | None = None
        # Tri-state on purpose. None means the ingest has never said anything,
        # which is the only case the startup grace period applies to; an
        # explicit False is stronger evidence than silence and must not fall
        # through to "assume fine".
        self._glm_feed_ok: bool | None = None
        # Backfill runs once per connection, not once per reconnect storm.
        self._backfill_done: bool = False
        self._backfill_warned: bool = False
        self._unsub_persons: Callable[[], None] | None = None
        # Platforms register here so a person who appears after setup gets
        # entities without a restart.
        self._target_listeners: list[Callable[[list[Target]], None]] = []

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

    # ------------------------------------------------------------------
    # Lightning targets and coverage
    # ------------------------------------------------------------------

    @property
    def targets(self) -> list[Target]:
        """Everywhere proximity is measured, zones first then people."""
        return list(self._targets.values())

    def target(self, key: str) -> Target | None:
        return self._targets.get(key)

    def _zone_targets(self) -> list[Target]:
        targets: list[Target] = []
        for location in self.locations:
            lat = location.get(LOC_LAT)
            lon = location.get(LOC_LON)
            if lat is None or lon is None:
                continue
            key = location.get(LOC_ZONE_ENTITY) or location.get(LOC_NAME) or ""
            if not key:
                continue
            targets.append(
                Target(
                    key=key,
                    name=location.get(LOC_NAME) or key,
                    latitude=float(lat),
                    longitude=float(lon),
                    kind="zone",
                    location=location,
                )
            )
        return targets

    def _person_targets(self) -> list[Target]:
        """Every ``person`` with a current GPS fix.

        Read from the state machine each time rather than from configuration:
        adding someone to Home Assistant is then all that is needed, and someone
        who has no fix is skipped instead of being reported at a stale position.
        """
        if not self.glm_track_persons:
            return []
        targets: list[Target] = []
        for state in self.hass.states.async_all("person"):
            lat = state.attributes.get("latitude")
            lon = state.attributes.get("longitude")
            if lat is None or lon is None:
                continue
            targets.append(
                Target(
                    key=state.entity_id,
                    name=state.attributes.get("friendly_name") or state.entity_id,
                    latitude=float(lat),
                    longitude=float(lon),
                    kind="person",
                )
            )
        return targets

    def prepare(self) -> None:
        """Resolve targets before the platforms are set up.

        Separate from ``async_start`` because the platforms build one entity per
        target and are forwarded before the broker session opens; resolving
        inside the connect path would set them up against an empty list.
        """
        self._refresh_targets()

    @callback
    def add_target_listener(
        self, listener: Callable[[list[Target]], None]
    ) -> Callable[[], None]:
        """Be told about targets that appear after setup."""
        self._target_listeners.append(listener)

        def _remove() -> None:
            if listener in self._target_listeners:
                self._target_listeners.remove(listener)

        return _remove

    def _refresh_targets(self) -> bool:
        """Rebuild the target list. True if the covered cell set changed."""
        targets = {t.key: t for t in self._zone_targets()}
        for person in self._person_targets():
            targets.setdefault(person.key, person)
        added = [t for key, t in targets.items() if key not in self._targets]
        self._targets = targets
        self._trend.forget(set(targets))
        for stale in set(self._metrics) - set(targets):
            del self._metrics[stale]

        if added:
            for listener in list(self._target_listeners):
                listener(added)

        cells = self._plan_cells()
        changed = cells != self._cells
        self._cells = cells
        return changed

    def _plan_cells(self) -> list[str]:
        """The geohash cells the configured radius reaches, for every target.

        The budget is split evenly so one target cannot starve the others, then
        the union is trimmed if it still overruns. Coverage is never narrowed
        silently — a dropped cell is a direction the sensor is blind in.
        """
        if not self.enable_glm or not self._targets:
            self.glm_dropped_cells = 0
            return []

        budget = max(1, MAX_SUBSCRIPTIONS - len(self.same_codes) - 1)
        per_target = max(1, budget // max(1, len(self._targets)))

        ordered: list[str] = []
        precisions: list[int] = []
        for target in self._targets.values():
            precision, cells = plan_coverage(
                target.latitude, target.longitude, self.glm_radius_mi, per_target
            )
            precisions.append(precision)
            for cell in cells:
                if cell not in ordered:
                    ordered.append(cell)
        self.glm_precision = min(precisions) if precisions else 0

        self.glm_dropped_cells = max(0, len(ordered) - budget)
        if self.glm_dropped_cells:
            _LOGGER.warning(
                "Lightning coverage needs %d geohash cells for %d target(s) at "
                "%.0f miles but only %d subscriptions are available; dropping "
                "the %d furthest. Reduce the radius or track fewer targets — "
                "those directions are now unmonitored",
                len(ordered),
                len(self._targets),
                self.glm_radius_mi,
                budget,
                self.glm_dropped_cells,
            )
        return ordered[:budget]

    def _build_subscriptions(self) -> list[tuple[str, int]]:
        subs: list[tuple[str, int]] = []
        if self.enable_alerts:
            for code in self.same_codes:
                subs.append((TOPIC_SAME_FMT.format(same=code), 1))
        if self.enable_glm and self._cells:
            # The heartbeat first: without it a stalled ingest is indis-
            # tinguishable from calm weather, which is the one failure this
            # sensor must never have. Only worth subscribing to alongside
            # actual coverage — on its own it monitors nothing.
            subs.append((TOPIC_GLM_STATUS, 1))
            for cell in self._cells:
                subs.append((glm_topic_for_geohash(cell), 0))
        if len(subs) > MAX_SUBSCRIPTIONS:
            # The broker caps subscriptions per client at 20. Keep alerts and
            # the heartbeat, trim coverage, and say so instead of failing
            # silently.
            _LOGGER.warning(
                "Configuration needs %d subscriptions but the broker allows %d; "
                "dropping the last %d lightning cells",
                len(subs),
                MAX_SUBSCRIPTIONS,
                len(subs) - MAX_SUBSCRIPTIONS,
            )
            subs = subs[:MAX_SUBSCRIPTIONS]
        return subs

    async def async_start(self) -> None:
        if self.enable_glm and self.glm_track_persons:
            # Every person, not only those with a fix now: someone who arrives
            # home and gains a fix has to start being measured.
            self._unsub_persons = async_track_state_change_event(
                self.hass,
                [state.entity_id for state in self.hass.states.async_all("person")],
                self._handle_person_moved,
            )
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
        if self._unsub_persons:
            self._unsub_persons()
            self._unsub_persons = None
        if self._client:
            await self._client.stop()
            self._client = None

    @callback
    def _handle_person_moved(self, _event: Any) -> None:
        """Re-plan coverage when a tracked person moves.

        Only a change to the *cell set* costs anything, so ordinary movement
        inside the covered area does not touch the broker. Metrics are always
        recomputed, because the person has moved relative to the same flashes.
        """
        if not self.enable_glm:
            return
        if self._refresh_targets() and self._client:
            self.hass.async_create_task(
                self._client.set_subscriptions(self._build_subscriptions())
            )
        self._recompute_all()

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
            if self.enable_glm and not self._backfill_done:
                self._backfill_done = True
                self.hass.async_create_task(self._async_backfill())
        async_dispatcher_send(
            self.hass, f"{SIGNAL_CONNECTION}_{self.entry.entry_id}", connected
        )

    async def _handle_message(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if topic.startswith("wxalerts/nws/v1/same/") and len(parts) >= 6:
            self._handle_alert(topic, parts[4], payload)
        elif topic == TOPIC_GLM_STATUS:
            self._handle_glm_status(payload)
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

    def _backfill_endpoint(self) -> str:
        """Where to ask for history.

        Derived from the broker host when unset, so a deployment that routes
        the ingest alongside the broker needs no configuration at all — and one
        that does not simply gets a slower ramp.
        """
        if self.glm_backfill_url:
            return self.glm_backfill_url
        return f"https://{DEFAULT_HOST}{BACKFILL_PATH}"

    async def _async_backfill(self) -> None:
        """Fill the window from history, once, on the first connection.

        The flash tree is unretained, so a client connecting mid-storm sees an
        empty window and reports a calm sky while ramping up over the whole
        window length. That is the worst possible moment to under-report.

        Entirely best-effort: the endpoint is not routed on every deployment,
        and failing to reach it costs a slower ramp, not a broken integration.
        """
        endpoint = self._backfill_endpoint()
        session = async_get_clientsession(self.hass)
        recovered = 0

        for target in list(self._targets.values()):
            params = {
                "lat": f"{target.latitude:.5f}",
                "lon": f"{target.longitude:.5f}",
                "radius_mi": str(self.glm_radius_mi),
                "minutes": str(self.glm_window_minutes),
            }
            try:
                async with session.get(
                    endpoint, params=params, timeout=aiohttp.ClientTimeout(
                        total=BACKFILL_TIMEOUT
                    )
                ) as response:
                    if response.status != 200:
                        self._warn_backfill(
                            "returned HTTP %s", endpoint, response.status
                        )
                        return
                    body = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                self._warn_backfill("is unreachable (%s)", endpoint, err)
                return

            flashes = self._parse_flashes(body.get("flashes") or [], time.time())
            if flashes:
                self._store.add(flashes)
                recovered += len(flashes)

        if recovered:
            _LOGGER.debug(
                "Backfilled %d flash(es) from %s; the window starts populated "
                "instead of ramping from zero",
                recovered,
                endpoint,
            )
            self._recompute_all()

    def _warn_backfill(self, reason: str, endpoint: str, detail: Any) -> None:
        """Say it once. A backfill failure is a slower ramp, not an outage, and
        repeating it every reconnect would bury the things that matter."""
        if self._backfill_warned:
            return
        self._backfill_warned = True
        _LOGGER.info(
            "Lightning history at %s " + reason + "; the window will fill from "
            "live flashes instead. This is not an error — it only means the "
            "first few minutes after a restart under-report.",
            endpoint,
            detail,
        )

    def _parse_flashes(self, raw: list[dict[str, Any]], now: float) -> list[Flash]:
        """Flash objects from the feed's wire shape.

        Shared by the live burst path and the backfill, which is the point of
        the endpoint returning the same shape as an MQTT message.
        """
        flashes: list[Flash] = []
        for flash in raw:
            lat = flash.get("lat")
            lon = flash.get("lon")
            if lat is None or lon is None:
                continue
            flashes.append(
                Flash(
                    when=parse_flash_time(flash.get("t"), now),
                    latitude=float(lat),
                    longitude=float(lon),
                    energy_j=flash.get("energy_j"),
                )
            )
        return flashes

    def _handle_glm_status(self, payload: bytes) -> None:
        """Retained heartbeat from the lightning ingest.

        Its own view of whether NOAA is still delivering is better than ours:
        we cannot tell a quiet sky from a dead feed, and it can.
        """
        if not payload:
            # The heartbeat was cleared rather than contradicted; fall back to
            # flash arrivals instead of declaring the feed dead.
            self._glm_heartbeat = None
            self._glm_feed_ok = None
            self._recompute_all()
            return
        try:
            status = json.loads(payload)
        except ValueError:
            _LOGGER.warning("Undecodable lightning heartbeat")
            return

        age = status.get("last_granule_age_seconds")
        healthy = status.get("healthy", status.get("feed", True))
        now = time.time()
        self._glm_feed_ok = healthy is not False
        if healthy is False:
            self._glm_heartbeat = None
            self._recompute_all()
            return

        # The heartbeat is retained, so it carries no hint of its own age: a
        # frozen "last granule 18 s ago" from an ingest that died an hour ago
        # would read as perfectly healthy. `at` is what makes that detectable —
        # real feed age is how old the message is plus how stale it said the
        # feed was when it was written.
        written_at = parse_flash_time(status.get("at"), now)
        message_age = max(0.0, now - written_at)
        granule_age = float(age) if isinstance(age, (int, float)) and age >= 0 else 0.0
        self._glm_heartbeat = now - (message_age + granule_age)
        self._recompute_all()

    def _handle_glm(self, leaf_geohash: str, payload: bytes) -> None:
        if not payload:
            return  # GLM is stateless; nothing is retained or tombstoned
        try:
            burst = json.loads(payload)
        except ValueError:
            return

        now = time.time()
        self._glm_last_message = now
        parsed = self._parse_flashes(burst.get("flashes") or [], now)
        if not parsed:
            return

        self._store.add(parsed)
        self._recompute_all(now)

    def _recompute_all(self, now: float | None = None) -> None:
        """Recompute every target against the shared window and dispatch.

        Done centrally rather than lazily in each entity because one burst
        changes every target at once, and because the trend has to be sampled on
        a schedule the entities do not control.
        """
        now = time.time() if now is None else now
        self._store.prune(now)
        for target in self._targets.values():
            metrics = compute(
                self._store,
                target,
                radius_mi=self.glm_radius_mi,
                alert_mi=self.glm_alert_mi,
                rings=self.glm_rings,
                now=now,
            )
            self._trend.sample(target.key, metrics.nearest_mi, now)
            metrics.trend = self._trend.label(target.key)
            self._metrics[target.key] = metrics
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_GLM_UPDATED}_{self.entry.entry_id}_{target.key}",
            )

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

    # ------------------------------------------------------------------
    # Lightning state the entities read
    # ------------------------------------------------------------------

    def metrics_for(self, key: str) -> Metrics:
        """Proximity metrics for a target; an empty set if it has none yet."""
        return self._metrics.get(key) or Metrics(
            rings={ring: 0 for ring in self.glm_rings}
        )

    @property
    def glm_feed_age(self) -> float | None:
        """Seconds since the lightning feed last showed a sign of life.

        The heartbeat is authoritative when present because it reports whether
        NOAA is still delivering. Without one, flash arrivals are the only
        evidence available — and they are weak evidence, since a genuinely calm
        sky produces none either. That asymmetry is why the heartbeat exists.
        """
        if self._glm_heartbeat is not None:
            return max(0.0, time.time() - self._glm_heartbeat)
        if self._glm_last_message is not None:
            return max(0.0, time.time() - self._glm_last_message)
        return None

    @property
    def glm_available(self) -> bool:
        """Whether a lightning reading can be trusted.

        A stale feed reports unavailable rather than zero. Reporting a confident
        zero during an outage is the dangerous failure — it looks exactly like
        calm weather, and the outage is most likely when the storm is overhead.
        """
        if not self.connected:
            return False
        if self._glm_feed_ok is False:
            return False
        age = self.glm_feed_age
        if age is None:
            # Nothing heard yet. Trust the connection during the window it takes
            # a quiet cell to produce its first message, then stop.
            return True
        return age <= GLM_STALE_SECONDS

    @property
    def glm_window_flashes(self) -> int:
        """Total flashes held, across every covered cell."""
        return len(self._store)

    def recent_flashes(self, key: str, limit: int = 25) -> list[dict[str, Any]]:
        """The closest recent flashes to a target, nearest first."""
        target = self._targets.get(key)
        if target is None:
            return []

        scored = []
        for flash in self._store.flashes():
            distance = haversine_miles(
                target.latitude, target.longitude, flash.latitude, flash.longitude
            )
            if distance <= self.glm_radius_mi:
                scored.append((distance, flash))
        scored.sort(key=lambda pair: pair[0])
        return [
            {
                "distance_mi": round(distance, 1),
                "latitude": flash.latitude,
                "longitude": flash.longitude,
                "energy_j": flash.energy_j,
                "time": datetime.fromtimestamp(flash.when, tz=timezone.utc).isoformat(),
            }
            for distance, flash in scored[:limit]
        ]
