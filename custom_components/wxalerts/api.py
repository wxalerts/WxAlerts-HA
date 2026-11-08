"""The wxalerts.org account API: sign-in and broker login registration.

Signing in is OAuth2 with PKCE against authentik, using a public client id
compiled into the integration. With the access token the integration registers
this Home Assistant instance and gets its own broker login back. Registering
the same instance again rotates that login instead of using another slot,
which is also how a rejected broker password is recovered.
"""

from __future__ import annotations

import logging
import time
from http import HTTPStatus
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .const import (
    API_HEARTBEAT_URL_FMT,
    API_INSTANCES_URL,
    API_TIMEOUT_SECONDS,
    CONF_INSTANCE_ID,
    CONF_TOKEN,
    DOMAIN,
    HA_OAUTH_CLIENT_ID,
    OAUTH_AUTHORIZE_URL,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
    PRICING_URL,
    REASON_REVOKED,
)

_LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    """Base for account API failures."""


class ApiAuthError(ApiError):
    """The access token was refused; a refresh or a new sign-in is needed."""


class SupporterRequired(ApiError):
    """The account is on the Free plan."""


class InstanceLimit(ApiError):
    """Every Home Assistant slot on the plan is in use."""

    def __init__(self, limit: Any = None, used: Any = None, upgrade_url: str = "") -> None:
        super().__init__(f"instance limit reached ({used} of {limit})")
        self.limit = limit
        self.used = used
        self.upgrade_url = upgrade_url or PRICING_URL


class InstanceDeactivated(ApiError):
    """The instance was revoked on the website or is outside the plan."""

    def __init__(self, reason: str | None) -> None:
        super().__init__(f"instance deactivated ({reason})")
        self.reason = reason or REASON_REVOKED


class ApiUnavailable(ApiError):
    """The API could not be reached or answered with a server error."""


class WxAlertsOAuth2Implementation(
    config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce
):
    """authentik, public client, PKCE, with a refresh token."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(
            hass,
            DOMAIN,
            HA_OAUTH_CLIENT_ID,
            authorize_url=OAUTH_AUTHORIZE_URL,
            token_url=OAUTH_TOKEN_URL,
        )

    @property
    def name(self) -> str:
        return "WxAlerts"

    @property
    def extra_authorize_data(self) -> dict:
        return {"scope": OAUTH_SCOPES, **super().extra_authorize_data}


def async_register_oauth(hass: HomeAssistant) -> None:
    """Make the sign-in implementation available to flows and entries."""
    config_entry_oauth2_flow.async_register_implementation(
        hass, DOMAIN, WxAlertsOAuth2Implementation(hass)
    )


async def _post(
    hass: HomeAssistant, url: str, access_token: str, body: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS),
        ) as response:
            try:
                payload = await response.json(content_type=None)
            except ValueError:
                payload = None
            return response.status, payload if isinstance(payload, dict) else {}
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ApiUnavailable(str(err)) from err


def _raise_for(status: int, payload: dict[str, Any]) -> None:
    """Turn an error response into the exception the callers handle.

    Errors arrive as ``{"detail": {code, message, upgrade_url}}``, with
    ``limit`` and ``used`` added for 409.
    """
    code = _error_code(payload)
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
    if status == HTTPStatus.UNAUTHORIZED:
        raise ApiAuthError(code or "unauthorized")
    if status == HTTPStatus.FORBIDDEN and code == "supporter_required":
        raise SupporterRequired
    if status == HTTPStatus.CONFLICT and code == "instance_limit":
        raise InstanceLimit(detail.get("limit"), detail.get("used"), detail.get("upgrade_url", ""))
    if status == HTTPStatus.FORBIDDEN:
        raise ApiAuthError(code or "forbidden")
    raise ApiUnavailable(f"HTTP {status} {code}".strip())


async def _instance_body(hass: HomeAssistant) -> dict[str, Any]:
    return {
        "instance_id": await async_get_instance_id(hass),
        "name": hass.config.location_name or "Home Assistant",
    }


async def async_register_instance(hass: HomeAssistant, access_token: str) -> dict[str, Any]:
    """Register this Home Assistant instance and return its broker login.

    The body names the instance by Home Assistant's own installation id and
    location name, which is what the website lists it as. Registering the
    same instance again returns the same username with a new password.
    """
    status, payload = await _post(
        hass, API_INSTANCES_URL, access_token, await _instance_body(hass)
    )
    if status in (HTTPStatus.OK, HTTPStatus.CREATED):
        if not payload.get("username") or not payload.get("password"):
            raise ApiUnavailable("registration returned no broker login")
        return payload
    _raise_for(status, payload)
    raise AssertionError("unreachable")


async def async_heartbeat(
    hass: HomeAssistant, access_token: str, instance_id: str
) -> dict[str, Any]:
    """Report this instance as alive and read back its standing.

    This is the only source of "last seen" on the website. The response is
    the instance entry, whose ``active`` and ``deactivated_reason`` say
    whether the person revoked it or the plan no longer covers it.
    """
    body = await _instance_body(hass)
    status, payload = await _post(
        hass,
        API_HEARTBEAT_URL_FMT.format(instance_id=instance_id),
        access_token,
        {"name": body["name"]},
    )
    if status == HTTPStatus.OK:
        return payload
    if status == HTTPStatus.NOT_FOUND:
        # The instance no longer exists on the account: as final as a revoke.
        return {"instance_id": instance_id, "active": False, "deactivated_reason": REASON_REVOKED}
    _raise_for(status, payload)
    raise AssertionError("unreachable")


def _error_code(payload: dict[str, Any]) -> str:
    """The machine-readable code, wherever the API put it.

    FastAPI nests it under ``detail``; accept a flat ``code`` or ``error`` too.
    """
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("code") or detail.get("error") or "")
    if isinstance(detail, str):
        return detail
    return str(payload.get("code") or payload.get("error") or "")


def broker_data(registration: dict[str, Any], instance_id: str | None = None) -> dict[str, Any]:
    """The entry fields a registration response fills in."""
    return {
        CONF_USERNAME: registration["username"],
        CONF_PASSWORD: registration["password"],
        CONF_INSTANCE_ID: registration.get("instance_id") or instance_id,
    }


async def _with_token(hass: HomeAssistant, entry: ConfigEntry, call):
    """Run ``call(access_token)`` with a valid token.

    The token is refreshed when it has expired, and once more if the API
    refuses it anyway (revoked sessions, clock skew). Raises ApiAuthError when
    no usable token can be had: the person has to sign in again.
    """
    if CONF_TOKEN not in entry.data:
        raise ApiAuthError("no token stored")
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    try:
        await session.async_ensure_token_valid()
    except aiohttp.ClientResponseError as err:
        if err.status in (HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED):
            raise ApiAuthError("refresh token refused") from err
        raise ApiUnavailable(str(err)) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ApiUnavailable(str(err)) from err
    try:
        return await call(entry.data[CONF_TOKEN]["access_token"])
    except ApiAuthError:
        token = await _force_refresh(hass, entry, implementation)
        return await call(token["access_token"])


async def async_send_heartbeat(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Heartbeat with the entry's token. Raises InstanceDeactivated if the
    instance is no longer active."""
    instance_id = entry.data.get(CONF_INSTANCE_ID) or await async_get_instance_id(hass)
    standing = await _with_token(
        hass, entry, lambda token: async_heartbeat(hass, token, instance_id)
    )
    if standing.get("active") is False:
        raise InstanceDeactivated(standing.get("deactivated_reason"))
    return standing


async def async_refresh_login(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Get a fresh broker login with the stored token, refreshing it as needed.

    The heartbeat goes first, so a revoke sticks: an instance the person
    removed on the website, or one outside the plan, raises
    InstanceDeactivated and is never silently re-registered. Otherwise the
    instance is registered again and the new login written into the entry.
    Raises ApiAuthError when the token cannot be refreshed, SupporterRequired
    or InstanceLimit for plan problems, ApiUnavailable for network trouble.
    """
    if entry.data.get(CONF_INSTANCE_ID):
        await async_send_heartbeat(hass, entry)
    registration = await _with_token(
        hass, entry, lambda token: async_register_instance(hass, token)
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, **broker_data(registration, entry.data.get(CONF_INSTANCE_ID))}
    )


async def _force_refresh(hass, entry, implementation) -> dict[str, Any]:
    try:
        token = await implementation.async_refresh_token(entry.data[CONF_TOKEN])
    except aiohttp.ClientResponseError as err:
        if err.status in (HTTPStatus.BAD_REQUEST, HTTPStatus.UNAUTHORIZED):
            raise ApiAuthError("refresh token refused") from err
        raise ApiUnavailable(str(err)) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ApiUnavailable(str(err)) from err
    token.setdefault("expires_at", time.time() + float(token.get("expires_in", 0)))
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: token})
    return token
