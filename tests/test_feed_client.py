"""FeedClient: the MQTT transport.

``aiomqtt.Client`` is faked rather than dialled. What matters here is the
behaviour the broker cares about — v5 over websockets, the right QoS per
topic, and above all a backoff that does not hot-loop, because every fresh
subscription replays the entire retained set.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import aiomqtt
import pytest

from custom_components.wxalerts.coordinator import (
    _BACKOFF_INITIAL,
    _BACKOFF_MAX,
    FeedClient,
    FeedConfig,
)

SUBSCRIPTIONS = [
    ("wxalerts/nws/v1/same/012113/#", 1),
    ("wxalerts/glm/v1/d/j/6/#", 0),
]


class FakeMessage:
    """What aiomqtt yields; ``topic`` is an object, not a string."""

    def __init__(self, topic: str, payload) -> None:
        self.topic = MagicMock()
        self.topic.__str__ = lambda _self, value=topic: value
        self.payload = payload


class FakeMqttClient:
    """Stands in for ``aiomqtt.Client``.

    ``script`` is a list of per-connection behaviours: either a list of
    messages to deliver before the stream ends, or an exception to raise.
    """

    instances: list["FakeMqttClient"] = []

    def __init__(self, script, **kwargs):
        self._script = script
        self.kwargs = kwargs
        self.subscribed: list[tuple[str, int]] = []
        FakeMqttClient.instances.append(self)

    async def __aenter__(self):
        if isinstance(self._script, Exception):
            raise self._script
        return self

    async def __aexit__(self, *_exc):
        return False

    async def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    @property
    def messages(self):
        script = self._script

        async def generator():
            for item in script:
                if isinstance(item, Exception):
                    raise item
                yield item

        return generator()


@pytest.fixture
def fake_mqtt():
    """Patch aiomqtt.Client with a scripted fake.

    Yields a setter: pass one script per connection attempt, in order.
    """
    FakeMqttClient.instances.clear()
    scripts: list = []

    def factory(**kwargs):
        script = scripts.pop(0) if scripts else []
        return FakeMqttClient(script, **kwargs)

    with patch(
        "custom_components.wxalerts.coordinator.aiomqtt.Client", side_effect=factory
    ):
        yield scripts


@pytest.fixture
def collected():
    """A recording on_message handler."""
    messages: list[tuple[str, bytes]] = []

    async def handler(topic: str, payload: bytes) -> None:
        messages.append((topic, payload))

    handler.messages = messages
    return handler


def make_client(on_message, on_connection=None, **config_kwargs) -> FeedClient:
    config = FeedConfig(
        client_id="wxha-test-0001", subscriptions=SUBSCRIPTIONS, **config_kwargs
    )
    return FeedClient(config, on_message, on_connection or (lambda _connected: None))


async def run_feed(client: FeedClient, connections: int = 1) -> list[float]:
    """Run the reconnect loop for N connection attempts and return the
    backoff delays it asked for.

    The loop is infinite by design, so the exit is the backoff sleep itself:
    the fake stops the client once it has been asked to wait N times.
    """
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= connections:
            client._stopping = True

    with patch("custom_components.wxalerts.coordinator.asyncio.sleep", fake_sleep):
        await client._run()
    return delays


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


async def test_connects_with_v5_over_websockets(fake_mqtt, collected):
    fake_mqtt.append([])
    client = make_client(collected)

    await run_feed(client)

    kwargs = FakeMqttClient.instances[0].kwargs
    assert kwargs["hostname"] == "mqtt.wxalerts.org"
    assert kwargs["port"] == 443
    assert kwargs["transport"] == "websockets"
    assert kwargs["websocket_path"] == "/mqtt"
    assert kwargs["protocol"] is aiomqtt.ProtocolVersion.V5
    assert kwargs["username"] == "wxalerts"
    assert kwargs["password"] == "wxalerts"
    assert kwargs["identifier"] == "wxha-test-0001"
    assert kwargs["tls_context"] is not None


async def test_subscribes_to_every_configured_topic(fake_mqtt, collected):
    fake_mqtt.append([])
    client = make_client(collected)

    await run_feed(client)

    assert FakeMqttClient.instances[0].subscribed == SUBSCRIPTIONS


async def test_messages_reach_the_handler(fake_mqtt, collected):
    fake_mqtt.append(
        [
            FakeMessage("wxalerts/nws/v1/same/012113/0012", b'{"event":"x"}'),
            FakeMessage("wxalerts/nws/v1/same/012113/0012", b""),
        ]
    )
    client = make_client(collected)

    await run_feed(client)

    assert collected.messages == [
        ("wxalerts/nws/v1/same/012113/0012", b'{"event":"x"}'),
        ("wxalerts/nws/v1/same/012113/0012", b""),
    ]


async def test_payload_is_normalised_to_bytes(fake_mqtt, collected):
    """A str payload must still arrive as bytes, or json.loads on a tombstone
    check would behave differently by transport."""
    fake_mqtt.append(
        [
            FakeMessage("wxalerts/nws/v1/same/012113/0012", "a string"),
            FakeMessage("wxalerts/nws/v1/same/012113/0013", None),
        ]
    )
    client = make_client(collected)

    await run_feed(client)

    assert collected.messages == [
        ("wxalerts/nws/v1/same/012113/0012", b"a string"),
        ("wxalerts/nws/v1/same/012113/0013", b""),
    ]


async def test_connection_state_is_reported_up_then_down(fake_mqtt, collected):
    states: list[bool] = []
    fake_mqtt.append([])
    client = make_client(collected, on_connection=states.append)

    await run_feed(client)

    assert states == [True, False]


async def test_a_failed_connection_never_reports_connected(fake_mqtt, collected):
    states: list[bool] = []
    fake_mqtt.append(aiomqtt.MqttError("refused"))
    client = make_client(collected, on_connection=states.append)

    await run_feed(client)

    assert states == [False]


# ---------------------------------------------------------------------------
# Reconnection — "a reconnect loop is a bandwidth amplifier"
# ---------------------------------------------------------------------------


async def test_backoff_grows_exponentially_and_never_hot_loops(fake_mqtt, collected):
    client = make_client(collected)
    for _ in range(6):
        fake_mqtt.append(aiomqtt.MqttError("broker said no"))

    delays = await run_feed(client, connections=5)

    assert delays == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert delays[0] == _BACKOFF_INITIAL
    assert all(delay > 0 for delay in delays)


async def test_backoff_is_capped(fake_mqtt, collected):
    client = make_client(collected)
    for _ in range(30):
        fake_mqtt.append(aiomqtt.MqttError("still no"))

    delays = await run_feed(client, connections=12)

    assert max(delays) == _BACKOFF_MAX
    assert delays[-1] == _BACKOFF_MAX


async def test_backoff_resets_after_a_stable_connection(fake_mqtt, collected):
    """A connection that held for hours and then dropped is not the broker
    pushing back — the next attempt should not start at a five-minute wait.

    Each successful pass reads the clock twice: once on connect, once to
    judge whether the connection was stable. The third pass is scripted to
    have lasted well past the reset threshold.
    """
    client = make_client(collected)
    for _ in range(3):
        fake_mqtt.append([])  # connects, stream ends, reconnect

    # Never 0.0: the loop only judges stability when it actually connected,
    # and it tests that by truthiness of the connect timestamp.
    clock = iter([100.0, 100.0, 100.0, 100.0, 100.0, 10_000.0])

    with patch(
        "custom_components.wxalerts.coordinator.time.monotonic",
        side_effect=lambda: next(clock),
    ):
        delays = await run_feed(client, connections=3)

    assert delays == [_BACKOFF_INITIAL, _BACKOFF_INITIAL * 2, _BACKOFF_INITIAL]


async def test_reconnect_resubscribes(fake_mqtt, collected):
    """Retained alerts only repopulate if the subscriptions are re-issued."""
    fake_mqtt.append(aiomqtt.MqttError("dropped"))
    fake_mqtt.append([])
    client = make_client(collected)

    await run_feed(client, connections=2)

    assert len(FakeMqttClient.instances) == 2
    assert FakeMqttClient.instances[1].subscribed == SUBSCRIPTIONS


async def test_an_unexpected_error_does_not_kill_the_loop(fake_mqtt, collected):
    """A bug in message handling must not silently end the feed forever."""
    fake_mqtt.append(ValueError("something we did not anticipate"))
    fake_mqtt.append([])
    client = make_client(collected)

    await run_feed(client, connections=2)

    assert len(FakeMqttClient.instances) == 2


async def test_a_mid_stream_drop_is_retried(fake_mqtt, collected):
    fake_mqtt.append(
        [
            FakeMessage("wxalerts/nws/v1/same/012113/0012", b"{}"),
            aiomqtt.MqttError("socket died"),
        ]
    )
    fake_mqtt.append([])
    client = make_client(collected)

    await run_feed(client, connections=2)

    assert len(collected.messages) == 1
    assert len(FakeMqttClient.instances) == 2


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_stop_cancels_the_running_task(fake_mqtt, collected):
    """After an unload nothing may be left waiting to reconnect."""
    fake_mqtt.append([])
    client = make_client(collected)
    client.start()
    task = client._task
    await asyncio.sleep(0)  # let the loop reach its backoff sleep

    await client.stop()

    assert client._task is None
    assert client._stopping is True
    assert task.cancelled() or task.done()


async def test_stop_before_start_is_harmless(collected):
    client = make_client(collected)
    await client.stop()


async def test_a_stopped_client_does_not_reconnect(fake_mqtt, collected):
    """The stop flag is checked before the backoff, so an unload that lands
    exactly as the connection drops does not start a fresh one.

    Nothing is patched here: the loop really would sleep and reconnect, and
    the assertion is that it returned instead.
    """
    fake_mqtt.append(aiomqtt.MqttError("dropped"))
    fake_mqtt.append([])
    client = make_client(collected)

    def unload_on_disconnect(connected: bool) -> None:
        if not connected:
            client._stopping = True

    client._on_connection = unload_on_disconnect
    await client._run()

    assert len(FakeMqttClient.instances) == 1
