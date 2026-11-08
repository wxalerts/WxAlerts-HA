"""Reading options that several platforms need the same way.

The event selection is read by the binary sensor platform, the device trigger
platform and the options flow, and each of them has to make the same decision
about an entry written by an older release. Doing that in one place is the
difference between "the sensors appeared but the triggers did not".
"""

from __future__ import annotations

from .const import CONF_EVENT_SENSORS, CONF_PHENOMENON_SENSORS
from .events import BY_KEY, DEFAULT_EVENT_KEYS


def selected_event_keys(entry) -> list[str]:
    """Catalogue keys the user wants binary sensors for.

    An entry from 0.3.x has no selection at all, only the boolean that switched
    the six phenomenon buckets on. Reading it as the default set means an
    upgrade keeps working automations pointed at a tornado sensor, under a new
    entity id, because the hazard it reports is genuinely narrower than the
    bucket's, and silently rebinding a tornado automation to something that
    means something else would be worse than making the break visible.
    """
    options = {**entry.data, **entry.options}
    selected = options.get(CONF_EVENT_SENSORS)
    if selected is None:
        if options.get(CONF_PHENOMENON_SENSORS, True):
            return list(DEFAULT_EVENT_KEYS)
        return []
    # Order is the catalogue's, not the order the user happened to tick them
    # in, so entity creation is deterministic across saves.
    chosen = set(selected)
    return [key for key in BY_KEY if key in chosen]
