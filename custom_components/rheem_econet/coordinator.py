"""Data coordinator: REST polling + MQTT push for EcoNet."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    EconetApi,
    EconetAuthError,
    EconetError,
    EconetMqtt,
    Equipment,
    Location,
    build_tls_context,
)
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_MQTT_TLS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

USAGE_REFRESH = timedelta(minutes=30)


@dataclass
class EconetData:
    locations: dict[str, Location] = field(default_factory=dict)
    equipment: dict[str, Equipment] = field(default_factory=dict)
    # unique_id -> {"total": float, "points": [...], "unit": str}
    energy_today: dict[str, dict[str, Any]] = field(default_factory=dict)


class EconetCoordinator(DataUpdateCoordinator[EconetData]):
    """Owns the API session, the MQTT connection and the device snapshot."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: EconetApi) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.entry = entry
        self.api = api
        self.mqtt: EconetMqtt | None = None
        self.mqtt_connected = False
        self._mqtt_token: str | None = None
        self._last_usage: datetime | None = None

    # ---- polling ---------------------------------------------------------
    async def _async_update_data(self) -> EconetData:
        try:
            locations = await self.api.get_locations()
        except EconetAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EconetError as err:
            raise UpdateFailed(str(err)) from err

        data = EconetData()
        for loc in locations:
            data.locations[loc.location_id] = loc
            for eq in loc.equipment:
                data.equipment[eq.unique_id] = eq

        previous = self.data.energy_today if self.data else {}
        data.energy_today = dict(previous)
        now = dt_util.utcnow()
        if self._last_usage is None or now - self._last_usage >= USAGE_REFRESH:
            self._last_usage = now
            await self._refresh_usage(data)

        # The MQTT username is the user token; after a REST re-login the old
        # session will be rejected on its next reconnect, so rebuild it.
        if self.mqtt is not None and self._mqtt_token != self.api.user_token:
            await self.hass.async_add_executor_job(self.mqtt.stop)
            self.mqtt = None
        if self.mqtt is None and self.api.environment and self.api.environment.mqtt_host:
            await self._start_mqtt()
        return data

    async def _refresh_usage(self, data: EconetData) -> None:
        start = dt_util.start_of_local_day()
        end = start + timedelta(days=1)
        for uid, eq in data.equipment.items():
            if not any(a.endswith("UsageReportView") for a in eq.actions):
                continue
            try:
                result = await self.api.get_usage(eq, start, end)
            except EconetError as err:
                _LOGGER.debug("Usage report failed for %s: %s", eq.name, err)
                continue
            energy = result.get("energy_usage") or {}
            points = energy.get("data") or []
            try:
                total = round(sum(float(p.get("value") or 0) for p in points), 3)
            except (TypeError, ValueError):
                continue
            data.energy_today[uid] = {
                "total": total,
                "points": points,
                "message": energy.get("message"),
                "configured": energy.get("configured"),
            }

    # ---- MQTT ------------------------------------------------------------
    async def _start_mqtt(self) -> None:
        env = self.api.environment
        assert env and env.mqtt_host
        verify = self.entry.options.get(CONF_VERIFY_MQTT_TLS, True)
        ctx = await self.hass.async_add_executor_job(build_tls_context, verify)
        self._mqtt_token = self.api.user_token
        self.mqtt = EconetMqtt(
            loop=self.hass.loop,
            host=env.mqtt_host,
            port=env.mqtt_port or 1884,
            account_id=self.api.account_id or "",
            user_token=self.api.user_token or "",
            system_key=self.api.system_key,
            tls_context=ctx,
            on_update=self._handle_mqtt_update,
            on_connection=self._handle_mqtt_connection,
        )
        await self.hass.async_add_executor_job(self.mqtt.start)

    async def async_shutdown(self) -> None:
        if self.mqtt:
            await self.hass.async_add_executor_job(self.mqtt.stop)
            self.mqtt = None
        await super().async_shutdown()

    @callback
    def _handle_mqtt_connection(self, connected: bool) -> None:
        self.mqtt_connected = connected
        if not connected:
            _LOGGER.debug("EcoNet MQTT offline; falling back to polling")

    @callback
    def _handle_mqtt_update(self, payload: dict[str, Any]) -> None:
        if not self.data:
            return
        name = str(payload.get("device_name", ""))
        serial = str(payload.get("serial_number", ""))
        eq = self.data.equipment.get(f"{name}_{serial}")
        if eq is None:
            _LOGGER.debug("MQTT update for unknown device %s/%s", name, serial)
            return
        if eq.apply_update(payload):
            self.async_set_updated_data(self.data)

    # ---- commands --------------------------------------------------------
    async def async_send(self, eq: Equipment, values: dict[str, Any]) -> None:
        """Publish a desired-state change and apply it optimistically."""
        if not self.mqtt or not self.mqtt_connected:
            raise HomeAssistantError(
                "EcoNet MQTT connection is not available; cannot send commands"
            )
        try:
            await self.hass.async_add_executor_job(self.mqtt.publish, eq, values)
        except EconetError as err:
            raise HomeAssistantError(str(err)) from err
        eq.apply_update(values)
        self.async_set_updated_data(self.data)

    async def async_set_away(self, location_id: str, away: bool) -> None:
        try:
            await self.api.set_away(location_id, away)
        except EconetError as err:
            raise HomeAssistantError(str(err)) from err
        loc = self.data.locations.get(location_id)
        if loc:
            loc.raw["@AWAY"] = away
        self.async_set_updated_data(self.data)
