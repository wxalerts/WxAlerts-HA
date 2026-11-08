"""Config flow: sign in with wxalerts.org, register the instance, done.

The browser leg is simulated by handing the flow what Home Assistant's OAuth
callback view would (the code and the decoded state). The token endpoint and
the account API are mocked with aioclient_mock; nothing leaves the box.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from custom_components.wxalerts.const import (
    API_INSTANCES_URL,
    CONF_EVENT_SENSORS,
    CONF_PLACES,
    DOMAIN,
    HA_OAUTH_CLIENT_ID,
    OAUTH_AUTHORIZE_URL,
    OAUTH_TOKEN_URL,
    PRICING_URL,
)
from custom_components.wxalerts.events import DEFAULT_EVENT_KEYS

from .conftest import make_entry
from .const import CACHED_PLACES, PASSWORD, USERNAME, registration

TOKEN = {
    "access_token": "fresh-access",
    "refresh_token": "fresh-refresh",
    "token_type": "Bearer",
    "expires_in": 3600,
}


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations, current_request_with_host):
    yield


async def _sign_in(hass, flow_id: str, url: str):
    """Do what the browser and the OAuth callback view would."""
    query = parse_qs(urlparse(url).query)
    from homeassistant.helpers.config_entry_oauth2_flow import _decode_jwt

    state = _decode_jwt(hass, query["state"][0])
    return await hass.config_entries.flow.async_configure(
        flow_id, {"code": "auth-code", "state": state}
    )


async def _start(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    return result


async def test_signing_in_registers_the_instance_and_creates_the_entry(
    hass, aioclient_mock, mock_feed_client
):
    result = await _start(hass)
    url = urlparse(result["url"])
    query = parse_qs(url.query)
    assert result["url"].startswith(OAUTH_AUTHORIZE_URL)
    assert query["client_id"] == [HA_OAUTH_CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid profile email offline_access"]

    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN)
    aioclient_mock.post(API_INSTANCES_URL, json=registration())

    result = await _sign_in(hass, result["flow_id"], result["url"])
    assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "WxAlerts"
    data = result["data"]
    assert data[CONF_USERNAME] == USERNAME
    assert data[CONF_PASSWORD] == PASSWORD
    assert data["token"]["access_token"] == "fresh-access"
    assert data["token"]["refresh_token"] == "fresh-refresh"
    assert data["auth_implementation"] == DOMAIN
    assert data[CONF_PLACES] == []
    assert result["options"] == {CONF_EVENT_SENSORS: list(DEFAULT_EVENT_KEYS)}

    # The token exchange carried the PKCE verifier, and registration the
    # bearer token, the installation id and the location name.
    token_call = aioclient_mock.mock_calls[0]
    assert "code_verifier" in token_call[2]
    register_call = aioclient_mock.mock_calls[1]
    assert register_call[3]["Authorization"] == "Bearer fresh-access"
    assert register_call[2] == {
        "instance_id": await async_get_instance_id(hass),
        "name": hass.config.location_name,
    }
    # The broker is dialled with what came back.
    assert mock_feed_client.call_args[0][0].username == USERNAME


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (403, {"detail": {"code": "supporter_required"}}, "supporter_required"),
        (
            409,
            {
                "detail": {
                    "code": "instance_limit",
                    "limit": 1,
                    "used": 1,
                    "upgrade_url": "https://wxalerts.org/pricing/",
                }
            },
            "instance_limit",
        ),
        (401, {"detail": "invalid_token"}, "oauth_unauthorized"),
        (502, {}, "cannot_connect"),
    ],
)
async def test_registration_failures_explain_themselves(
    hass, aioclient_mock, status, body, reason
):
    result = await _start(hass)
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN)
    aioclient_mock.post(API_INSTANCES_URL, status=status, json=body)

    result = await _sign_in(hass, result["flow_id"], result["url"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    if reason in ("supporter_required", "instance_limit"):
        assert result["description_placeholders"]["pricing_url"] == PRICING_URL
    if reason == "instance_limit":
        assert result["description_placeholders"]["limit"] == "1"


async def test_a_flat_error_code_is_understood_too(hass, aioclient_mock):
    result = await _start(hass)
    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN)
    aioclient_mock.post(API_INSTANCES_URL, status=403, json={"code": "supporter_required"})
    result = await _sign_in(hass, result["flow_id"], result["url"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["reason"] == "supporter_required"


async def test_the_upsell_messages_carry_the_pricing_link(hass):
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components/wxalerts"
    strings = json.loads((root / "strings.json").read_text())
    aborts = strings["config"]["abort"]
    assert "Supporter plan" in aborts["supporter_required"]
    for key in ("supporter_required", "instance_limit"):
        assert "{pricing_url}" in aborts[key]
    assert json.loads((root / "translations/en.json").read_text()) == strings


async def test_a_refused_sign_in_aborts(hass):
    result = await _start(hass)
    from homeassistant.helpers.config_entry_oauth2_flow import _decode_jwt

    state = _decode_jwt(hass, parse_qs(urlparse(result["url"]).query)["state"][0])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"error": "access_denied", "state": state}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "user_rejected_authorize"


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


async def test_reauth_signs_in_again_and_re_registers(
    hass, aioclient_mock, mock_feed_client
):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.EXTERNAL_STEP

    aioclient_mock.post(OAUTH_TOKEN_URL, json=TOKEN)
    aioclient_mock.post(API_INSTANCES_URL, json=registration(password="rotated"))
    result = await _sign_in(hass, result["flow_id"], result["url"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "rotated"
    assert entry.data["token"]["access_token"] == "fresh-access"
    # Places are kept; the broker republishes them anyway.
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
