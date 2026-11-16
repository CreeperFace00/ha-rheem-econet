"""End-to-end setup test with the cloud mocked out."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rheem_econet.api import Environment, Equipment, Location
from custom_components.rheem_econet.const import DOMAIN

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "locations.json").read_text())


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _locations() -> list[Location]:
    data = json.loads(json.dumps(FIXTURE))
    out = []
    for raw in data["results"]["locations"]:
        loc = Location(raw=raw)
        loc.equipment = [Equipment(raw=e, location_id=loc.location_id, location_name="Home") for e in raw["equiptments"]]
        out.append(loc)
    return out


@pytest.fixture
def mock_api():
    with patch("custom_components.rheem_econet.EconetApi", autospec=True) as cls:
        api = cls.return_value
        api.login = AsyncMock()
        api.get_locations = AsyncMock(side_effect=lambda: _locations())
        api.get_usage = AsyncMock(return_value={"energy_usage": {"data": [{"name": "h1", "value": 1.25}, {"name": "h2", "value": 0.5}]}})
        api.set_away = AsyncMock()
        api.environment = Environment("Production", "k", "s", "broker.example", 1884)
        api.account_id = "acct"
        api.user_token = "tok"
        api.system_key = "k"
        yield api


@pytest.fixture
def mock_mqtt():
    with patch("custom_components.rheem_econet.coordinator.EconetMqtt") as cls:
        inst = cls.return_value
        inst.publish = MagicMock(return_value="ANDROID_x")
        yield cls, inst


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

    hass.config.units = US_CUSTOMARY_SYSTEM  # assertions below are in °F
    entry =MockConfigEntry(domain=DOMAIN, data={CONF_EMAIL: "a@b.c", CONF_PASSWORD: "pw"}, unique_id="acct")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_creates_entities(hass: HomeAssistant, mock_api, mock_mqtt) -> None:
    cls, _ = mock_mqtt
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    coordinator._handle_mqtt_connection(True)

    ids = sorted(e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
    print("\n".join(ids))
    wh = hass.states.get("water_heater.home_heat_pump_water_heater")
    assert wh is not None
    assert wh.attributes["operation_mode"] == "heat_pump"
    assert wh.attributes["operation_list"] == ["off", "eco", "heat_pump", "high_demand", "electric"]
    assert wh.attributes["temperature"] == 127
    assert hass.states.get("sensor.home_heat_pump_water_heater_hot_water_available").state == "40"
    assert hass.states.get("sensor.home_heat_pump_water_heater_energy_today").state == "1.75"
    assert hass.states.get("binary_sensor.home_heat_pump_water_heater_heating").state == "on"
    assert hass.states.get("sensor.home_triton_tank_level").state == "100"
    assert hass.states.get("select.home_triton_leak_action").state == "Alarm Only"
    climate = hass.states.get("climate.home_thermostat")
    assert climate.state == "heat_cool"
    assert climate.attributes["current_temperature"] == 72
    assert climate.attributes["untested_device_type"] is True
    # MQTT is started from the Production environment's cloud_url/cloud_port.
    assert cls.call_args.kwargs["host"] == "broker.example"
    assert cls.call_args.kwargs["port"] == 1884


async def test_water_heater_commands(hass: HomeAssistant, mock_api, mock_mqtt) -> None:
    _, mqtt = mock_mqtt
    entry = await _setup(hass)
    entry.runtime_data._handle_mqtt_connection(True)
    ent = "water_heater.home_heat_pump_water_heater"

    await hass.services.async_call("water_heater", "set_temperature", {"entity_id": ent, "temperature": 125}, blocking=True)
    eq, values = mqtt.publish.call_args.args
    assert eq.device_name == "1111222233334444" and values == {"@SETPOINT": 125}

    await hass.services.async_call("water_heater", "set_operation_mode", {"entity_id": ent, "operation_mode": "eco"}, blocking=True)
    assert mqtt.publish.call_args.args[1] == {"@MODE": 1}
    assert hass.states.get(ent).attributes["operation_mode"] == "eco"  # optimistic

    await hass.services.async_call("water_heater", "turn_off", {"entity_id": ent}, blocking=True)
    assert mqtt.publish.call_args.args[1] == {"@MODE": 0}

    await hass.services.async_call("water_heater", "set_away_mode", {"entity_id": ent, "away_mode": True}, blocking=True)
    mock_api.set_away.assert_awaited_with("loc-1", True)


async def test_mqtt_push_updates_state(hass: HomeAssistant, mock_api, mock_mqtt) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    coordinator._handle_mqtt_update(
        {"device_name": "1111222233334444", "serial_number": "09-0c-12-13-0b-05-47-21-01", "@SETPOINT": 118, "@CONNECTED": True}
    )
    await hass.async_block_till_done()
    assert hass.states.get("water_heater.home_heat_pump_water_heater").attributes["temperature"] == 118


async def test_commands_fail_cleanly_without_mqtt(hass: HomeAssistant, mock_api, mock_mqtt) -> None:
    from homeassistant.exceptions import HomeAssistantError

    await _setup(hass)  # MQTT never reports connected
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "water_heater", "set_temperature",
            {"entity_id": "water_heater.home_heat_pump_water_heater", "temperature": 120}, blocking=True,
        )


async def test_thermostat_commands(hass: HomeAssistant, mock_api, mock_mqtt) -> None:
    _, mqtt = mock_mqtt
    entry = await _setup(hass)
    entry.runtime_data._handle_mqtt_connection(True)
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": "climate.home_thermostat", "target_temp_low": 68, "target_temp_high": 76}, blocking=True,
    )
    assert mqtt.publish.call_args.args[1] == {"@HEATSETPOINT": 68, "@COOLSETPOINT": 76}
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": "climate.home_thermostat", "hvac_mode": "cool"}, blocking=True)
    assert mqtt.publish.call_args.args[1] == {"@MODE": 1}
    await hass.services.async_call("climate", "set_fan_mode", {"entity_id": "climate.home_thermostat", "fan_mode": "High"}, blocking=True)
    assert mqtt.publish.call_args.args[1] == {"@FANSPEED": 3}


async def test_metric_install_sends_fahrenheit(hass: HomeAssistant, mock_api, mock_mqtt) -> None:
    from homeassistant.util.unit_system import METRIC_SYSTEM

    _, mqtt = mock_mqtt
    entry = await _setup(hass)
    hass.config.units = METRIC_SYSTEM
    entry.runtime_data._handle_mqtt_connection(True)
    entry.runtime_data.async_set_updated_data(entry.runtime_data.data)
    await hass.async_block_till_done()
    ent = "water_heater.home_heat_pump_water_heater"
    assert round(hass.states.get(ent).attributes["temperature"]) == 53  # 127 °F
    await hass.services.async_call("water_heater", "set_temperature", {"entity_id": ent, "temperature": 50}, blocking=True)
    assert mqtt.publish.call_args.args[1] == {"@SETPOINT": 122}


async def test_live_heat_pump_gen5_payload(hass: HomeAssistant, mock_api, mock_mqtt, monkeypatch) -> None:
    """Sanitized getUserDataForApp capture from a real heatpumpWaterHeaterGen5."""
    import sys

    live = json.loads((Path(__file__).parent / "fixtures" / "heatpump_gen5_live.json").read_text())
    monkeypatch.setattr(sys.modules[__name__], "FIXTURE", live)
    _, mqtt = mock_mqtt
    entry = await _setup(hass)
    entry.runtime_data._handle_mqtt_connection(True)
    ent = "water_heater.home_heat_pump_water_heater"
    wh = hass.states.get(ent)
    assert wh.attributes["operation_list"] == ["off", "eco", "heat_pump", "high_demand", "electric", "vacation"]
    assert wh.attributes["operation_mode"] == "heat_pump"
    assert wh.attributes["temperature"] == 125
    assert (wh.attributes["min_temp"], wh.attributes["max_temp"]) == (110, 140)
    assert hass.states.get("sensor.home_heat_pump_water_heater_hot_water_available").state == "100"
    health = hass.states.get("sensor.home_heat_pump_water_heater_compressor_health")
    assert health.state == "100" and health.attributes["status"] == "Compressor life is normal"
    assert health.attributes["unit_of_measurement"] == "%"
    assert hass.states.get("sensor.home_heat_pump_water_heater_element_health").state == "100"
    # No shut-off valve installed: its object has no enum, so no select is created.
    assert hass.states.get("select.home_heat_pump_water_heater_shutoff_valve") is None

    # The echo the cloud really sends back after a command (captured live).
    entry.runtime_data._handle_mqtt_update(
        {"@SETPOINT": 124, "@ACTIVE": True, "@STATUS": "", "device_name": "5555666677778888",
         "serial_number": "0a-00-00-00-00-00-00-00-01", "transactionId": "ANDROID_2026-11-16T21:51:02"}
    )
    await hass.async_block_till_done()
    assert hass.states.get(ent).attributes["temperature"] == 124
    await hass.services.async_call("water_heater", "set_operation_mode", {"entity_id": ent, "operation_mode": "vacation"}, blocking=True)
    assert mqtt.publish.call_args.args[1] == {"@MODE": 5}
