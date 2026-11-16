# EcoNet cloud protocol (Rheem / Ruud)

Recovered from `com.ruud.econetconsumerandroid` **6.18.0** (`versionName
6.18.0-11623-3100206`, split APKs in this directory) by decompiling with JADX
1.5.6. Class names below are the obfuscated R8 names in that build. Expect
them to change between app versions; the string constants are more stable.

Legend: **[code]** = read directly in decompiled code, **[sample]** = from
JSON fixtures bundled in the APK's `assets/`, **[inferred]** = not directly
verified.

## 1. Platform

The app is a native Kotlin app (no Flutter/RN). All device traffic goes through
a **ClearBlade** IoT platform:

| Purpose | Value | Source |
|---|---|---|
| REST base | `https://rheem.rheemconnect.com/api/v/1/` | `SharedPreferenceUtils.B0()` default [code] |
| System key | `e2e699cb0bb0bbb88fc8858cb5a401` | `R.string.PROD_VALUE_SYSTEM_KEY` [code] |
| System secret | `E2E699CB0BE6C6FADDB1B0BC9A20` | `R.string.PROD_VALUE_SYSTEM_SECRET_KEY` [code] |
| OTA API (unused here) | `https://api.rheemconnect.com` `PATCH ota/approvals/{id}/` | `InterfaceC1489b` [code] |

The key and secret are app-wide constants shipped in every copy of the app.
They are not per-user secrets. They match the values used by the open-source
`pyeconet` library, which targets the older `rheem.clearblade.com` host.

### Headers (every REST call) [code: `O.a.C0307a`]

```
ClearBlade-SystemKey:    <system key>
ClearBlade-SystemSecret: <system secret>
Content-Type:            application/json
User-Agent:              com.ruud.econetconsumerandroid/<version>
ClearBlade-UserToken:    <user token>        (after login)
```

## 2. Session bootstrap

1. `POST user/anon` with body `{}` returns `{"user_token": "<anon token>"}` [code: `InterfaceC1502o.a()`]
2. `POST code/{system_key}/environments` with the anon token returns
   `results.environments[]` [code: `InterfaceC1502o.b()`]. The app picks the
   entry whose `name` equals the stored label, default `"Production"`
   (`SharedPreferenceUtils.T()`). The fields it uses are `system_key`,
   `system_secret`, `platform_url`, `cloud_url`, `cloud_port`, `auth_url`,
   `auth_port`, `aws_gateway_url`, `environment_configuration_version` [code:
   `BaseActivity.U1()`].
3. `POST user/auth` with body `{"email": "...", "password": "..."}` [code: `L.D()`, `Models.LoginRequest`]
   returns `user_token`, `user_id` and `options` (which include `account_id`).
   **Login does not need a Firebase App Check token.** App Check is only
   attached to register, forgot-password and OTP flows.
4. If `account_id` is missing, `POST code/{key}/getUserInfo` returns it in
   `results.account_id`.

## 3. REST code services

All calls are `POST code/{system_key}/<service>` with a JSON body [code: `L`, `InterfaceC1492e`].

| Service | Body | Use |
|---|---|---|
| `getUserDataForApp` | `{"location_only": false, "version": "<app ver>", "resource": "econet", "os": "android"}` | Main payload: `results.locations[].equiptments[]` (sic), plus templates, geo-fencing, OTA approvals |
| `setAway` | `{"@AWAY": true, "location_id": "..."}` | Location away mode |
| `dynamicAction` | `{"ACTION": "waterheaterUsageReportView", "device_name", "serial_number"}` | Usage-report configuration (`usage[]`, daily/weekly/monthly) |
| `dynamicAction` | same + `"usage_type": "energyUsage", "start_date", "end_date"` | Usage data: `results.energy_usage.data[{name,value}]`, `water_usage`, `hours_of_operation`, `equipment_cycles` |
| `dynamicAction` | `{"ACTION": "homeAwayView", ...}` / `{"ACTION": "alertList", ...}` | Away config / alerts |
| `getAlertList`, `getAlertDetails`, `clearAlert`, `readAlert` | | Alerts |
| `configureSchedule`, `getProgramById`, `configureAway`, `configurePreScheduleAway`, `getEnergyPlan`, `configureEnergyPlan` | | Schedules and energy plans (not implemented) |
| `updateLocation`, `updateDevice`, `deleteEquipment`, `createNewDevice`, `provisioningConfig` | | Account and provisioning (not implemented) |

The HVAC equivalent of the usage action is `thermostatUsageReportView`. Dates
use the format `yyyy-MM-dd'T'HH:mm:ss.SSSXXX` in UTC (`core/utilities/f.h()`).

## 4. MQTT

[code: `com.rheem.econet.data.remote.v`]

* URL: `ssl://<environment.cloud_url>:<environment.cloud_port>`. There is no
  hard-coded fallback, so the broker always comes from step 2.
* Client ID: `<stored prefix><epoch ms>_android`
* Username: **user token**. Password: **system key**.
* TLS: the app installs a trust-all `X509TrustManager`, so it never verifies
  the broker's certificate. This integration verifies by default and has an
  option to turn verification off.
* Subscribe: `user/{account_id}/device/reported` (QoS 0)
* Publish commands to `user/{account_id}/device/desired` (QoS 0, not retained):

  ```json
  {"device_name": "<id>", "serial_number": "<sn>", "@SETPOINT": 125,
   "transactionId": "ANDROID_2026-11-09T08:30:15"}
  ```

  The base keys come from the template's `uniqueIdentifier.device` list,
  which is `["device_name", "serial_number"]` [sample: `getTemplate.json`;
  code: `EquipmentViewModel.w()`]. One message can carry several objects, for
  example `@HEATSETPOINT` and `@COOLSETPOINT` together (`EquipmentViewModel.G()`).
* Reported messages are partial: `device_name` and `serial_number`, plus the
  changed `@OBJECT`s. A value is either a bare scalar or an object with
  `value`, `status` and `constraints` [code: `views/location/w0.java`].
* The app can also request a full refresh over MQTT: it publishes
  `{"ACTION": "getUserData", "location_only": false, "transactionId": ...}`
  to `user/{account_id}/android/{device id}/desired` and gets the answer on
  `.../reported`. This integration polls REST instead.

## 5. Object model

Every device ("equipment") is a flat map of `@OBJECT` keys. An object looks like this:

```json
"@MODE": {"constraints": {"enumText": ["Off","Energy Saver","Heat Pump","High Demand","Electric"],
                          "lowerLimit": 0, "upperLimit": 4},
          "status": "Heat Pump", "value": 2}
```

* **Enums are written as the index into that device's own `enumText` list.**
  Labels are padded with spaces (`"Enabled       "`), so strip them before
  comparing. The global `WaterHeaterMode` enum in the app is only an icon map.
* **Temperatures are always °F on the wire.** The template flags them with
  `isConversion: true`, and the app converts for Celsius users
  (`celsiusIdentifier.data = ["@SETPOINT","@HEATSETPOINT","@COOLSETPOINT"]`).
* `device_type` is `"WH"` (water heater) or `"HVAC"` (thermostat or control
  center). `@TYPE` names the firmware template.

### Water heater objects (`WH`)

| Object | Meaning | Seen on |
|---|---|---|
| `@SETPOINT` | Target temperature (°F), with min/max and a scald warning at 120 | all |
| `@MODE` | Operating mode enum (`Off / Energy Saver / Heat Pump / High Demand / Electric`, or `Disabled / Enabled`) | all |
| `@ENABLED` | Enable enum, on models without `@MODE` | eagle |
| `@ACTIVE` | bool: currently heating | most |
| `@RUNNING` | Text such as "Running" | heat pump |
| `@CONNECTED` | bool: WiFi module online | all |
| `@HOTWATER` | Remaining hot water, given as an icon name (`ic_tank_fourty_percent_v2.png`) | dragon, heat pump |
| `@TANK` | % tank level | Triton |
| `@COMBUSTION`, `@HOURS`, `@CYCLES`, `@BURNTIME` | Diagnostics | Triton, Eagle |
| `@ALTITUDE`, `@SAVEWATER`, `@RPUMPINSTANCE`, `@LEAKACTION` | Settings enums | Niagara, Triton |
| `@VALVE`, `@VALVESTATUS`, `@VALVEINSTALLED`, `@VALVEOVERRIDE` | Leak shut-off valve | Hotspring, Triton |
| `@ALERTCOUNT`, `@AWAY`, `@SCHEDULE` | Misc | all |

Templates seen in the app: `heatpumpWaterHeaterGen4`, `heatpumpWaterHeaterGen5`,
`tritonWaterHeater`, `niagaraWaterHeater`, `hotspringWaterHeater`,
`eagleWaterHeater`, `dragonWaterHeater`, `nashWaterHeater`, `nexiWaterHeater`,
`vulcanWaterHeater`, `smartbuckWaterHeater`, `contractorWaterHeater`,
`CTA2045UCMACDevice` (demand-response module).

### Thermostat objects (`HVAC`, e.g. `econetControlCenter`)

`@HEATSETPOINT`, `@COOLSETPOINT`, `@MODE` (`Heating / Cooling / Auto / Fan
Only / Off`), `@FANSPEED`, `@HUMIDITY`, `@DEHUMSETPOINT`, `@DEHUMENABLE`,
`@DEADBAND`, `@STATUS`, `@SCHEDULESTATUS`, `@BEEP`, `@SCREENLOCK`, `@SETPOINT`.
**[inferred]** On thermostats `@SETPOINT` looks like the room temperature
display: it has a 0–99 range and differs from the heat and cool setpoints.

## 6. Verified live (2026-11-16, one `heatpumpWaterHeaterGen5`)

* Anonymous bootstrap, `user/auth`, `getUserDataForApp` and both
  `dynamicAction` usage calls work exactly as described above.
* The Production environment returns the broker `rheemprod.rheemcert.com:1884`.
* **Login is rate-limited.** About four logins within a few minutes produced
  HTTP 429 on `user/auth` for roughly ten minutes. Keep the token and reuse it.
* **Broker certificate:** a valid `*.rheemcert.com` certificate that chains to
  *DigiCert Global Root CA* (G1). Current Debian and certifi bundles no longer
  include that root, so default verification fails. The integration ships the
  root (`digicert_global_root_ca.pem`, SHA-256 `43:48:A0:E9:…:C7:01:61`) and
  adds it to the trust store, after which verification succeeds.
* A `@SETPOINT` publish is echoed on the reported topic within a second, with
  the same `transactionId`, and REST shows the new value straight away:

  ```json
  {"@SETPOINT": 124, "@ACTIVE": true, "@STATUS": "", "device_name": "...",
   "serial_number": "...", "transactionId": "ANDROID_2026-11-16T21:51:02"}
  ```
* Gen5 modes are `Off / Energy Saver / Heat Pump / High Demand / Electric/Gas /
  Vacation`. It has `@MODE` and `@ENABLED` together.
* On heat pumps `@TANK` and `@COMBUSTION` are 0-100 health scores (status
  "Element operating normally", "Compressor life is normal"), not a tank level
  or combustion figure. `@VALVE` exists with an empty value when no shut-off
  valve is installed.
* Usage: the requested window is honoured. Up to about a day gives hourly
  points named by hour of day; longer windows give daily points (`Mon_16`).
  The response also carries `historyData` (the previous period) and a
  `message` with the total. The server's day boundary was 07:00 UTC for a
  US Pacific location in winter (expected 08:00), so its hour labels and
  daily buckets can sit one hour off local time.

## 7. Open questions

* Whether `platform_url` from the environment should replace the REST base.
  The integration keeps the built-in base URL, which works.
* Whether every enum object exposed as a `select` is accepted from the cloud
  side. Only `@SETPOINT` writes have been tested live.
* Everything about `HVAC` devices is untested.
