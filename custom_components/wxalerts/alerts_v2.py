"""MQTT V2 alerts, mapped onto the alert dict the entities read.

A V2 alert message (mqtt-fanout docs/contract.md section 6) differs from the V1
county message the entities were written for:

* ``id`` is the eight character short id, not the numeric id;
* ``status`` is ``active``, ``cancelled`` or ``expired``, and a cancellation
  may be partial (``places`` then names the places the alert no longer covers);
* ``places`` lists the labels of the account's places inside the alert, so the
  alert belongs to places rather than to a county;
* ``geometry`` is minimized, and there is no ``description`` or
  ``instruction``.

Everything else the entities and events show (vtec, event, phenomena,
significance, severity, urgency, certainty, headline, onset, ends, action,
geometry_source) is in the message under the same name. The two things that
are not, the numeric ``id`` and the ``instruction`` (and the unused
``description``), are read from the public API by short id
(``api.async_alert_page``). When that fails the alert is shown without them:
``instruction`` and the numeric ``id`` are ``None``, and ``short_id`` stays.

``id`` is therefore always the numeric id (as on V1) or ``None``, never the
short id.
"""

from __future__ import annotations

from typing import Any

from .const import V2_ALERT_STATUS_ACTIVE, V2_SCOPE_PREFIX

STATUSES = ("active", "cancelled", "expired")

# Copied through unchanged from a V2 message.
_COPIED = (
    "vtec",
    "event",
    "office",
    "phenomena",
    "significance",
    "severity",
    "urgency",
    "certainty",
    "headline",
    "issued_at",
    "onset",
    "expires",
    "ends",
    "ugc",
    "geometry",
    "geometry_source",
    "action",
    "status",
)


def place_scope(place_id: str) -> str:
    """The key a place's alerts are filed under on V2. Not a SAME code (six
    digits), so it can never collide with a V1 county key."""
    return f"{V2_SCOPE_PREFIX}{place_id}"


def parse_message(raw: Any) -> dict[str, Any] | None:
    """A V2 message as the entities' alert dict, or None if it is not one.

    Adds ``short_id``, ``url`` and ``places`` (the labels), and sets ``id``,
    ``instruction`` and ``description`` to None until the API fills them in.
    """
    if not isinstance(raw, dict):
        return None
    short_id = raw.get("id")
    if not isinstance(short_id, str) or len(short_id) != 8 or not short_id.isalnum():
        return None
    if raw.get("status") not in STATUSES:
        return None
    alert: dict[str, Any] = {key: raw.get(key) for key in _COPIED}
    places = raw.get("places")
    alert.update(
        {
            "id": None,
            "short_id": short_id,
            "url": raw.get("url"),
            "instruction": None,
            "description": None,
            "places": [p for p in places if isinstance(p, str)]
            if isinstance(places, list)
            else [],
        }
    )
    return alert


def merge_detail(alert: dict[str, Any], page: dict[str, Any] | None) -> dict[str, Any]:
    """The alert with what the API adds: numeric id, instruction, description.

    The message's own fields win where both have them (it is the newer one),
    except geometry, which the message may have minimized away to nothing.
    """
    if not page:
        return alert
    merged = dict(alert)
    numeric = page.get("id")
    if isinstance(numeric, int) and not isinstance(numeric, bool):
        merged["id"] = numeric
    for key in ("instruction", "description"):
        if page.get(key) is not None:
            merged[key] = page[key]
    if merged.get("geometry") is None and page.get("geometry") is not None:
        merged["geometry"] = page["geometry"]
        merged["geometry_source"] = merged.get("geometry_source") or page.get("geom_source")
    return merged


def from_api(item: dict[str, Any], page: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """An API alert object (``/alerts/at``, optionally with its page for the
    geometry) as the entities' alert dict, or None if it is unusable.

    Only for the alerts in force when the feed connects, so ``action`` is
    unknown (None) until the feed says something about the alert.
    """
    short_id = item.get("short_id")
    if not isinstance(short_id, str) or not short_id:
        return None
    source = {**item, **(page or {})}
    numeric = source.get("id")
    return {
        "id": numeric if isinstance(numeric, int) and not isinstance(numeric, bool) else None,
        "short_id": short_id,
        "url": f"https://wxalerts.org/alerts/{short_id}/",
        "vtec": source.get("vtec_key"),
        "event": source.get("event"),
        "office": source.get("office"),
        "phenomena": source.get("phenomena"),
        "significance": source.get("significance"),
        "severity": source.get("severity"),
        "urgency": source.get("urgency"),
        "certainty": source.get("certainty"),
        "headline": source.get("headline"),
        "issued_at": source.get("issued_at"),
        "onset": source.get("onset"),
        "expires": source.get("expires"),
        "ends": source.get("ends"),
        "ugc": source.get("ugc") or [],
        "geometry": source.get("geometry"),
        "geometry_source": source.get("geom_source"),
        "action": None,
        "status": source.get("status") or V2_ALERT_STATUS_ACTIVE,
        "instruction": source.get("instruction"),
        "description": source.get("description"),
        "places": [],
    }


def alert_key(user_id: int, alert: dict[str, Any]) -> str:
    """The alert's identity in the state: the topic the feed publishes it on,
    ``V2/<user id>/NWS/<office>/<short id>``, which is also how an alert loaded
    from the API is matched with a later message about it."""
    return f"V2/{user_id}/NWS/{alert.get('office')}/{alert['short_id']}"
