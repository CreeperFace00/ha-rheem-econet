"""Base entity for EcoNet devices."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Equipment
from .const import DOMAIN, TESTED_DEVICE_TYPES
from .coordinator import EconetCoordinator


class EconetEntity(CoordinatorEntity[EconetCoordinator]):
    """Entity bound to one piece of EcoNet equipment."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EconetCoordinator, uid: str, key: str) -> None:
        super().__init__(coordinator)
        self._uid = uid
        self._attr_unique_id = f"{uid}_{key}"
        eq = self.equipment
        untested = eq.device_type not in TESTED_DEVICE_TYPES
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uid)},
            name=eq.name,
            manufacturer="Rheem / Ruud",
            model=eq.model + (" (untested)" if untested else ""),
            serial_number=eq.serial_number,
            suggested_area=eq.location_name,
        )

    @property
    def equipment(self) -> Equipment:
        return self.coordinator.data.equipment[self._uid]

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._uid in self.coordinator.data.equipment
            and self.equipment.connected
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.equipment.device_type not in TESTED_DEVICE_TYPES:
            return {"untested_device_type": True}
        return None
