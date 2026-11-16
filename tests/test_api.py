"""Protocol-layer tests (no Home Assistant needed beyond imports)."""

from __future__ import annotations

import json
import ssl
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from custom_components.rheem_econet.api import (
    EconetMqtt,
    Equipment,
    _iso_utc,
    parse_hot_water_icon,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "locations.json").read_text())


def _eq(index: int) -> Equipment:
    loc = FIXTURE["results"]["locations"][0]
    return Equipment(raw=json.loads(json.dumps(loc["equiptments"][index])), location_id="loc-1", location_name="Home")


def test_equipment_accessors():
    eq = _eq(0)
    assert eq.name == "Heat Pump Water Heater"
    assert eq.model == "heatpumpWaterHeaterGen5"
    assert eq.value("@SETPOINT") == 127
    assert eq.limits("@SETPOINT") == (110, 140)
    assert eq.enum_options("@MODE")[2] == "Heat Pump"
    assert eq.enum_label("@MODE") == "Heat Pump"
    assert eq.connected is True


def test_enum_labels_are_stripped():
    eq = _eq(1)
    assert eq.enum_options("@MODE") == ["Disabled", "Enabled"]
    assert eq.enum_label("@MODE") == "Enabled"


def test_apply_update_bare_and_object_values():
    eq = _eq(0)
    assert eq.apply_update({"device_name": "x", "@SETPOINT": 120}) is True
    assert eq.value("@SETPOINT") == 120
    assert eq.limits("@SETPOINT") == (110, 140)  # constraints preserved
    assert eq.apply_update({"@MODE": {"value": 1, "status": "Energy Saver"}}) is True
    assert eq.enum_label("@MODE") == "Energy Saver"
    assert eq.apply_update({"@SETPOINT": 120}) is False
    eq.apply_update({"@CONNECTED": False})
    assert eq.connected is False


def test_hot_water_icon():
    assert parse_hot_water_icon("ic_tank_fourty_percent_v2.png") == 40
    assert parse_hot_water_icon("ic_tank_hundread_percent.png") == 100
    assert parse_hot_water_icon("ic_tank_zero_percent_v2.png") == 0
    assert parse_hot_water_icon("something.png") is None
    assert parse_hot_water_icon(None) is None


def test_iso_utc_matches_app_format():
    dt = datetime(2026, 11, 9, 8, 30, 15, 123456, tzinfo=timezone.utc)
    assert _iso_utc(dt) == "2026-11-09T08:30:15.123Z"


def test_mqtt_publish_payload_and_topic():
    fake_client = MagicMock()
    fake_client.publish.return_value = MagicMock(rc=0)
    with patch("paho.mqtt.client.Client", return_value=fake_client):
        mqtt = EconetMqtt(
            loop=MagicMock(),
            host="broker",
            port=1884,
            account_id="acct",
            user_token="tok",
            system_key="key",
            tls_context=ssl.create_default_context(),
            on_update=lambda p: None,
        )
    fake_client.username_pw_set.assert_called_once_with("tok", "key")
    tid = mqtt.publish(_eq(0), {"@SETPOINT": 125})
    topic, body = fake_client.publish.call_args.args[:2]
    assert topic == "user/acct/device/desired"
    sent = json.loads(body)
    assert sent == {
        "device_name": "1111222233334444",
        "serial_number": "09-0c-12-13-0b-05-47-21-01",
        "@SETPOINT": 125,
        "transactionId": tid,
    }
    assert tid.startswith("ANDROID_")


def test_mqtt_message_dispatch():
    loop = MagicMock()
    received = []
    with patch("paho.mqtt.client.Client", return_value=MagicMock()):
        mqtt = EconetMqtt(loop, "h", 1, "a", "t", "k", ssl.create_default_context(), received.append)
    msg = MagicMock(topic="user/a/device/reported", payload=b'{"device_name":"d","@SETPOINT":120}')
    mqtt._handle_message(None, None, msg)
    loop.call_soon_threadsafe.assert_called_once()
    cb, payload = loop.call_soon_threadsafe.call_args.args
    cb(payload)
    assert received == [{"device_name": "d", "@SETPOINT": 120}]


def test_tls_context_trusts_bundled_root():
    """The broker chains to DigiCert Global Root CA, absent from current bundles."""
    from custom_components.rheem_econet.api import build_tls_context

    subjects = [dict(x[0] for x in c["subject"]).get("commonName") for c in build_tls_context(True).get_ca_certs()]
    assert "DigiCert Global Root CA" in subjects
    assert build_tls_context(False).verify_mode == ssl.CERT_NONE
