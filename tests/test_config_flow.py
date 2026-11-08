"""Config flow: the broker login, the Supporter upsell, and re-authentication.

The flow tests patch ``async_validate_login``; the validation itself is tested
below against a faked ``aiomqtt.Client``, so no test dials the broker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiomqtt.exceptions import MqttConnectError
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType

from custom_components.wxalerts.const import (
    CONF_EVENT_SENSORS,
    CONF_PLACES,
    DOMAIN,
    PRICING_URL,
)
from custom_components.wxalerts.coordinator import (
    CannotConnect,
    InvalidAuth,
    SupporterRequired,
    async_validate_login,
)
from custom_components.wxalerts.events import DEFAULT_EVENT_KEYS

from .conftest import make_entry
from .const import CACHED_PLACES, PASSWORD, PLACES, TOPIC_PLACES, USERNAME, encode

VALIDATE = "custom_components.wxalerts.config_flow.async_validate_login"
LOGIN = {CONF_USERNAME: USERNAME, CONF_PASSWORD: PASSWORD}


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    yield


async def _start_user_flow(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_a_good_login_creates_the_entry_with_its_places(hass, mock_feed_client):
    result = await _start_user_flow(hass)
    assert result["description_placeholders"]["pricing_url"] == PRICING_URL

    with patch(VALIDATE, AsyncMock(return_value=PLACES)) as validate:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**LOGIN, CONF_USERNAME: f"  {USERNAME} "}
        )
        await hass.async_block_till_done()

    validate.assert_awaited_once_with(hass, USERNAME, PASSWORD)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "WxAlerts"
    assert result["data"] == {
        CONF_USERNAME: USERNAME,
        CONF_PASSWORD: PASSWORD,
        CONF_PLACES: CACHED_PLACES,
    }
    assert result["options"] == {CONF_EVENT_SENSORS: list(DEFAULT_EVENT_KEYS)}
    # Devices exist straight away, before the broker replays anything.
    assert hass.states.get("binary_sensor.wxalerts_home_storm_cell_approaching")


async def test_a_good_login_before_places_arrive_still_creates_the_entry(
    hass, mock_feed_client
):
    result = await _start_user_flow(hass)
    with patch(VALIDATE, AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PLACES] == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InvalidAuth, "invalid_auth"),
        (SupporterRequired, "supporter_required"),
        (CannotConnect, "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
async def test_errors_are_shown_and_the_form_stays(hass, error, expected):
    result = await _start_user_flow(hass)
    with patch(VALIDATE, AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    # The upsell link rides along with every error, so a Free user reading
    # "that did not work" also sees where to upgrade.
    assert result["description_placeholders"]["pricing_url"] == PRICING_URL


async def test_the_bad_login_message_carries_the_upsell(hass):
    """The marketed path for Free users: the error itself says Home Assistant
    needs Supporter and links to pricing."""
    import json
    from pathlib import Path

    strings = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components/wxalerts/strings.json"
        ).read_text()
    )
    errors = strings["config"]["error"]
    for key in ("invalid_auth", "supporter_required"):
        assert "Supporter plan" in errors[key]
        assert "{pricing_url}" in errors[key]
    translated = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components/wxalerts/translations/en.json"
        ).read_text()
    )
    assert translated == strings


async def test_a_fixed_login_after_an_error_succeeds(hass, mock_feed_client):
    result = await _start_user_flow(hass)
    with patch(VALIDATE, AsyncMock(side_effect=InvalidAuth)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    with patch(VALIDATE, AsyncMock(return_value=PLACES)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_only_one_entry_per_instance(hass, mock_feed_client):
    make_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] in ("single_instance_allowed", "already_configured")


# ===========================================================================
# Re-authentication
# ===========================================================================
async def test_reauth_takes_a_rotated_password(hass, mock_feed_client):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(VALIDATE, AsyncMock(side_effect=InvalidAuth)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**LOGIN, CONF_PASSWORD: "old"}
        )
    assert result["errors"] == {"base": "invalid_auth"}

    with patch(VALIDATE, AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**LOGIN, CONF_PASSWORD: "rotated"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "rotated"
    # Places are kept when the check did not see the retained list.
    assert entry.data[CONF_PLACES] == CACHED_PLACES


async def test_reauth_refreshes_places_when_it_sees_them(hass, mock_feed_client):
    entry = make_entry(places=[])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reauth_flow(hass)
    with patch(VALIDATE, AsyncMock(return_value=PLACES)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
        await hass.async_block_till_done()
    assert entry.data[CONF_PLACES] == CACHED_PLACES


# ===========================================================================
# Options
# ===========================================================================
async def test_options_flow_selects_event_types(hass, mock_feed_client):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EVENT_SENSORS: ["tornado_watch"]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_EVENT_SENSORS: ["tornado_watch"]}


# ===========================================================================
# async_validate_login against a faked broker
# ===========================================================================
class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _FakeClient:
    def __init__(self, *, enter_error=None, suback=(1,), messages=()) -> None:
        self.enter_error = enter_error
        self.suback = list(suback)
        self._messages = list(messages)
        self.subscribed: list[str] = []
        self.kwargs: dict = {}

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *_exc):
        return False

    async def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)
        return self.suback

    @property
    def messages(self):
        messages = self._messages

        async def generator():
            for message in messages:
                yield message

        return generator()


def _patch_client(fake: _FakeClient):
    def factory(**kwargs):
        fake.kwargs = kwargs
        return fake

    return patch(
        "custom_components.wxalerts.coordinator.aiomqtt.Client",
        MagicMock(side_effect=factory),
    )


async def test_validation_returns_the_retained_places(hass):
    fake = _FakeClient(
        messages=[
            _Message(f"wxalerts/u/{USERNAME}/status", b"{}"),
            _Message(TOPIC_PLACES, encode(PLACES)),
        ]
    )
    with _patch_client(fake):
        places = await async_validate_login(hass, USERNAME, PASSWORD)

    assert places == PLACES
    assert fake.subscribed == [f"wxalerts/u/{USERNAME}/#"]
    assert fake.kwargs["username"] == USERNAME
    assert fake.kwargs["transport"] == "websockets"
    assert fake.kwargs["hostname"] == "mqtt.wxalerts.org"
    assert fake.kwargs["websocket_path"] == "/mqtt"


async def test_validation_without_places_is_still_a_good_login(hass):
    with _patch_client(_FakeClient(messages=[])):
        assert await async_validate_login(hass, USERNAME, PASSWORD) is None


@pytest.mark.parametrize("rc", [4, 5, 134, 135])
async def test_validation_maps_a_refused_login_to_invalid_auth(hass, rc):
    with _patch_client(_FakeClient(enter_error=MqttConnectError(rc))):
        with pytest.raises(InvalidAuth):
            await async_validate_login(hass, USERNAME, "wrong")


async def test_validation_maps_a_refused_subscription_to_supporter_required(hass):
    with _patch_client(_FakeClient(suback=(0x87,))):
        with pytest.raises(SupporterRequired):
            await async_validate_login(hass, USERNAME, PASSWORD)


async def test_the_public_login_gets_the_upsell_without_dialling(hass):
    with patch("custom_components.wxalerts.coordinator.aiomqtt.Client") as client:
        with pytest.raises(SupporterRequired):
            await async_validate_login(hass, "wxalerts", "wxalerts")
    client.assert_not_called()


@pytest.mark.parametrize("error", [MqttConnectError(3), OSError("no route"), TimeoutError()])
async def test_validation_maps_network_failures_to_cannot_connect(hass, error):
    with _patch_client(_FakeClient(enter_error=error)):
        with pytest.raises(CannotConnect):
            await async_validate_login(hass, USERNAME, PASSWORD)
