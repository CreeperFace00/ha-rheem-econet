# Rheem / Ruud EcoNet for Home Assistant

A custom integration for EcoNet-connected Rheem and Ruud equipment. It was
built by reverse-engineering the official Ruud EcoNet Android app (6.18.0), and
the protocol notes are in [docs/PROTOCOL.md](docs/PROTOCOL.md).

> HA core has an `econet` integration (built on `pyeconet`). This one uses its
> own domain, `rheem_econet`, so both can be installed side by side. It targets
> the current `rheem.rheemconnect.com` endpoints the app uses today.

## How it works

* **Cloud push:** after logging in with your EcoNet email and password, it reads
  the broker address from the cloud's `environments` service and holds an MQTT
  connection open. Device changes show up immediately, and commands go out over
  the same connection.
* **Polling backstop:** a full REST refresh runs every 5 minutes by default.
  Energy usage is fetched every 30 minutes.

## Support matrix

| Device | Entities | Status |
|---|---|---|
| Water heaters (`WH`): heat pump Gen4/Gen5, Triton, Niagara, Hotspring, Eagle, … | `water_heater` (setpoint, operation modes, on/off, away), hot-water %, alerts, heating, connectivity, energy today, model-specific diagnostics | **Verified live on a heat pump Gen5** (login, state, MQTT push, setpoint writes, energy report). Mode changes, away mode and other models are untested |
| Thermostats / control centers (`HVAC`) | `climate` (mode, heat/cool range, fan speed, dehumidify setpoint, away preset), status, energy today | **Untested** |
| Settings enums: altitude, save water, recirculation pump, leak action, dehumidifier, beep, screen lock, shut-off valve | `select` (beep, screen lock and **shut-off valve are disabled by default**) | **Untested** |

Entities on device types other than water heaters carry
`untested_device_type: true`, and the device model ends in "(untested)".
Optional diagnostic sensors for unknown objects are created disabled.

## Install

**HACS (recommended):**

1. In HACS, open the menu (⋮) → **Custom repositories**.
2. Add `https://github.com/CreeperFace00/ha-rheem-econet` with type **Integration**.
3. Find "Rheem / Ruud EcoNet" in HACS, download it, and restart Home Assistant.
4. Go to Settings → Devices & services → Add integration → **Rheem / Ruud EcoNet**.

**Manual:** copy `custom_components/rheem_econet` into your HA
`config/custom_components/`, restart, then add the integration as in step 4.

Options: refresh interval, and **Verify the MQTT broker's TLS certificate**
(on by default). The broker's certificate chains to a DigiCert root that
current CA bundles have dropped, so the integration ships that root and trusts
it for this connection.

The cloud rate-limits logins (HTTP 429 after a few in quick succession). The
integration logs in once and reuses the token, but repeated reloads or probe
runs can trigger it. It clears after about ten minutes.

## Verifying against your account first (recommended)

Before installing, you can run the protocol layer against your real account
with the bundled probe:

```bash
pip install aiohttp paho-mqtt
python3 tools/econet_probe.py --email you@example.com info      # login + broker
python3 tools/econet_probe.py --email you@example.com devices   # every @OBJECT
python3 tools/econet_probe.py --email you@example.com watch     # live MQTT
python3 tools/econet_probe.py --email you@example.com set @SETPOINT 120
python3 tools/econet_probe.py --email you@example.com usage     # energy report
```

## Commands reference

All commands are MQTT publishes of `{"device_name", "serial_number",
"@OBJECT": value}`. Temperatures are in **°F** (HA converts for metric users),
and enums are written as the **index** into the device's own option list.

## Tests

```bash
pip install pytest-homeassistant-custom-component paho-mqtt
pytest
```
