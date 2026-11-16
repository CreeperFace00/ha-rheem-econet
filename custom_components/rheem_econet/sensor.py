"""Sensor platform for EcoNet devices.

Most sensors are generated from whatever @OBJECTs a device reports, so models
other than the author's water heater get sensors too (flagged as untested via
the device model / attributes).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EconetConfigEntry
from .api import Equipment, parse_hot_water_icon
from .entity import EconetEntity

# Objects consumed by the water_heater / climate entities, or meaningless as sensors.
SKIP_NUMERIC = {
    "@SETPOINT",
    "@HEATSETPOINT",
    "@COOLSETPOINT",
    "@DEHUMSETPOINT",
    "@NAME",
    "@SETPTMAX",
    "@DEADBAND",
}

# Known numeric objects: key -> (friendly name, icon, diagnostic?)
KNOWN_NUMERIC: dict[str, tuple[str, str | None, bool]] = {
    "@TANK": ("Tank level", "mdi:water-percent", False),
    "@COMBUSTION": ("Combustion", "mdi:fire", True),
    "@HUMIDITY": ("Humidity", None, False),
    "@HOURS": ("Run time", "mdi:timer-outline", True),
    "@BURNTIME": ("Burn time", "mdi:timer-outline", True),
    "@CYCLES": ("Heating cycles", "mdi:counter", True),
}

HEAT_PUMP_HEALTH: dict[str, tuple[str, str]] = {
    "@TANK": ("Element health", "mdi:heating-coil"),
    "@COMBUSTION": ("Compressor health", "mdi:heat-pump-outline"),
}

# Plain-text status objects worth surfacing.
TEXT_OBJECTS: dict[str, tuple[str, str]] = {
    "@RUNNING": ("Running status", "mdi:information-outline"),
    "@STATUS": ("Status", "mdi:information-outline"),
    "@OPERATION": ("Operation", "mdi:information-outline"),
    "@SCHEDULESTATUS": ("Schedule status", "mdi:calendar-clock"),
    "@RCOPERATIONS": ("Recirculation", "mdi:pump"),
    "@RPUMPMODE": ("Recirculation pump mode", "mdi:pump"),
}

UNIT_MAP = {
    "%": PERCENTAGE,
    "deg f": UnitOfTemperature.FAHRENHEIT,
    "hrs": UnitOfTime.HOURS,
    "min": UnitOfTime.MINUTES,
}


def _is_numeric_object(eq: Equipment, key: str) -> bool:
    item = eq.obj(key)
    if not isinstance(item, dict) or "constraints" not in item:
        return False
    if eq.constraints(key).get("enumText"):
        return False
    return isinstance(item.get("value"), (int, float)) and not isinstance(item.get("value"), bool)


async def async_setup_entry(
    hass: HomeAssistant, entry: EconetConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for uid, eq in coordinator.data.equipment.items():
        if eq.has("@ALERTCOUNT"):
            entities.append(AlertCountSensor(coordinator, uid))
        if eq.has("@HOTWATER"):
            entities.append(HotWaterSensor(coordinator, uid))
        for key in eq.raw:
            if not key.startswith("@") or key in SKIP_NUMERIC:
                continue
            if key == "@HUMIDITY" and eq.device_type == "HVAC":
                continue  # exposed on the climate entity
            if _is_numeric_object(eq, key):
                entities.append(NumericObjectSensor(coordinator, uid, key))
            elif key in TEXT_OBJECTS and isinstance(eq.value(key), str):
                entities.append(TextObjectSensor(coordinator, uid, key))
        if any(a.endswith("UsageReportView") for a in eq.actions):
            entities.append(EnergyTodaySensor(coordinator, uid))
    async_add_entities(entities)


class AlertCountSensor(EconetEntity, SensorEntity):
    _attr_translation_key = "alert_count"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, uid: str) -> None:
        super().__init__(coordinator, uid, "alert_count")

    @property
    def native_value(self) -> int | None:
        return self.equipment.value("@ALERTCOUNT")


class HotWaterSensor(EconetEntity, SensorEntity):
    """Remaining hot water; some models only report it as an icon filename."""

    _attr_translation_key = "hot_water"
    _attr_icon = "mdi:water-thermometer"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, uid: str) -> None:
        super().__init__(coordinator, uid, "hot_water")

    @property
    def native_value(self) -> int | None:
        val = self.equipment.value("@HOTWATER")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return int(val)
        return parse_hot_water_icon(val)


class NumericObjectSensor(EconetEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, uid: str, key: str) -> None:
        super().__init__(coordinator, uid, key.lstrip("@").lower())
        self._key = key
        name, icon, diagnostic = KNOWN_NUMERIC.get(
            key, (key.lstrip("@").replace("_", " ").title(), None, True)
        )
        units = str(self.equipment.constraints(key).get("units", "")).strip().lower()
        self._attr_native_unit_of_measurement = UNIT_MAP.get(units)
        # Heat pump water heaters reuse these two objects for the app's
        # "health" screen: a 0-100 score with a status text such as
        # "Compressor life is normal" (seen live on heatpumpWaterHeaterGen5,
        # where @COMBUSTION even claims units "HRS").
        if key in HEAT_PUMP_HEALTH and self.equipment.model.lower().startswith("heatpump"):
            name, icon = HEAT_PUMP_HEALTH[key]
            diagnostic = True
            self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_name = name
        self._attr_icon = icon
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if key not in KNOWN_NUMERIC:
            self._attr_entity_registry_enabled_default = False
        if self._attr_native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
        elif key == "@HUMIDITY":
            self._attr_device_class = SensorDeviceClass.HUMIDITY
        elif key == "@CYCLES":
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        if self._attr_native_unit_of_measurement in (UnitOfTime.HOURS, UnitOfTime.MINUTES):
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        return self.equipment.value(self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs = dict(super().extra_state_attributes or {})
        status = str(self.equipment.status(self._key) or "").strip()
        if status:
            attrs["status"] = status
        return attrs or None


class TextObjectSensor(EconetEntity, SensorEntity):
    def __init__(self, coordinator, uid: str, key: str) -> None:
        super().__init__(coordinator, uid, key.lstrip("@").lower())
        self._key = key
        self._attr_name, self._attr_icon = TEXT_OBJECTS[key]

    @property
    def native_value(self) -> str | None:
        val = self.equipment.value(self._key)
        return str(val).strip() or None if val is not None else None


class EnergyTodaySensor(EconetEntity, SensorEntity):
    """Today's energy use from the app's usage report (experimental).

    Sums the points returned by dynamicAction/<type>UsageReportView for the
    window [local midnight, next midnight].
    """

    _attr_translation_key = "energy_today"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator, uid: str) -> None:
        super().__init__(coordinator, uid, "energy_today")

    @property
    def available(self) -> bool:
        return super().available and self._uid in self.coordinator.data.energy_today

    @property
    def native_value(self) -> float | None:
        info = self.coordinator.data.energy_today.get(self._uid)
        return info["total"] if info else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self.coordinator.data.energy_today.get(self._uid) or {}
        return {"points": info.get("points"), "configured": info.get("configured")}
