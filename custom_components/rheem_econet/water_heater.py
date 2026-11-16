"""Water heater platform (device_type "WH")."""

from __future__ import annotations

from typing import Any

from homeassistant.components.water_heater import (
    STATE_ECO,
    STATE_ELECTRIC,
    STATE_HEAT_PUMP,
    STATE_HIGH_DEMAND,
    STATE_PERFORMANCE,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, STATE_ON, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from . import EconetConfigEntry
from .const import DEVICE_TYPE_WATER_HEATER
from .entity import EconetEntity

# EcoNet label (stripped, lower-case) -> HA operation state. Unknown labels are
# slugified so every mode the device advertises stays selectable.
LABEL_TO_STATE = {
    "off": STATE_OFF,
    "disabled": STATE_OFF,
    "enabled": STATE_ON,
    "energy saver": STATE_ECO,
    "heat pump": STATE_HEAT_PUMP,
    "high demand": STATE_HIGH_DEMAND,
    "electric": STATE_ELECTRIC,
    "electric/gas": STATE_ELECTRIC,  # heatpumpWaterHeaterGen5, seen live
    "performance": STATE_PERFORMANCE,
}
OFF_LABELS = {"off", "disabled"}
# Preferred mode when turning on, most efficient first.
ON_PREFERENCE = ["energy saver", "heat pump", "enabled", "normal", "standard"]


async def async_setup_entry(
    hass: HomeAssistant, entry: EconetConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        EconetWaterHeater(coordinator, uid)
        for uid, eq in coordinator.data.equipment.items()
        if eq.device_type == DEVICE_TYPE_WATER_HEATER and eq.has("@SETPOINT")
    )


class EconetWaterHeater(EconetEntity, WaterHeaterEntity):
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT  # wire format is always °F
    _attr_precision = 1.0

    def __init__(self, coordinator, uid: str) -> None:
        super().__init__(coordinator, uid, "water_heater")
        eq = self.equipment
        # Gen4/Gen5 heat pumps expose @MODE; simpler units only @ENABLED.
        self._mode_key = "@MODE" if eq.has("@MODE") else ("@ENABLED" if eq.has("@ENABLED") else None)

    # ---- mode helpers ----------------------------------------------------
    def _labels(self) -> list[str]:
        return self.equipment.enum_options(self._mode_key) if self._mode_key else []

    @staticmethod
    def _to_state(label: str) -> str:
        return LABEL_TO_STATE.get(label.lower(), slugify(label))

    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        features = WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.AWAY_MODE
        if self._labels():
            features |= WaterHeaterEntityFeature.OPERATION_MODE
            if any(l.lower() in OFF_LABELS for l in self._labels()):
                features |= WaterHeaterEntityFeature.ON_OFF
        return features

    @property
    def operation_list(self) -> list[str] | None:
        labels = self._labels()
        return [self._to_state(l) for l in labels] if labels else None

    @property
    def current_operation(self) -> str | None:
        if not self._mode_key:
            return None
        label = self.equipment.enum_label(self._mode_key)
        return self._to_state(label) if label else None

    # ---- temperatures ----------------------------------------------------
    @property
    def target_temperature(self) -> float | None:
        return self.equipment.value("@SETPOINT")

    @property
    def min_temp(self) -> float:
        low, _ = self.equipment.limits("@SETPOINT")
        return low if low is not None else 90

    @property
    def max_temp(self) -> float:
        _, high = self.equipment.limits("@SETPOINT")
        return high if high is not None else 140

    @property
    def is_away_mode_on(self) -> bool | None:
        loc = self.coordinator.data.locations.get(self.equipment.location_id)
        return bool(self.equipment.value("@AWAY")) or (loc.away if loc else False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        eq = self.equipment
        attrs: dict[str, Any] = {
            "econet_mode": eq.enum_label(self._mode_key) if self._mode_key else None,
            "running": eq.value("@RUNNING"),
            "active": eq.value("@ACTIVE"),
            "model": eq.model,
            "alert_count": eq.value("@ALERTCOUNT"),
        }
        warn = eq.constraints("@SETPOINT").get("warning") or []
        if warn:
            attrs["scald_warning_above"] = warn[0].get("value")
        return {k: v for k, v in attrs.items() if v is not None} | (
            super().extra_state_attributes or {}
        )

    # ---- commands --------------------------------------------------------
    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        value = int(round(max(self.min_temp, min(self.max_temp, float(temp)))))
        await self.coordinator.async_send(self.equipment, {"@SETPOINT": value})

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        for idx, label in enumerate(self._labels()):
            if self._to_state(label) == operation_mode:
                await self.coordinator.async_send(self.equipment, {self._mode_key: idx})
                return
        raise ServiceValidationError(f"Unsupported operation mode: {operation_mode}")

    async def async_turn_off(self, **kwargs: Any) -> None:
        for idx, label in enumerate(self._labels()):
            if label.lower() in OFF_LABELS:
                await self.coordinator.async_send(self.equipment, {self._mode_key: idx})
                return

    async def async_turn_on(self, **kwargs: Any) -> None:
        labels = [l.lower() for l in self._labels()]
        for wanted in ON_PREFERENCE:
            if wanted in labels:
                await self.coordinator.async_send(
                    self.equipment, {self._mode_key: labels.index(wanted)}
                )
                return
        for idx, label in enumerate(labels):
            if label not in OFF_LABELS:
                await self.coordinator.async_send(self.equipment, {self._mode_key: idx})
                return

    async def async_turn_away_mode_on(self) -> None:
        await self.coordinator.async_set_away(self.equipment.location_id, True)

    async def async_turn_away_mode_off(self) -> None:
        await self.coordinator.async_set_away(self.equipment.location_id, False)
