# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [2.0.3] - 2026-05-28

### Added
- **Backend mode indicator** — `Panda.py` publishes `{prefix}/backend_mode` = `"CC2"` or `"Klipper"`
  as a retained MQTT topic on every connect; web GUI header displays a blue `CC2 Mode` or green
  `Klipper Mode` badge so the active path is always visible
- **State-transition logging** (always-on, not behind `DEBUG`): `[HEAT-ON]`, `[HEAT-OFF]`, `[IDLE]`,
  `[AT-TEMP]` on every status change; periodic `[STATUS]` every 60s while state is stable
- **Bind confirmation log** `[WS] Bind confirmed` emitted for both CC2 and Klipper paths
- **Power-off completion log** `[POWER-OFF] Heater shutdown complete`; warns if Panda not connected

### Changed
- **`heizung` MQTT topic → `heating`**; `heizung_stop/set` → `heat_stop/set` — HA dashboard YAML and
  `panda_web.py` tile definitions updated to match
- **German runtime strings fully removed**: local variable `kammer` → `chamber_target`; log strings
  `"Neue Datei erkannt"` → `"New file detected"`, `"MANUELL MODE"` → `"MANUAL MODE"`,
  `"System ist LOCKED! Befehl ignoriert"` → `"System is LOCKED! Command ignored"`; German variable
  names `kammer_soll` → `chamber_setpoint`, `kammer_ist` → `chamber_temp`, `bett_limit` → `bed_limit`
- **Status strings**: `"Hysterese"` replaced — shows `"Idle"` when CC2 has no active print or
  target=0; shows `"At Temperature"` when holding within hysteresis band
- **HA MQTT autodiscovery fully rebuilt** — all entities use clean `unique_id` (`{PRINTER_SN}_{slug}`),
  `object_id` with `panda_` prefix, English names; `panda_slicer_target_temp` corrected
  (was `panda_slicer_target`); existing entities must be re-added to dashboards after upgrade
- **CC2 mode: TLS emulation server not started** — no Panda Touch in the CC2 path; port 8883 stays
  closed; `cert.pem` / `key.pem` not required when `CC2_IP` is set
- **Relay magic numbers replaced** — `RELAY_ON = 85.0` / `RELAY_OFF = 20.0` constants replace all
  `> 50` / `== 20.0` / `== 85.0` comparisons throughout heating logic

### Fixed
- **`bed_target_temper` hardcoded `100.0`** — now sends actual `chamber_setpoint` so the Panda
  Touch display shows the real target while heating
- **Heat tile lag (~10s)** — backend now polls `get_settings` 0.3s after every heat command; Heat
  tile in HA and web GUI flips to ON within ~0.5s instead of the next device heartbeat
- **`global_heating_state` not reset on WS disconnect** — retained `RELAY_ON` across reconnects;
  now resets to `RELAY_OFF` and publishes `heating=OFF` on disconnect so HA doesn't show Heat=ON
  while the device is offline
- **`panda_web` stale topics on MQTT disconnect** — `state["topics"]` now cleared on disconnect so
  the web GUI shows `--` for all tiles instead of the last known values during broker outage
- **CC2 MQTT disconnect not reflected in HA** — `cc2_on_disconnect` now publishes
  `{CC2_TOPIC_PREFIX}/status=offline` so all CC2 HA entities go unavailable when the printer
  reboots or loses network; reconnect republishes `status=online` without needing an HA MQTT reconnect
- **`_canvas_info` / `_last_filament_type` not cleared on CC2 disconnect** — stale tray data
  could cause wrong filament type to fire on the next print start; now cleared on disconnect
- **MQTT keepalive=60** on all MQTT clients; TCP `SO_KEEPALIVE` (idle=10s) on backend client
- **Chamber setpoint max** raised from 80°C to 85°C to match relay hardware limit (`RELAY_ON`)
- **Fan speed raw=1 edge case** fixed (was mapped incorrectly)
- Three HA MQTT autodiscovery validation errors resolved

## [2.0.2] - 2026-05-28

### Fixed
- **WS recv() blocks indefinitely** — Panda Breath device only sends settings messages reactively;
  after the initial bind exchange the loop sat at `await websocket.recv()` forever, so temperature
  comparisons and heat on/off logic never ran again. Fixed with `asyncio.wait_for(recv, timeout=10)`
  — if the device goes quiet for 10s, panda_backend sends `{"get_settings": 1}` to poll it.
  This is why HA showed "Heating Active" from the single bind response but never updated again.
- **panda_web slow startup** — `mqtt_client.connect()` (blocking) in the MQTT thread could stall
  before the HTTP server started; switched to `connect_async` + `keepalive=120` to match other
  clients

## [2.0.1] - 2026-05-28

### Fixed
- **Heater not starting on print**: `main_loop` can be `None` when retained MQTT messages arrive
  during container startup before `asyncio.run(main())` sets it — `run_coroutine_threadsafe(None)`
  silently dropped the heat command; now guarded with `if main_loop:` check
- **WS loop heat command missing `work_mode=2`** in CC2 mode — device could be in Standby
  (work_mode=0) and ignore the `work_on/isrunning` command; now always forces Manual mode
- **Reconnect heat command** now also includes `work_mode=2` (was sent separately, race possible);
  added 0.2s delay between `work_mode=2` and the heat command to let device process mode change
- **Missing log** when print starts with kammer_soll>0 but Panda WS not yet connected — now logs
  "heat queued at kammer_soll=X°C, will fire on WS connect" for easier diagnosis

## [2.0.0] - 2026-05-28

### Added
- Docker/GHCR multi-arch build workflow (`linux/amd64`, `linux/arm64`) — pre-built images
  at `ghcr.io/njhughes-01/biqu-panda-breath-mod`
- `cc2_connector.py` — Elegoo Centauri Carbon 2 MQTT bridge; publishes sensors via HA
  autodiscovery (nozzle/bed/chamber temps, targets, print status/progress, fan speed,
  LED, speed mode, filament detected, file list, active filament type)
- `panda_web.py` — browser-based control dashboard on port 8088; replaces the upstream
  PySide6 desktop GUI concept with a dependency-free HTTP+MQTT interface
- `docker-compose.cc2.yml` — Compose override for CC2 users; standard users run only
  `docker-compose.yml` and never need the CC2 backend
- `generate_config.py` — testable Python module for `panda_config.json` generation
- Test suite (`pytest`) covering config generation, CC2 message parsing, web endpoints,
  and CC2_IP conditional integration
- `SETUP.md` — full setup guide with both deployment paths, env var reference tables,
  TLS documentation, and troubleshooting
- Runtime TLS cert generation via Docker volume (`panda_certs`) — no manual cert setup
- CC2 filament-to-chamber-temp map (`CC2_FILAMENT_MAP` env var override); automatically
  selects target from AMS tray, filename, or nozzle temp as fallback
- Slicer Priority Mode for CC2: detect print start → pause CC2 → heat chamber → auto-resume
  when chamber reaches target; full filament type → chamber temp mapping
- Print control buttons in HA: pause, resume, stop, LED toggle, fan speed, speed mode
- `MQTT_PORT` env var support (`HA_MQTT_PORT`) — was hardcoded 1883

### Changed
- `docker-compose.yml` switched from local build to GHCR image pull
- `docker-entrypoint.sh` config generation refactored to call `generate_config.py`
- `CC2_IP` env var gates CC2 MQTT subscription and bed-temp source in `Panda.py`;
  standard deployments (no `CC2_IP`) behave identically to upstream
- CC2 mode always forces `work_mode=2` (Manual) on Panda Breath connect — AUTO mode
  uses Klipper bed-temp logic which can't reach the CC2's API
- `panda_backend` in `docker-compose.cc2.yml` now depends on `cc2_backend` being healthy
  before starting

### Fixed
- German HA entity names and log strings translated to English
- paho-mqtt v2 callback API (`CallbackAPIVersion.VERSION2`, `ReasonCode.is_failure`)
- `panda_web` button state now reflects live MQTT state (mode/power/slicer buttons)
- HA REST API bed temp fetch skipped when CC2 MQTT data is available
- **TCP SO_KEEPALIVE** on both HA MQTT sockets (idle=10s) — prevents NAT/VLAN firewall
  dropping idle connections and causing "Keep alive timeout" disconnects
- **TLS emulation loop** no longer resets `global_heating_state` in CC2 mode — the WS
  loop exclusively owns heating state when CC2_IP is set
- **WS reconnect heat recovery** — condition was `global_heating_state > 50` (always false
  after disconnect resets it to 20); now correctly checks `kammer_soll > 0`
- **Duplicate pause race** — `cc2_paused_for_preheat` re-checked inside heat coroutines
  both before the 1.5s sleep and after, preventing double-pause on concurrent triggers
- **Blind resume** — auto-resume at temperature now verifies `cc2_print_status == "paused"`
  before publishing resume command
- **Manual mode** — now correctly clears `current_data["slicer_priority_mode"]`; previously
  only published MQTT `OFF` without updating the in-memory flag
- **CC2 state machine** — `bind_confirmed` reset to `False` on WS disconnect so `work_mode=2`
  is re-forced on every reconnect, not just the first
- **Print-end cleanup** — `active_filament_type` retained message cleared, `cc2_pending_filament`
  and slicer targets reset so next print starts clean
- **Retained message race** — `active_filament_type` arriving before `print_status` on
  container restart buffered in `cc2_pending_filament` and applied on print-start transition
- **PLA/TPU (target=0)** — explicitly clears `kammer_soll` and sends heater-off; previously
  heater stayed on from prior ABS/ASA print
- **Safety overrides** (power-off, emergency stop, unlock) now resume CC2 if it was paused
  for preheat
- **Thread safety** in `cc2_connector.py` — `_canvas_info`, `_file_list`, `_last_print_state`,
  `_last_filament_type` protected with `_cc2_state_lock`; `publish_to_ha` called from both
  the cc2 and HA MQTT threads
- **LED optimistic publish** uses `retain=True` so HA switch state survives reconnects
- **Registration retry** on CC2 MQTT failure retries after 5s instead of silently dropping
