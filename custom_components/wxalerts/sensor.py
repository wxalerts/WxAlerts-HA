"""Sensors per place: highest live severity, active-alert count, top alert,
storm cell ETA and storm cell threat. One account-wide sensor for feed age.

The alert count sensor carries the full alert list in attributes; that is what
a Markdown card renders.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WxAlertsConfigEntry
from .const import (
    COND_CONDITION,
    COND_CONDITION_CODE,
    COND_DEWPOINT_C,
    COND_FEELS_LIKE_C,
    COND_HUMIDITY,
    COND_PRECIP_LAST_HOUR_MM,
    COND_PRESSURE_HPA,
    COND_TEMPERATURE_C,
    COND_VISIBILITY_KM,
    COND_WIND_DIRECTION_DEG,
    COND_WIND_GUST_KMH,
    COND_WIND_SPEED_KMH,
    SEVERITY_NONE,
    SEVERITY_ORDER,
    STORM_ID,
    STORM_RUNG,
    STORM_RUNG_LABEL,
    THREAT_NONE,
    THREAT_OPTIONS,
)
from .coordinator import Place, WxAlertsCoordinator, threat_for_rung
from .entity import (
    WxAlertsAlertEntity,
    WxAlertsConditionsEntity,
    WxAlertsServiceEntity,
    WxAlertsStormEntity,
)
from .events import event_key_for, event_name_for


# Wind direction got its own device and state class in recent releases; on an
# older Home Assistant the sensor is a plain degrees reading.
_WIND_DIRECTION = getattr(SensorDeviceClass, "WIND_DIRECTION", None)
_MEASUREMENT_ANGLE = getattr(SensorStateClass, "MEASUREMENT_ANGLE", None)


def _measurement(
    key: str,
    field: str,
    device_class: SensorDeviceClass | None,
    unit: str,
    precision: int,
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
) -> SensorEntityDescription:
    # The field rides in the description's key and the translation key is the
    # entity's name; native units are metric and Home Assistant converts.
    return SensorEntityDescription(
        key=field,
        translation_key=key,
        device_class=device_class,
        native_unit_of_measurement=unit,
        state_class=state_class,
        suggested_display_precision=precision,
    )


CONDITION_SENSORS: tuple[SensorEntityDescription, ...] = (
    _measurement("temperature", COND_TEMPERATURE_C, SensorDeviceClass.TEMPERATURE,
                 UnitOfTemperature.CELSIUS, 1),
    _measurement("dew_point", COND_DEWPOINT_C, SensorDeviceClass.TEMPERATURE,
                 UnitOfTemperature.CELSIUS, 1),
    _measurement("feels_like", COND_FEELS_LIKE_C, SensorDeviceClass.TEMPERATURE,
                 UnitOfTemperature.CELSIUS, 1),
    _measurement("humidity", COND_HUMIDITY, SensorDeviceClass.HUMIDITY,
                 PERCENTAGE, 0),
    _measurement("wind_speed", COND_WIND_SPEED_KMH, SensorDeviceClass.WIND_SPEED,
                 UnitOfSpeed.KILOMETERS_PER_HOUR, 0),
    _measurement("wind_gust", COND_WIND_GUST_KMH, SensorDeviceClass.WIND_SPEED,
                 UnitOfSpeed.KILOMETERS_PER_HOUR, 0),
    _measurement("wind_bearing", COND_WIND_DIRECTION_DEG, _WIND_DIRECTION,
                 DEGREE, 0, _MEASUREMENT_ANGLE),
    _measurement("pressure", COND_PRESSURE_HPA, SensorDeviceClass.ATMOSPHERIC_PRESSURE,
                 UnitOfPressure.HPA, 1),
    _measurement("visibility", COND_VISIBILITY_KM, SensorDeviceClass.DISTANCE,
                 UnitOfLength.KILOMETERS, 1),
    _measurement("precipitation_last_hour", COND_PRECIP_LAST_HOUR_MM,
                 SensorDeviceClass.PRECIPITATION, UnitOfPrecipitationDepth.MILLIMETERS, 1),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WxAlertsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    def _for_places(places: list[Place]) -> list[SensorEntity]:
        entities: list[SensorEntity] = []
        for place in places:
            entities.append(HighestSeveritySensor(coordinator, place))
            entities.append(AlertCountSensor(coordinator, place))
            entities.append(TopAlertSensor(coordinator, place))
            entities.append(StormEtaSensor(coordinator, place))
            entities.append(StormThreatSensor(coordinator, place))
            entities.extend(
                ConditionSensor(coordinator, place, description)
                for description in CONDITION_SENSORS
            )
            entities.append(ConditionTextSensor(coordinator, place))
        return entities

    @callback
    def _places_added(places: list[Place]) -> None:
        async_add_entities(_for_places(places))

    entry.async_on_unload(coordinator.add_place_listener(_places_added))
    async_add_entities(
        [FeedAgeSensor(coordinator), *_for_places(list(coordinator.places.values()))]
    )


class HighestSeveritySensor(WxAlertsAlertEntity, SensorEntity):
    """Extreme / Severe / Moderate / Minor / None: dashboard colour fuel."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [*SEVERITY_ORDER, SEVERITY_NONE]
    _attr_translation_key = "highest_severity"

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "severity")

    @property
    def native_value(self) -> str:
        return self.coordinator.highest_severity(self.same)


class AlertCountSensor(WxAlertsAlertEntity, SensorEntity):
    """Number of live hazards; the full list rides in attributes."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "alert_count"
    _attr_native_unit_of_measurement = "alerts"

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "count")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.alerts_for(self.same))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        live = self.coordinator.alerts_for(self.same)
        alerts = [
            {
                "id": alert.get("id"),
                "vtec": alert.get("vtec"),
                # The canonical name and the key an automation selects on. The
                # raw field is kept beside them because the two feed paths word
                # the same hazard differently.
                "event": event_name_for(alert),
                "event_type": event_key_for(alert),
                "event_raw": alert.get("event"),
                "phenomena": alert.get("phenomena"),
                "significance": alert.get("significance"),
                "severity": alert.get("severity"),
                "urgency": alert.get("urgency"),
                "certainty": alert.get("certainty"),
                "headline": alert.get("headline"),
                "instruction": alert.get("instruction"),
                "onset": alert.get("onset"),
                "ends": alert.get("ends"),
                "action": alert.get("action"),
                "geometry_source": alert.get("geometry_source"),
            }
            for alert in live
        ]
        place = self.place
        return {
            "place_id": self.place_id,
            "place_name": place.name if place else None,
            "same": self.same,
            "events": [alert["event"] for alert in alerts],
            "event_types": [alert["event_type"] for alert in alerts],
            "alerts": alerts,
        }


class TopAlertSensor(WxAlertsAlertEntity, SensorEntity):
    """The most urgent hazard in effect, by name.

    Not an ENUM device class: the catalogue is a superset of what NWS publishes
    today and an uncatalogued product still has to be reportable.
    """

    _attr_translation_key = "top_alert"
    _attr_icon = "mdi:weather-lightning-rainy"

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "top")

    @property
    def native_value(self) -> str:
        alerts = self.coordinator.alerts_for(self.same)
        return event_name_for(alerts[0]) if alerts else SEVERITY_NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alerts = self.coordinator.alerts_for(self.same)
        top = alerts[0] if alerts else None
        place = self.place
        return {
            "place_id": self.place_id,
            "place_name": place.name if place else None,
            "same": self.same,
            "event_type": event_key_for(top) if top else None,
            "severity": top.get("severity") if top else SEVERITY_NONE,
            "headline": top.get("headline") if top else None,
            "instruction": top.get("instruction") if top else None,
            "onset": top.get("onset") if top else None,
            "ends": top.get("ends") if top else None,
            "alert_count": len(alerts),
        }


class StormEtaSensor(WxAlertsStormEntity, SensorEntity):
    """Minutes until the most threatening storm cell arrives.

    Counts down between messages, and is unknown with no storm: "no storm"
    and "a storm zero minutes away" are different answers.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0
    _attr_translation_key = "storm_cell_eta"

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "storm_eta")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.storm_eta(self.storm)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        storm = self.storm or {}
        return {"place_id": self.place_id, "storm_id": storm.get(STORM_ID)}


class StormThreatSensor(WxAlertsStormEntity, SensorEntity):
    """The rung of the most threatening storm cell, as an enum."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = THREAT_OPTIONS
    _attr_translation_key = "storm_cell_threat"

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "storm_threat")

    @property
    def native_value(self) -> str:
        storm = self.storm
        return threat_for_rung(storm.get(STORM_RUNG)) if storm else THREAT_NONE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        storm = self.storm or {}
        return {
            "place_id": self.place_id,
            "storm_id": storm.get(STORM_ID),
            "rung": storm.get(STORM_RUNG),
            "rung_label": storm.get(STORM_RUNG_LABEL),
        }


class FeedAgeSensor(WxAlertsServiceEntity, SensorEntity):
    """Seconds since the server's status heartbeat was written.

    Keeps rising on its own when nothing arrives, which is the point: a feed
    that stopped has to look different from a quiet day.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 0
    _attr_translation_key = "feed_age"

    def __init__(self, coordinator: WxAlertsCoordinator) -> None:
        super().__init__(coordinator, "feed_age")

    @property
    def native_value(self) -> int | None:
        age = self.coordinator.feed_age
        return None if age is None else round(age)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"online": self.coordinator.feed_online}


class ConditionSensor(WxAlertsConditionsEntity, SensorEntity):
    """One field of the latest observation near the place."""

    def __init__(
        self,
        coordinator: WxAlertsCoordinator,
        place: Place,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, place, f"conditions_{description.translation_key}")
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        value = (self.observation or {}).get(self.entity_description.key)
        if isinstance(value, bool) or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class ConditionTextSensor(WxAlertsConditionsEntity, SensorEntity):
    """The observed weather in words, with the stable code beside it.

    Text rather than an enum: stations report a long tail of phrasings and an
    unlisted one must not become an invalid state.
    """

    _attr_translation_key = "condition"
    _attr_icon = "mdi:weather-partly-cloudy"

    def __init__(self, coordinator: WxAlertsCoordinator, place: Place) -> None:
        super().__init__(coordinator, place, "conditions_condition")

    @property
    def native_value(self) -> str | None:
        return (self.observation or {}).get(COND_CONDITION)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **super().extra_state_attributes,
            "condition_code": (self.observation or {}).get(COND_CONDITION_CODE),
        }
