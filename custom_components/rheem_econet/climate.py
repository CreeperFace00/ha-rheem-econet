"""Climate platform for EcoNet thermostats (device_type "HVAC").

UNTESTED: built from the app's HVAC code paths and bundled sample payloads
only; the author had no thermostat to verify against.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_AWAY,
    PRESET_HOME,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EconetConfigEntry
from .const import DEVICE_TYPE_HVAC
from .entity import EconetEntity

LABEL_TO_HVAC = {
    "heating": HVACMode.HEAT,
    "heat": HVACMode.HEAT,
    "emergency heat": HVACMode.HEAT,
    "cooling": HVACMode.COOL,
    "cool": HVACMode.COOL,
    "auto": HVACMode.HEAT_COOL,
    "fan only": HVACMode.FAN_ONLY,
    "off": HVACMode.OFF,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: EconetConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        EconetThermostat(coordinator, uid)
        for uid, eq in coordinator.data.equipment.items()
        if eq.device_type == DEVICE_TYPE_HVAC
    )


class EconetThermostat(EconetEntity, ClimateEntity):
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = 1
    _attr_preset_modes = [PRESET_HOME, PRESET_AWAY]

    def __init__(self, coordinator, uid: str) -> None:
        super().__init__(coordinator, uid, "climate")
        eq = self.equipment
        self._fan_key = next((k for k in ("@FANSPEED", "@FANMODE") if eq.has(k)), None)

    def _mode_label(self) -> str:
        return (self.equipment.enum_label("@MODE") or "").lower()

    @property
    def supported_features(self) -> ClimateEntityFeature:
        f = ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        if self.hvac_mode == HVACMode.HEAT_COOL:
            f |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        else:
            f |= ClimateEntityFeature.TARGET_TEMPERATURE
        if self._fan_key:
            f |= ClimateEntityFeature.FAN_MODE
        if self.equipment.has("@DEHUMSETPOINT"):
            f |= ClimateEntityFeature.TARGET_HUMIDITY
        return f

    @property
    def hvac_modes(self) -> list[HVACMode]:
        modes: list[HVACMode] = []
        for label in self.equipment.enum_options("@MODE"):
            mode = LABEL_TO_HVAC.get(label.lower())
            if mode and mode not in modes:
                modes.append(mode)
        return modes or [HVACMode.OFF]

    @property
    def hvac_mode(self) -> HVACMode | None:
        return LABEL_TO_HVAC.get(self._mode_label())

    @property
    def current_temperature(self) -> float | None:
        # Inference: on thermostats the template's "setpoint" display object
        # (@SETPOINT, range 0-99) is the room temperature reading, while the
        # actual targets are @HEATSETPOINT / @COOLSETPOINT.
        return self.equipment.value("@SETPOINT")

    @property
    def current_humidity(self) -> float | None:
        return self.equipment.value("@HUMIDITY")

    @property
    def target_humidity(self) -> float | None:
        return self.equipment.value("@DEHUMSETPOINT")

    @property
    def target_temperature(self) -> float | None:
        mode = self.hvac_mode
        if mode == HVACMode.COOL:
            return self.equipment.value("@COOLSETPOINT")
        if mode == HVACMode.HEAT:
            return self.equipment.value("@HEATSETPOINT")
        return None

    @property
    def target_temperature_low(self) -> float | None:
        return self.equipment.value("@HEATSETPOINT")

    @property
    def target_temperature_high(self) -> float | None:
        return self.equipment.value("@COOLSETPOINT")

    @property
    def min_temp(self) -> float:
        low, _ = self.equipment.limits("@HEATSETPOINT")
        return low if low is not None else 50

    @property
    def max_temp(self) -> float:
        _, high = self.equipment.limits("@COOLSETPOINT")
        return high if high is not None else 92

    @property
    def fan_modes(self) -> list[str] | None:
        return self.equipment.enum_options(self._fan_key) if self._fan_key else None

    @property
    def fan_mode(self) -> str | None:
        return self.equipment.enum_label(self._fan_key) if self._fan_key else None

    @property
    def preset_mode(self) -> str:
        loc = self.coordinator.data.locations.get(self.equipment.location_id)
        away = bool(self.equipment.value("@AWAY")) or (loc.away if loc else False)
        return PRESET_AWAY if away else PRESET_HOME

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        eq = self.equipment
        attrs = {
            "status": eq.value("@STATUS"),
            "schedule_status": eq.value("@SCHEDULESTATUS"),
            "model": eq.model,
        }
        return {k: v for k, v in attrs.items() if v} | (super().extra_state_attributes or {})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        values: dict[str, int] = {}
        if ATTR_TARGET_TEMP_LOW in kwargs:
            values["@HEATSETPOINT"] = int(round(kwargs[ATTR_TARGET_TEMP_LOW]))
        if ATTR_TARGET_TEMP_HIGH in kwargs:
            values["@COOLSETPOINT"] = int(round(kwargs[ATTR_TARGET_TEMP_HIGH]))
        if ATTR_TEMPERATURE in kwargs:
            key = "@COOLSETPOINT" if self.hvac_mode == HVACMode.COOL else "@HEATSETPOINT"
            values[key] = int(round(kwargs[ATTR_TEMPERATURE]))
        if values:
            await self.coordinator.async_send(self.equipment, values)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        for idx, label in enumerate(self.equipment.enum_options("@MODE")):
            if LABEL_TO_HVAC.get(label.lower()) == hvac_mode:
                await self.coordinator.async_send(self.equipment, {"@MODE": idx})
                return

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT_COOL if HVACMode.HEAT_COOL in self.hvac_modes else self.hvac_modes[0])

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        options = self.equipment.enum_options(self._fan_key)
        if fan_mode in options:
            await self.coordinator.async_send(self.equipment, {self._fan_key: options.index(fan_mode)})

    async def async_set_humidity(self, humidity: int) -> None:
        await self.coordinator.async_send(self.equipment, {"@DEHUMSETPOINT": int(humidity)})

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self.coordinator.async_set_away(
            self.equipment.location_id, preset_mode == PRESET_AWAY
        )
