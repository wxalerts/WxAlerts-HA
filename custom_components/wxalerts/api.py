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


async def async_register_instance(hass: HomeAssistant, access_token: str) -> dict[str, Any]:
    """Register this Home Assistant instance and return its broker login.

    The body names the instance by Home Assistant's own installation id and
    location name, which is what the website lists it as.
    """
    body = {
        "instance_id": await async_get_instance_id(hass),
        "name": hass.config.location_name or "Home Assistant",
    }
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            API_INSTANCES_URL,
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS),
        ) as response:
            try:
                payload = await response.json(content_type=None)
            except ValueError:
                payload = None
            payload = payload if isinstance(payload, dict) else {}
            status = response.status
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ApiUnavailable(str(err)) from err

    if status in (HTTPStatus.OK, HTTPStatus.CREATED):
        if not payload.get("username") or not payload.get("password"):
            raise ApiUnavailable("registration returned no broker login")
        return payload
    code = _error_code(payload)
    if status == HTTPStatus.UNAUTHORIZED:
        raise ApiAuthError(code or "unauthorized")
    if status == HTTPStatus.FORBIDDEN and code == "supporter_required":
        raise SupporterRequired
    if status == HTTPStatus.CONFLICT and code == "instance_limit":
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
        raise InstanceLimit(detail.get("limit"), detail.get("used"), detail.get("upgrade_url", ""))
    if status == HTTPStatus.FORBIDDEN:
        raise ApiAuthError(code or "forbidden")
    raise ApiUnavailable(f"HTTP {status} {code}".strip())


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


async def async_refresh_login(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Get a fresh broker login with the stored token, refreshing it as needed.

    Writes the new token and login into the entry. Raises ApiAuthError when
    the token cannot be refreshed (sign in again), SupporterRequired or
    InstanceLimit for plan problems, ApiUnavailable for network trouble.
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
        registration = await async_register_instance(
            hass, entry.data[CONF_TOKEN]["access_token"]
        )
    except ApiAuthError:
        # A token can be refused before its stated expiry (revoked sessions,
        # clock skew). One forced refresh, then give up.
        token = await _force_refresh(hass, entry, implementation)
        registration = await async_register_instance(hass, token["access_token"])

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
