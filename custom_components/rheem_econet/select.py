"""Select entities for writable enum settings on EcoNet devices.

UNTESTED: these objects were found in the app's sample payloads for models
the author does not own (Niagara, Triton, Hotspring, control centers). They
are written the same way the app writes every enum: ``{"@KEY": index}``.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EconetConfigEntry
from .entity import EconetEntity

# key -> (name, icon, enabled by default)
WRITABLE_ENUMS: dict[str, tuple[str, str, bool]] = {
    "@ALTITUDE": ("Altitude", "mdi:image-filter-hdr", True),
    "@SAVEWATER": ("Save water", "mdi:water-check", True),
    "@RPUMPINSTANCE": ("Recirculation pump", "mdi:pump", True),
    "@LEAKACTION": ("Leak action", "mdi:pipe-leak", True),
    "@DEHUMENABLE": ("Dehumidifier", "mdi:air-humidifier", True),
    "@BEEP": ("Beep", "mdi:volume-high", False),
    "@SCREENLOCK": ("Screen lock", "mdi:lock", False),
    # Closing the shutoff valve disables the water heater (app shows a confirm
    # dialog for this). Disabled by default so it can't be toggled by accident.
    "@VALVE": ("Shutoff valve", "mdi:valve", False),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: EconetConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        EnumSelect(coordinator, uid, key)
        for uid, eq in coordinator.data.equipment.items()
        for key in WRITABLE_ENUMS
        if eq.enum_options(key)
    )


class EnumSelect(EconetEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, uid: str, key: str) -> None:
        super().__init__(coordinator, uid, key.lstrip("@").lower())
        self._key = key
        name, icon, enabled = WRITABLE_ENUMS[key]
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_registry_enabled_default = enabled

    @property
    def options(self) -> list[str]:
        return self.equipment.enum_options(self._key)

    @property
    def current_option(self) -> str | None:
        return self.equipment.enum_label(self._key)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_send(
            self.equipment, {self._key: self.options.index(option)}
        )
