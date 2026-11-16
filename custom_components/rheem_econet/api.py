"""Standalone async client for the Rheem/Ruud EcoNet cloud.

This module has no Home Assistant dependency so it can be exercised from the
command line (see tools/econet_probe.py).

Protocol summary (recovered from the Android app, see docs/PROTOCOL.md):

* REST: ClearBlade platform at ``REST_BASE_URL``. Every call carries the
  ``ClearBlade-SystemKey`` / ``ClearBlade-SystemSecret`` headers; authenticated
  calls add ``ClearBlade-UserToken``. Business logic lives in ClearBlade "code
  services" at ``code/{system_key}/<name>`` (always POST, JSON body).
* MQTT: TLS broker given by the "Production" environment's ``cloud_url`` /
  ``cloud_port``. Username is the user token, password is the system key.
  Device state arrives on ``user/{account_id}/device/reported``; commands are
  published to ``user/{account_id}/device/desired`` as
  ``{"device_name", "serial_number", "transactionId", "@OBJECT": value}``.
* Object values are Fahrenheit for temperatures, and enum values are the index
  into that object's ``constraints.enumText`` list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from pathlib import Path
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .const import (
    APP_VERSION,
    DEFAULT_ENVIRONMENT,
    DEFAULT_SYSTEM_KEY,
    DEFAULT_SYSTEM_SECRET,
    REST_BASE_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


class EconetError(Exception):
    """Generic EcoNet failure."""


class EconetAuthError(EconetError):
    """Credentials rejected or session could not be re-established."""


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


def _clean(text: Any) -> str:
    """EcoNet pads enum labels with trailing spaces ("Enabled       ")."""
    return str(text).strip() if text is not None else ""


_HOTWATER_WORDS = {
    "zero": 0,
    "ten": 10,
    "twenty": 20,
    "thirty": 30,
    "fourty": 40,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundread": 100,
    "hundred": 100,
}


def parse_hot_water_icon(icon: str | None) -> int | None:
    """Turn ``ic_tank_fourty_percent_v2.png`` into 40.

    Some water heaters report remaining hot water only as an icon name.
    """
    if not icon:
        return None
    match = re.search(r"tank_([a-z]+)_percent", str(icon))
    if not match:
        return None
    return _HOTWATER_WORDS.get(match.group(1))


@dataclass
class Equipment:
    """One device ("equiptment" in the API) belonging to a location."""

    raw: dict[str, Any]
    location_id: str
    location_name: str

    # ---- identity --------------------------------------------------------
    @property
    def device_name(self) -> str:
        """Device identifier used in MQTT commands (MAC-derived id)."""
        return str(self.raw.get("device_name", ""))

    @property
    def serial_number(self) -> str:
        return str(self.raw.get("serial_number", ""))

    @property
    def unique_id(self) -> str:
        return f"{self.device_name}_{self.serial_number}"

    @property
    def device_type(self) -> str:
        """"WH" (water heater) or "HVAC" (thermostat / control center)."""
        return str(self.raw.get("device_type", ""))

    @property
    def model(self) -> str:
        """Firmware template, e.g. ``heatpumpWaterHeaterGen5``."""
        return _clean(self.raw.get("@TYPE")) or self.device_type

    @property
    def name(self) -> str:
        return _clean(self.value("@NAME")) or self.model

    @property
    def actions(self) -> list[str]:
        return list(self.raw.get("actions") or [])

    # ---- object accessors -----------------------------------------------
    def has(self, key: str) -> bool:
        return key in self.raw

    def obj(self, key: str) -> Any:
        return self.raw.get(key)

    def value(self, key: str) -> Any:
        """Return the scalar value of an @OBJECT (bare or ``{"value": ...}``)."""
        item = self.raw.get(key)
        if isinstance(item, dict):
            return item.get("value")
        return item

    def status(self, key: str) -> str | None:
        item = self.raw.get(key)
        if isinstance(item, dict) and "status" in item:
            return _clean(item["status"])
        return None

    def constraints(self, key: str) -> dict[str, Any]:
        item = self.raw.get(key)
        if isinstance(item, dict) and isinstance(item.get("constraints"), dict):
            return item["constraints"]
        return {}

    def enum_options(self, key: str) -> list[str]:
        return [_clean(t) for t in self.constraints(key).get("enumText") or []]

    def enum_label(self, key: str) -> str | None:
        """Current label of an enum object."""
        options = self.enum_options(key)
        val = self.value(key)
        if isinstance(val, int) and 0 <= val < len(options):
            return options[val]
        return self.status(key)

    def limits(self, key: str) -> tuple[float | None, float | None]:
        c = self.constraints(key)
        return c.get("lowerLimit"), c.get("upperLimit")

    @property
    def connected(self) -> bool:
        return bool(self.value("@CONNECTED"))

    # ---- updates ---------------------------------------------------------
    def apply_update(self, update: dict[str, Any]) -> bool:
        """Merge a partial MQTT ``reported`` payload. Returns True if changed.

        Mirrors views/location/w0.java: an update value may be a bare scalar
        (replace ``value`` only) or an object carrying value/status/constraints.
        """
        changed = False
        for key, new in update.items():
            if not key.startswith("@"):
                continue
            old = self.raw.get(key)
            if isinstance(old, dict) and not isinstance(new, dict):
                if old.get("value") != new:
                    old["value"] = new
                    changed = True
            elif isinstance(old, dict) and isinstance(new, dict):
                merged = {**old, **new}
                if merged != old:
                    self.raw[key] = merged
                    changed = True
            elif old != new:
                self.raw[key] = new
                changed = True
        return changed


@dataclass
class Location:
    raw: dict[str, Any]
    equipment: list[Equipment] = field(default_factory=list)

    @property
    def location_id(self) -> str:
        return str(self.raw.get("location_id", ""))

    @property
    def name(self) -> str:
        return _clean(self.raw.get("@LOCATION_NAME")) or "EcoNet"

    @property
    def away(self) -> bool:
        return bool(self.raw.get("@AWAY"))


@dataclass
class Environment:
    """Subset of code/{key}/environments that the integration needs."""

    name: str
    system_key: str
    system_secret: str
    mqtt_host: str | None
    mqtt_port: int | None


# --------------------------------------------------------------------------
# REST client
# --------------------------------------------------------------------------


class EconetApi:
    """REST client mirroring the app's Retrofit services."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        base_url: str = REST_BASE_URL,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._base = base_url if base_url.endswith("/") else base_url + "/"
        self.system_key = DEFAULT_SYSTEM_KEY
        self.system_secret = DEFAULT_SYSTEM_SECRET
        self.environment: Environment | None = None
        self.user_token: str | None = None
        self.user_id: str | None = None
        self.account_id: str | None = None
        self._login_lock = asyncio.Lock()

    # ---- low level -------------------------------------------------------
    def _headers(self, token: str | None) -> dict[str, str]:
        headers = {
            "ClearBlade-SystemKey": self.system_key,
            "ClearBlade-SystemSecret": self.system_secret,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["ClearBlade-UserToken"] = token
        return headers

    async def _raw_post(
        self, path: str, body: Any, token: str | None
    ) -> tuple[int, Any]:
        url = self._base + path
        try:
            async with self._session.post(
                url,
                headers=self._headers(token),
                data=json.dumps(body if body is not None else {}),
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                text = await resp.text()
                try:
                    payload = json.loads(text) if text else {}
                except ValueError:
                    payload = {"_text": text}
                return resp.status, payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise EconetError(f"POST {path} failed: {err}") from err

    async def _code(self, service: str, body: Any = None) -> Any:
        """Call a ClearBlade code service, re-authenticating once on 401."""
        if not self.user_token:
            await self.login()
        path = f"code/{self.system_key}/{service}"
        status, payload = await self._raw_post(path, body, self.user_token)
        if status in (401, 403):
            _LOGGER.debug("%s returned %s, re-authenticating", service, status)
            await self.login(force=True)
            status, payload = await self._raw_post(path, body, self.user_token)
        if status >= 400:
            raise EconetError(f"{service} returned HTTP {status}: {payload}")
        return payload

    # ---- session ---------------------------------------------------------
    async def fetch_environment(self, name: str = DEFAULT_ENVIRONMENT) -> Environment | None:
        """Bootstrap like SplashActivity: user/anon, then code/{key}/environments."""
        status, anon = await self._raw_post("user/anon", {}, None)
        anon_token = anon.get("user_token") if isinstance(anon, dict) else None
        if status >= 400 or not anon_token:
            _LOGGER.debug("Anonymous auth failed (%s): %s", status, anon)
            return None
        status, payload = await self._raw_post(
            f"code/{self.system_key}/environments", {}, anon_token
        )
        if status >= 400 or not isinstance(payload, dict):
            _LOGGER.debug("environments failed (%s): %s", status, payload)
            return None
        envs = (payload.get("results") or {}).get("environments") or []
        chosen = next(
            (e for e in envs if str(e.get("name", "")).lower() == name.lower()), None
        ) or next((e for e in envs if e.get("is_primary")), None)
        if not chosen:
            return None
        env = Environment(
            name=str(chosen.get("name")),
            system_key=chosen.get("system_key") or self.system_key,
            system_secret=chosen.get("system_secret") or self.system_secret,
            mqtt_host=chosen.get("cloud_url"),
            mqtt_port=int(chosen["cloud_port"]) if chosen.get("cloud_port") else None,
        )
        self.system_key = env.system_key
        self.system_secret = env.system_secret
        self.environment = env
        return env

    async def login(self, force: bool = False) -> None:
        async with self._login_lock:
            if self.user_token and not force:
                return
            if self.environment is None:
                try:
                    await self.fetch_environment()
                except EconetError as err:
                    _LOGGER.debug("Environment bootstrap failed: %s", err)
            status, payload = await self._raw_post(
                "user/auth", {"email": self._email, "password": self._password}, None
            )
            token = payload.get("user_token") if isinstance(payload, dict) else None
            if status in (400, 401, 403):
                raise EconetAuthError(f"Login rejected (HTTP {status})")
            if status == 429:
                # Seen live after a handful of logins in a few minutes.
                raise EconetError("Login rate-limited by the cloud (HTTP 429)")
            if status >= 400 or not token:
                raise EconetError(f"Login failed (HTTP {status})")
            self.user_token = token
            self.user_id = payload.get("user_id")
            options = payload.get("options") or {}
            self.account_id = options.get("account_id") or self.account_id
            if not self.account_id:
                info = await self._code("getUserInfo", {})
                self.account_id = (info.get("results") or {}).get("account_id")
            if not self.account_id:
                raise EconetError("Could not determine account_id")

    # ---- data ------------------------------------------------------------
    async def get_locations(self) -> list[Location]:
        """code/{key}/getUserDataForApp - the app's main dashboard payload."""
        payload = await self._code(
            "getUserDataForApp",
            {
                "location_only": False,
                "version": APP_VERSION,
                "resource": "econet",
                "os": "android",
            },
        )
        results = payload.get("results") or {}
        locations: list[Location] = []
        for loc_raw in results.get("locations") or []:
            loc = Location(raw=loc_raw)
            # Sic: the API really spells it "equiptments".
            for eq_raw in loc_raw.get("equiptments") or loc_raw.get("equipments") or []:
                loc.equipment.append(
                    Equipment(raw=eq_raw, location_id=loc.location_id, location_name=loc.name)
                )
            locations.append(loc)
        return locations

    async def set_away(self, location_id: str, away: bool) -> None:
        """code/{key}/setAway with SetAwayGeoFencing {"@AWAY", "location_id"}."""
        await self._code("setAway", {"@AWAY": away, "location_id": location_id})

    async def get_alerts(self) -> Any:
        return await self._code("getAlertList", {})

    async def get_usage_config(self, eq: Equipment) -> dict[str, Any]:
        action = (
            "thermostatUsageReportView"
            if eq.device_type == "HVAC"
            else "waterheaterUsageReportView"
        )
        payload = await self._code(
            "dynamicAction",
            {
                "ACTION": action,
                "device_name": eq.device_name,
                "serial_number": eq.serial_number,
            },
        )
        return payload.get("results") or {}

    async def get_usage(
        self,
        eq: Equipment,
        start: datetime,
        end: datetime,
        usage_type: str = "energyUsage",
    ) -> dict[str, Any]:
        """code/{key}/dynamicAction with UsageReportDataRequest."""
        action = (
            "thermostatUsageReportView"
            if eq.device_type == "HVAC"
            else "waterheaterUsageReportView"
        )
        payload = await self._code(
            "dynamicAction",
            {
                "ACTION": action,
                "device_name": eq.device_name,
                "serial_number": eq.serial_number,
                "usage_type": usage_type,
                "start_date": _iso_utc(start),
                "end_date": _iso_utc(end),
            },
        )
        return payload.get("results") or {}


def _iso_utc(dt: datetime) -> str:
    """Format like core/utilities/f.h(): yyyy-MM-dd'T'HH:mm:ss.SSSXXX in UTC."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------
# MQTT client
# --------------------------------------------------------------------------


# DigiCert Global Root CA (G1). The broker's certificate chains to it, but
# current CA bundles (Debian, certifi) no longer ship it, so verification
# fails out of the box. Verified live 2026-11-16.
_EXTRA_CA_FILE = Path(__file__).with_name("digicert_global_root_ca.pem")


def build_tls_context(verify: bool) -> ssl.SSLContext:
    """Blocking (loads CA bundles) - call from an executor inside HA.

    The official app installs a trust-all TrustManager for MQTT. We verify
    against the system store plus the bundled DigiCert root, and let the user
    opt out entirely.
    """
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif _EXTRA_CA_FILE.exists():
        ctx.load_verify_locations(cafile=str(_EXTRA_CA_FILE))
    return ctx


class EconetMqtt:
    """Thin paho-mqtt wrapper; callbacks are delivered on the given loop."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        host: str,
        port: int,
        account_id: str,
        user_token: str,
        system_key: str,
        tls_context: ssl.SSLContext,
        on_update: Callable[[dict[str, Any]], None],
        on_connection: Callable[[bool], None] | None = None,
    ) -> None:
        import paho.mqtt.client as mqtt  # imported lazily for CLI friendliness

        self._loop = loop
        self._account_id = account_id
        self._on_update = on_update
        self._on_connection = on_connection
        self._topic_reported = f"user/{account_id}/device/reported"
        self._topic_desired = f"user/{account_id}/device/desired"
        self._host = host
        self._port = port
        self.connected = False
        self._lock = threading.Lock()

        client_id = f"{account_id}{int(time.time() * 1000)}_android"
        try:
            from paho.mqtt.enums import CallbackAPIVersion

            self._client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=client_id)
        except ImportError:  # paho-mqtt < 2.0
            self._client = mqtt.Client(client_id=client_id)
        self._client.username_pw_set(user_token, system_key)
        self._client.tls_set_context(tls_context)
        if tls_context.verify_mode == ssl.CERT_NONE:
            self._client.tls_insecure_set(True)
        self._client.reconnect_delay_set(min_delay=5, max_delay=300)
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    # paho v1 and v2 use different callback arities; accept both.
    def _handle_connect(self, client, userdata, flags, rc, *args) -> None:
        ok = (rc == 0) if isinstance(rc, int) else not getattr(rc, "is_failure", False)
        if not ok:
            _LOGGER.warning("EcoNet MQTT connect refused: %s", rc)
            self._set_connected(False)
            return
        client.subscribe(self._topic_reported, qos=0)
        _LOGGER.debug("EcoNet MQTT connected, subscribed to %s", self._topic_reported)
        self._set_connected(True)

    def _handle_disconnect(self, client, userdata, *args) -> None:
        _LOGGER.debug("EcoNet MQTT disconnected: %s", args)
        self._set_connected(False)

    def _set_connected(self, state: bool) -> None:
        self.connected = state
        if self._on_connection:
            self._loop.call_soon_threadsafe(self._on_connection, state)

    def _handle_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _LOGGER.debug("Ignoring non-JSON MQTT payload on %s", msg.topic)
            return
        if isinstance(payload, dict):
            self._loop.call_soon_threadsafe(self._on_update, payload)

    def start(self) -> None:
        """Blocking connect + background network loop (run in executor)."""
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def publish(self, eq: Equipment, values: dict[str, Any]) -> str:
        """Send a command exactly as EquipmentViewModel + v.A() do."""
        transaction_id = "ANDROID_" + datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        body = {
            "device_name": eq.device_name,
            "serial_number": eq.serial_number,
            **values,
            "transactionId": transaction_id,
        }
        _LOGGER.debug("EcoNet MQTT publish %s: %s", self._topic_desired, body)
        info = self._client.publish(self._topic_desired, json.dumps(body), qos=0, retain=False)
        if info.rc != 0:
            raise EconetError(f"MQTT publish failed (rc={info.rc})")
        return transaction_id
