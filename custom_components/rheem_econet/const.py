"""Constants for the Rheem / Ruud EcoNet integration.

Values in this file were recovered by decompiling the official Android app
(com.ruud.econetconsumerandroid 6.18.0). See docs/PROTOCOL.md for provenance.
"""

DOMAIN = "rheem_econet"

# ClearBlade platform used by the EcoNet app (SharedPreferenceUtils.B0()).
REST_BASE_URL = "https://rheem.rheemconnect.com/api/v/1/"

# App-wide (not per-user) ClearBlade system credentials baked into the APK as
# string resources PROD_VALUE_SYSTEM_KEY / PROD_VALUE_SYSTEM_SECRET_KEY.
DEFAULT_SYSTEM_KEY = "e2e699cb0bb0bbb88fc8858cb5a401"
DEFAULT_SYSTEM_SECRET = "E2E699CB0BE6C6FADDB1B0BC9A20"

# The app selects the environment named "Production" from code/{key}/environments.
DEFAULT_ENVIRONMENT = "Production"

APP_PACKAGE = "com.ruud.econetconsumerandroid"
APP_VERSION = "6.18.0"
USER_AGENT = f"{APP_PACKAGE}/{APP_VERSION}"

CONF_VERIFY_MQTT_TLS = "verify_mqtt_tls"
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 300  # seconds; MQTT pushes fill the gaps

DEVICE_TYPE_WATER_HEATER = "WH"
DEVICE_TYPE_HVAC = "HVAC"

# The only hardware this integration has been verified against.
TESTED_DEVICE_TYPES = {DEVICE_TYPE_WATER_HEATER}
