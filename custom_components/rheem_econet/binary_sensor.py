"""Binary sensors for EcoNet devices."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EconetConfigEntry
from .entity import EconetEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: EconetConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for uid, eq in coordinator.data.equipment.items():
        entities.append(ConnectedSensor(coordinator, uid))
        if isinstance(eq.value("@ACTIVE"), bool):
            entities.append(ActiveSensor(coordinator, uid))
    async_add_entities(entities)


class ConnectedSensor(EconetEntity, BinarySensorEntity):
    """Whether the device's WiFi module is online with the cloud."""

    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, uid: str) -> None:
        super().__init__(coordinator, uid, "connected")

    @property
    def available(self) -> bool:
        # Must stay available while the device is offline, or it's useless.
        return self.coordinator.last_update_success and self._uid in self.coordinator.data.equipment

    @property
    def is_on(self) -> bool:
        return self.equipment.connected


class ActiveSensor(EconetEntity, BinarySensorEntity):
    """@ACTIVE: the unit is currently heating."""

    _attr_translation_key = "heating"
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, coordinator, uid: str) -> None:
        super().__init__(coordinator, uid, "active")

    @property
    def is_on(self) -> bool:
        return bool(self.equipment.value("@ACTIVE"))
