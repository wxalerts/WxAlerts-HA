"""The NWS hazard catalogue — every alert type a user can automate on.

An automation wants to say "Tornado Warning", not "phenomenon TO". The two
are not the same question: a Tornado Watch and a Tornado Warning share the
phenomenon and mean entirely different things, so the unit of selection here
is the **event type** — phenomenon *and* significance together.

Resolving an alert to one of these is deliberately two-pass:

1. **VTEC pair first.** Products carrying P-VTEC arrive with ``phenomena`` and
   ``significance`` set, and that pair is authoritative. It also lets retired
   codes fold into their replacements, so an office still issuing ``WC.Y``
   lands on Cold Weather Advisory alongside everyone else.
2. **Name second.** CAP-only products (Special Weather Statement, Air Quality
   Alert, the civil-emergency set) never had VTEC, and the ingest publishes
   ``phenomena``/``significance`` as null for them — see ``_publish_alert_row``
   in nws-ingest. Those come straight from api.weather.gov and so carry the
   canonical event name.

The ordering matters, because the two producers disagree on wording. The
VTEC path builds its name mechanically as "<phenomenon> <significance>", which
gives "Fire Weather Warning" where NWS says "Red Flag Warning" and "Marine
Warning" where NWS says "Special Marine Warning". Matching the pair first means
those never reach the name comparison; the aliases below are a safety net for
the cases where a name is all we have.

The catalogue is a superset of ``api.weather.gov/alerts/types``: it also keeps
VTEC codes NWS has retired from that list but which offices can still issue.
An event type absent from here is not dropped — it gets a slug of its own name
and still appears in the alert list; it simply cannot be picked in the options
flow.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EventType:
    """One selectable hazard type."""

    key: str
    name: str
    # (phenomena, significance) pairs that resolve to this event. Several is
    # normal: a retired code and its replacement are the same hazard.
    vtec: tuple[tuple[str, str], ...] = ()
    # Alternate spellings seen in the wild, matched after the canonical name.
    aliases: tuple[str, ...] = ()


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _e(name: str, *vtec: str, aliases: tuple[str, ...] = ()) -> EventType:
    """Declare an event type. VTEC pairs are written "TO.W" for brevity."""
    pairs = tuple(tuple(code.split(".")) for code in vtec)  # type: ignore[misc]
    return EventType(key=_key(name), name=name, vtec=pairs, aliases=aliases)


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
# Grouped by hazard family for review; the options flow sorts alphabetically,
# which is how someone hunting for "blizzard" actually finds it.

EVENT_TYPES: tuple[EventType, ...] = (
    # --- Convective ------------------------------------------------------
    _e("Tornado Warning", "TO.W"),
    _e("Tornado Watch", "TO.A"),
    _e("Severe Thunderstorm Warning", "SV.W"),
    _e("Severe Thunderstorm Watch", "SV.A"),
    _e("Severe Weather Statement", "SV.S"),
    _e("Extreme Wind Warning", "EW.W"),
    _e("Snow Squall Warning", "SQ.W"),
    _e("Special Weather Statement"),
    _e("Hazardous Weather Outlook"),
    _e("Short Term Forecast"),
    # --- Flooding --------------------------------------------------------
    # FA and FL are both "Flood" to NWS and differ only in origin (areal vs
    # forecast point), so they fold together: an automation that wants to know
    # the house may flood does not care which office product said so.
    _e("Flash Flood Warning", "FF.W"),
    _e("Flash Flood Watch", "FF.A"),
    _e("Flash Flood Statement", "FF.S"),
    _e("Flood Warning", "FA.W", "FL.W"),
    _e("Flood Watch", "FA.A", "FL.A"),
    _e("Flood Advisory", "FA.Y", "FL.Y"),
    _e("Flood Statement", "FA.S", "FL.S"),
    _e("Hydrologic Outlook", "HY.O"),
    _e("Hydrologic Statement", "HY.S"),
    # --- Winter ----------------------------------------------------------
    _e("Blizzard Warning", "BZ.W"),
    _e("Blizzard Watch", "BZ.A"),
    _e("Winter Storm Warning", "WS.W"),
    _e("Winter Storm Watch", "WS.A"),
    _e("Winter Weather Advisory", "WW.Y"),
    _e("Ice Storm Warning", "IS.W"),
    _e("Lake Effect Snow Warning", "LE.W"),
    _e("Lake Effect Snow Watch", "LE.A"),
    _e("Lake Effect Snow Advisory", "LE.Y"),
    _e("Freezing Rain Advisory", "ZR.Y"),
    _e("Freezing Fog Advisory", "ZF.Y"),
    _e("Frost Advisory", "FR.Y"),
    _e("Freeze Warning", "FZ.W"),
    _e("Freeze Watch", "FZ.A"),
    _e("Hard Freeze Warning", "HZ.W"),
    _e("Hard Freeze Watch", "HZ.A"),
    # --- Heat and cold ---------------------------------------------------
    # NWS renamed the EH products to "Extreme Heat" in 2025 and retired the WC
    # (wind chill) family into EC and Cold Weather Advisory in 2024. Both old
    # codes still resolve here, so an office lagging the change is invisible to
    # an automation.
    _e("Extreme Heat Warning", "EH.W", aliases=("Excessive Heat Warning",)),
    _e("Extreme Heat Watch", "EH.A", aliases=("Excessive Heat Watch",)),
    _e("Heat Advisory", "HT.Y"),
    _e("Extreme Cold Warning", "EC.W", "WC.W", aliases=("Wind Chill Warning",)),
    _e("Extreme Cold Watch", "EC.A", "WC.A", aliases=("Wind Chill Watch",)),
    _e("Cold Weather Advisory", "WC.Y", aliases=("Wind Chill Advisory",)),
    # --- Wind ------------------------------------------------------------
    _e("High Wind Warning", "HW.W"),
    _e("High Wind Watch", "HW.A"),
    _e("Wind Advisory", "WI.Y"),
    _e("Lake Wind Advisory", "LW.Y"),
    # --- Tropical --------------------------------------------------------
    _e("Hurricane Warning", "HU.W"),
    _e("Hurricane Watch", "HU.A"),
    _e("Hurricane Force Wind Warning", "HF.W"),
    _e("Hurricane Force Wind Watch", "HF.A"),
    _e("Tropical Storm Warning", "TR.W"),
    _e("Tropical Storm Watch", "TR.A"),
    _e("Typhoon Warning", "TY.W"),
    _e("Typhoon Watch", "TY.A"),
    _e("Tropical Cyclone Local Statement", "HU.S", "TR.S", "TY.S"),
    _e("Storm Surge Warning", "SS.W"),
    _e("Storm Surge Watch", "SS.A"),
    # --- Coastal and surf ------------------------------------------------
    _e("Coastal Flood Warning", "CF.W"),
    _e("Coastal Flood Watch", "CF.A"),
    _e("Coastal Flood Advisory", "CF.Y"),
    _e("Coastal Flood Statement", "CF.S"),
    _e("Lakeshore Flood Warning", "LS.W"),
    _e("Lakeshore Flood Watch", "LS.A"),
    _e("Lakeshore Flood Advisory", "LS.Y"),
    _e("Lakeshore Flood Statement", "LS.S"),
    _e("High Surf Warning", "SU.W"),
    _e("High Surf Advisory", "SU.Y"),
    _e("Rip Current Statement", "RP.S"),
    _e("Beach Hazards Statement", "BH.S", aliases=("Beach Hazard Statement",)),
    _e("Tsunami Warning", "TS.W"),
    _e("Tsunami Watch", "TS.A"),
    _e("Tsunami Advisory", "TS.Y"),
    # --- Marine ----------------------------------------------------------
    _e("Special Marine Warning", "MA.W", aliases=("Marine Warning",)),
    _e("Marine Weather Statement", "MA.S", aliases=("Marine Statement",)),
    _e("Gale Warning", "GL.W"),
    _e("Gale Watch", "GL.A"),
    _e("Storm Warning", "SR.W"),
    _e("Storm Watch", "SR.A"),
    _e("Hazardous Seas Warning", "SE.W"),
    _e("Hazardous Seas Watch", "SE.A"),
    # The four small-craft advisories are separate VTEC codes with separate
    # meanings (winds, rough bar, hazardous seas), but NWS publishes only the
    # one name, so they share an event type and the alert's own headline says
    # which. Keeping them apart would offer three choices no forecast text
    # distinguishes.
    _e("Small Craft Advisory", "SC.Y", "SI.Y", "RB.Y", "SW.Y"),
    _e("Brisk Wind Advisory", "BW.Y"),
    _e("Heavy Freezing Spray Warning", "UP.W", aliases=("Freezing Spray Warning",)),
    _e("Heavy Freezing Spray Watch", "UP.A", aliases=("Freezing Spray Watch",)),
    _e("Freezing Spray Advisory", "UP.Y", "ZY.Y"),
    _e("Low Water Advisory", "LO.Y"),
    _e("Marine Dense Fog Advisory", "MF.Y"),
    _e("Marine Dense Smoke Advisory", "MS.Y"),
    _e("Marine Ashfall Advisory", "MH.Y"),
    _e("Marine Ashfall Warning", "MH.W"),
    # --- Fire, smoke, dust, air ------------------------------------------
    _e("Red Flag Warning", "FW.W", aliases=("Fire Weather Warning",)),
    _e("Fire Weather Watch", "FW.A"),
    _e("Extreme Fire Danger"),
    _e("Fire Warning"),
    _e("Dense Smoke Advisory", "SM.Y"),
    _e("Dense Fog Advisory", "FG.Y"),
    _e("Dust Storm Warning", "DS.W"),
    _e("Blowing Dust Warning", "DU.W"),
    _e("Blowing Dust Advisory", "DU.Y"),
    # A separate name NWS still publishes; it has no VTEC code of its own, so
    # only the CAP path can produce it.
    _e("Dust Advisory"),
    _e("Air Stagnation Advisory", "AS.Y"),
    _e("Air Quality Alert"),
    # --- Geophysical -----------------------------------------------------
    _e("Ashfall Warning", "AF.W"),
    _e("Ashfall Advisory", "AF.Y"),
    _e("Volcano Warning"),
    _e("Earthquake Warning"),
    _e("Avalanche Warning"),
    _e("Avalanche Watch"),
    _e("Avalanche Advisory"),
    # --- Civil emergencies (CAP only, no VTEC) ---------------------------
    _e("Civil Danger Warning"),
    _e("Civil Emergency Message"),
    _e("Evacuation Immediate"),
    _e("Shelter In Place Warning"),
    _e("Local Area Emergency"),
    _e("Hazardous Materials Warning"),
    _e("Radiological Hazard Warning"),
    _e("Nuclear Power Plant Warning"),
    _e("Law Enforcement Warning"),
    _e("Child Abduction Emergency"),
    _e("Blue Alert"),
    _e("911 Telephone Outage"),
    _e("Administrative Message"),
    _e("Test"),
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

BY_KEY: dict[str, EventType] = {event.key: event for event in EVENT_TYPES}


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


BY_VTEC: dict[tuple[str, str], EventType] = {}
BY_NAME: dict[str, EventType] = {}
for _event in EVENT_TYPES:
    for _pair in _event.vtec:
        # A pair belonging to two event types is a bug in the table above, not
        # something to resolve silently at runtime.
        assert _pair not in BY_VTEC, f"duplicate VTEC pair {_pair}"
        BY_VTEC[_pair] = _event
    for _name in (_event.name, *_event.aliases):
        assert _normalize(_name) not in BY_NAME, f"duplicate event name {_name}"
        BY_NAME[_normalize(_name)] = _event
del _event, _pair, _name  # type: ignore[name-defined]


# Sorted for the options selector: alphabetical is what someone typing
# "blizzard" into the search box expects.
SELECTABLE_EVENTS: tuple[EventType, ...] = tuple(
    sorted(EVENT_TYPES, key=lambda event: event.name)
)

# On a fresh install, the hazards that can kill someone who is asleep. Anything
# quieter is a deliberate choice rather than a default, because every enabled
# type is an entity in the registry forever.
DEFAULT_EVENT_KEYS: tuple[str, ...] = (
    "tornado_warning",
    "severe_thunderstorm_warning",
    "flash_flood_warning",
    "extreme_wind_warning",
    "hurricane_warning",
    "blizzard_warning",
    "ice_storm_warning",
    "snow_squall_warning",
    "tsunami_warning",
)
assert all(key in BY_KEY for key in DEFAULT_EVENT_KEYS)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def event_type_for(alert: Mapping[str, Any]) -> EventType | None:
    """The catalogue entry an alert belongs to, or None if it is not in it."""
    phenomena = alert.get("phenomena")
    significance = alert.get("significance")
    if phenomena and significance:
        found = BY_VTEC.get((phenomena.upper(), significance.upper()))
        if found is not None:
            return found
    name = alert.get("event")
    if name:
        return BY_NAME.get(_normalize(name))
    return None


def event_key_for(alert: Mapping[str, Any]) -> str:
    """A stable key for an alert, catalogued or not.

    An uncatalogued hazard still needs an identity — it is what the active-alert
    list reports and what a start/end transition is measured on. Falling back to
    a slug of the name keeps a new NWS product working before this table knows
    about it; only the options selector is limited to the catalogue.
    """
    found = event_type_for(alert)
    if found is not None:
        return found.key
    name = alert.get("event")
    if name:
        return _key(name)
    phenomena = alert.get("phenomena")
    significance = alert.get("significance")
    if phenomena and significance:
        return f"{phenomena.lower()}_{significance.lower()}"
    return "unknown"


def event_name_for(alert: Mapping[str, Any]) -> str:
    """The display name, preferring the catalogue's canonical spelling.

    The two producers word the same hazard differently, so a dashboard reading
    the raw field shows "Fire Weather Warning" from one path and "Red Flag
    Warning" from the other for one event.
    """
    found = event_type_for(alert)
    if found is not None:
        return found.name
    return alert.get("event") or "Unknown"
