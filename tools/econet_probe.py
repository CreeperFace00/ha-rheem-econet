#!/usr/bin/env python3
"""Exercise the EcoNet protocol outside Home Assistant.

Usage (password is prompted, or read from ECONET_PASSWORD):

    pip install aiohttp paho-mqtt
    python3 tools/econet_probe.py --email you@example.com info
    python3 tools/econet_probe.py --email you@example.com devices
    python3 tools/econet_probe.py --email you@example.com dump      # raw JSON
    python3 tools/econet_probe.py --email you@example.com watch     # live MQTT
    python3 tools/econet_probe.py --email you@example.com set @SETPOINT 120
    python3 tools/econet_probe.py --email you@example.com usage

`set` only acts on a water heater unless --device is given. Add --insecure-tls
if `watch` can't connect (the official app doesn't verify the broker cert).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import json
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

# Load custom_components/rheem_econet/{const,api}.py without importing the
# package __init__ (which needs Home Assistant).
PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "rheem_econet"
pkg = types.ModuleType("rheem_econet")
pkg.__path__ = [str(PKG_DIR)]
sys.modules["rheem_econet"] = pkg
api_mod = importlib.import_module("rheem_econet.api")

import aiohttp  # noqa: E402


def redact(token: str | None) -> str:
    return f"{token[:6]}…({len(token)} chars)" if token else "None"


def pick_device(locations, wanted: str | None):
    for loc in locations:
        for eq in loc.equipment:
            if wanted and wanted in (eq.device_name, eq.serial_number, eq.name):
                return eq
            if not wanted and eq.device_type == "WH":
                return eq
    raise SystemExit("No matching device (use --device)")


async def main(args) -> None:
    password = os.environ.get("ECONET_PASSWORD") or getpass.getpass("EcoNet password: ")
    async with aiohttp.ClientSession() as session:
        api = api_mod.EconetApi(session, args.email, password)
        await api.login()
        env = api.environment
        print(f"account_id : {api.account_id}")
        print(f"user_token : {redact(api.user_token)}")
        print(f"environment: {env.name if env else 'n/a (using built-in defaults)'}")
        print(f"mqtt broker: {env.mqtt_host}:{env.mqtt_port}" if env else "mqtt broker: unknown")
        if args.cmd == "info":
            return

        locations = await api.get_locations()
        if args.cmd == "dump":
            print(json.dumps([l.raw for l in locations], indent=2))
            return
        if args.cmd == "devices":
            for loc in locations:
                print(f"\n# {loc.name}  (location_id={loc.location_id}, away={loc.away})")
                for eq in loc.equipment:
                    print(f"  - {eq.name} [{eq.device_type}/{eq.model}] connected={eq.connected}")
                    print(f"    device_name={eq.device_name} serial={eq.serial_number}")
                    for key in sorted(k for k in eq.raw if k.startswith("@")):
                        label = eq.enum_label(key) if eq.enum_options(key) else None
                        lo, hi = eq.limits(key)
                        extra = f"  -> {label!r} of {eq.enum_options(key)}" if label else ""
                        rng = f"  [{lo}..{hi}]" if lo is not None and not label else ""
                        print(f"      {key:18} = {eq.value(key)!r}{rng}{extra}")
            return
        if args.cmd == "usage":
            eq = pick_device(locations, args.device)
            print(json.dumps(await api.get_usage_config(eq), indent=2)[:4000])
            start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            print(json.dumps(await api.get_usage(eq, start, start + timedelta(days=1)), indent=2)[:4000])
            return

        if not env or not env.mqtt_host:
            raise SystemExit("No MQTT broker from the environments endpoint")
        loop = asyncio.get_running_loop()
        connected = asyncio.Event()

        def on_update(payload):
            print(f"[{datetime.now():%H:%M:%S}] reported: {json.dumps(payload)}")

        def on_conn(state):
            print(f"MQTT connected={state}")
            if state:
                connected.set()

        ctx = api_mod.build_tls_context(not args.insecure_tls)
        mqtt = api_mod.EconetMqtt(
            loop, env.mqtt_host, env.mqtt_port or 1884, api.account_id,
            api.user_token, api.system_key, ctx, on_update, on_conn,
        )
        mqtt.start()
        try:
            await asyncio.wait_for(connected.wait(), 30)
        except asyncio.TimeoutError:
            mqtt.stop()
            raise SystemExit("MQTT did not connect within 30 s (try --insecure-tls)")

        if args.cmd == "set":
            eq = pick_device(locations, args.device)
            raw = args.value
            value = int(raw) if raw.lstrip("-").isdigit() else raw
            tid = mqtt.publish(eq, {args.key: value})
            print(f"sent {args.key}={value!r} to {eq.name} (transactionId={tid}); waiting for echo…")
            await asyncio.sleep(15)
        else:  # watch
            print("Watching for reported updates, Ctrl-C to stop")
            await asyncio.Event().wait()
        mqtt.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--email", default=os.environ.get("ECONET_EMAIL"), required=not os.environ.get("ECONET_EMAIL"))
    p.add_argument("--device", help="device_name, serial number or display name")
    p.add_argument("--insecure-tls", action="store_true", help="don't verify the MQTT broker certificate")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("info", "devices", "dump", "watch", "usage"):
        sub.add_parser(name)
    s = sub.add_parser("set")
    s.add_argument("key", help="object name, e.g. @SETPOINT or @MODE")
    s.add_argument("value", help="integer (temperature in °F, or enum index)")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
