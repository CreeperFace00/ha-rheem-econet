"""Rheem / Ruud EcoNet integration (reverse-engineered from the Android app)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EconetApi, EconetAuthError, EconetError
from .coordinator import EconetCoordinator

PLATFORMS = [
    Platform.WATER_HEATER,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
]

type EconetConfigEntry = ConfigEntry[EconetCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EconetConfigEntry) -> bool:
    api = EconetApi(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
    )
    try:
        await api.login()
    except EconetAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except EconetError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = EconetCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: EconetConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EconetConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
