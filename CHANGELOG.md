# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [2.0.5] - 2026-05-28

### Fixed
- **HA MQTT disconnects (panda_backend)** — same best-practice fix applied as cc2_backend:
  `clean_session=False` with stable `PandaNative_{PRINTER_SN}` client_id; autodiscovery
  published once on first connect only; subscribes to `homeassistant/status` birth message
  and republishes discovery when HA restarts
- **Device turns off when chamber reaches target in CC2 mode** — when at temperature with
  an active CC2 print the WS loop was sending `work_on=False`, killing the device's internal
  PID temperature control. Fixed: `target_state` stays `RELAY_ON` while `_cc2_printing` and
  `target > 0`; device manages setpoint autonomously. Only turns off when print ends
- **Print never resumed after chamber reached target** — `cc2_chamber_temp=0` stale retained
  message from cc2_backend startup caused `min(chamber_temp, 0) = 0 < target`, blocking the
  resume condition forever. Both sensors are now used with a `> 5°C` validity guard; falls back
  to Panda Breath sensor alone if CC2 reading is not yet valid
- **HA MQTT disconnects (cc2_backend)** — correct HA/Mosquitto integration pattern:
  `clean_session=False` with stable `cc2_bridge_{CC2_SN}` client_id; autodiscovery published
  once on first connect; subscribes to `homeassistant/status` birth message and republishes
  discovery only when HA restarts; `loop_forever()` in daemon thread
- **HA MQTT keepalive drops** — added explicit `cc2/status=online` publish in heartbeat_thread
  every 20s to keep broker keepalive timer alive; tightened reconnect delay to min=1/max=10s
- **CC2 chamber temp in preheat logic** — `current_data["cc2_chamber_temp"]` populated from
  `cc2/chamber_temp` MQTT subscription; used alongside Panda Breath sensor for `needs_preheat`
  and auto-resume decisions; `> 5°C` guard prevents stale startup value from blocking resume
- **panda_web MQTT disconnects** — switched to `loop_forever()` in daemon thread with
  `reconnect_delay_set(min=2, max=30)` for reliable reconnect handling

### Added
- **CC2 Chamber tile** in panda_web shows CC2 printer's own chamber temperature sensor
  alongside the Panda Breath sensor reading

## [2.0.4] - 2026-05-28

### Fixed
- **HA MQTT "Keep alive timeout" every ~2 minutes** in `cc2_connector` — switched `ha_client`
  from a manual `loop_forever()` thread to paho's `loop_start()` (internally managed thread)
  and reduced `keepalive` from 60s to 30s; the manual thread was not reliably sending MQTT
  PINGREQs, causing broker-side keep-alive timeouts. These disconnects were also the root
  cause of delayed heater shut-off: `cc2_backend` couldn't forward `print_status=cancelled`
  to HA while disconnected, so `panda_backend` never got the turn-off signal until HA reconnected
- **Heat tile shows OFF despite active heating in CC2 mode** — in CC2 mode the Panda Breath
  device's `isrunning` confirmation lags the heat command; chamber temperature was rising but
  the Heat tile (and `heating` MQTT topic) showed OFF for up to 60s. Now uses
  `global_heating_state == RELAY_ON` for the Heat tile in CC2 mode (our commanded state)
  rather than waiting for device confirmation; legacy Klipper mode unchanged
- **`get_settings` poll missing from manual/set and auto/set handlers** — added 0.3s-delayed
  `get_settings` poll after mode-switch heat commands (same as CC2 slicer path) so device
  state syncs within ~0.5s after a mode change
- **Syntax error in if/elif chain** — `_cc2_printing` assignment was inserted between `elif`
  clauses in both WS loop and TLS emulation loop, breaking Python parsing; moved before the
  `if global_lock:` block in both locations
- **cc2_backend startup crash** — `loop_forever()` raised a TCP timeout on first CC2 MQTT
  connect attempt (printer not ready yet), calling `sys.exit(1)`; fixed with
  `retry_first_connection=True`
- **panda_web MQTT keepalive drops** — added TCP `SO_KEEPALIVE` (idle=10s) to panda_web MQTT
  `on_connect`, matching the panda_backend socket hardening
- **Filament type selected at idle/startup** — `publish_active_filament` was called on canvas
  info responses regardless of print state; now only fires when `print_state` is in the active
  printing set (`printing`, `preheating`, `paused`, etc.)
- **Tray lookup by list index instead of tray_id field** — `active_tray_id` was used as a
  `tray_list` array index; CC2 reports slot numbers (e.g. 4) not list positions, so the lookup
  silently fell through to first-tray fallback. Now matches by each tray's own `tray_id` field;
  logs a warning with available IDs if the active tray isn't found
- **CC2 disconnect not reflected in HA** — `cc2_on_disconnect` now publishes
  `{prefix}/status=offline` and clears `_canvas_info`/`_last_filament_type`; reconnect
  republishes `status=online` without requiring an HA MQTT reconnect
- **TLS emulation server started in CC2 mode** — port 8883 was opened and cert files required
  even though no Panda Touch connects in CC2 mode; server now skipped when `CC2_IP` is set

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
