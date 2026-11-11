# Panda Breath investigation and drying handoff — 2026-11-11

Goal: preserve the verified device, transport, deployment and drying findings for the next session. Done when this record and pending planning files are committed, completed-session scratch is relocated, and unmerged work remains recoverable. This closeout changes documentation and Git ignore rules only; it does not contact or change a printer, heater, broker, controller or gateway.

## Device identity and deployed services

- Actual Panda: `10.88.88.197`, MAC `ac:eb:e6:8e:36:2c`, MQTT identity `ACEBE68E362C`, firmware V1.0.4 during investigation.
- `10.88.88.127` is **backporchlight2**, MAC `54:48:e6:d4:02:1f`. Its “This URI does not exist” HTTP response was not a Panda response.
- Mod UI/API: `http://10.88.88.129:8080`. `GET /api/state` reads status; commands require task-specific authorization.
- Dedicated Panda device broker: `10.88.88.129:1884`, topics `panda_breath/ACEBE68E362C/{state,command,availability}`. This is separate from HA's broker; the device connects directly to this broker.
- Docker host: `proxmoxdocker` / debdocker, stack `/home/claude/hawser-stacks/panda`; services `panda_backend`, `panda_web`, `panda_device_mqtt`.
- Deployed backend image: `panda-supervised:20261110-reviewed`, source `c5aa93241436396a0ff183351a299a72a5c38cf1`. Repository branch: `fix/native-panda-mqtt`. No PR, push, merge or Jev merge gate is claimed.

## Control fixes and settings preservation

The original `dry_temp/set` and `dry_time/set` handlers updated local MQTT state without configuring the device. Native MQTT now configures and verifies device settings; device `custom_timer` uses whole **hours**, while the existing mod `dry_time` display uses **minutes**. An eight-hour request therefore displays 480 minutes and sends 8 hours. See [native transport](native-mqtt.md) and [supervised drying](supervised-drying.md).

The approved supervisor owns a fixed finish time across disconnects and backend restarts. If Panda reports drying OFF before that deadline, recovery requires fresh device evidence and uses the remaining duration, with bounded retries and uncertainty handling. Nathan explicitly authorized recovery after Panda's physical OFF button: use the **mod Stop** to cancel. Mode changes, mod power OFF and locks also cancel. An owed expiry/cancellation Stop persists until fresh device telemetry confirms it. Physical relay state is not independently measured.

Deployment preserved the original live settings rather than replacing them with repository defaults:

- Host file `supervised-state/panda_config.json` is mounted at `/app/panda_config.json`; original/final SHA256 `a0cced36386dfdfc157f5b1a549b221b792ad2c8c1a58b6044a1eac8bd769c5f`.
- Existing environment values compared equal; certificate and device-broker password mounts were unchanged.
- Named volume `panda_panda_dry_state` mounts `/app/dry_state`; `PANDA_DRY_SESSION_FILE=/app/dry_state/drying_session.json`.
- Backend image/state mounts updated in `compose.dry-fix.yaml`, `compose.native-mqtt.yaml`, and `compose.override.yaml` on the deployment host.
- Private rollback material remains at `/home/claude/hawser-stacks/panda/backups/supervised-20261110`; prior image `panda-native-mqtt:20261109-cancel-fixed` retained. Do not print backups or environment values. Assess active timer ownership before any rollback.

## Completed live drying cycle

Nathan authorized one existing-settings drying request: **50°C for eight hours**. It was sent once through the mod, at `2026-11-11 04:08:41 UTC` (Nov 10, 9:08 PM MST). Session `767a012322694223ab6605b563957bb3` had the fixed deadline `2026-11-11T12:08:41.286830Z` (Nov 11, **5:08 AM MST**).

Startup was intermittent: configuration confirmation initially failed and Panda subsequently reported OFF. The approved supervisor recovered without a second manual start. At 04:12:45 UTC, fresh state confirmed running and heating, with Panda temperature 19°C versus 15°C before start.

At the 06:20 AM MST check, fresh device state showed `completed`, `pending_stop=false`, and device drying running false. The host deadline had elapsed and Stop was confirmed. No new cycle was armed. The read-only temperature watcher emitted no incident alert during the run.

The [476 saved temperature samples](evidence/drying-cycle-20261111.csv) cover the active window: Panda's sensor reached approximately 50°C, while the CC2 chamber sensor peaked at 34°C. These are separate sensors, **not spool-core temperature or measured moisture content**. Nathan later confirmed the material was **PLA**, with the spool **inside the CC2 on the build plate**. The cooler chamber reading does not prove a faulty sensor; the plate and spool can be warmer than chamber air. Dryness remains unverified. Nathan will handle a test print in another session; none was prepared or started here.

## CC2 bed and future printing

The separately authorized bed command set a 50°C target once through the forked OrcaSlicer MCP. A subsequent status read confirmed target 50°C; actual bed readings were about 53–54°C. Later the target was 0°C without another agent command. Published CC2 firmware includes a 600-second bed idle timeout and an `M140 S0` handler; the live runtime timeout was not read or modified, so it remains a plausible explanation rather than a confirmed cause.

Use our forked OrcaSlicer MCP for CC2 status, camera, slicing, upload and printing, never HA or cc2print. Configured printer address is `10.88.88.190`. The installed launcher is `~/.local/bin/orca-mcp`; pass `ORCA_CC2_URL` explicitly when launching it through MCP stdio. A stale client allowlist does not establish that a server tool is absent.

Before a new print: establish actual printer/nozzle and matching live machine, process and filament presets; verify PLA loading/tray rather than assuming it from the dried spool. Obtain physical bed-clear confirmation (the spool was on it). Explicitly disable bed leveling for ordinary prints. Do not actively heat the chamber for PETG prints. These notes authorize no printer or heater action.

## Network evidence and final configuration

- Docker's IoT address `10.88.88.129/24` reaches Panda `10.88.88.197` on the same subnet; this path does not traverse pfSense routing.
- Header-only captures at Docker and Nano HD showed retransmissions in both directions. Paired Nano wired/Wi-Fi-facing software captures matched with at most 68 microseconds between capture points. Broker replies appeared on the AP Wi-Fi-facing interface without Panda advancing its TCP acknowledgement. These were Ethernet-format software captures, **not over-the-air captures**; RF delivery versus Panda receive/firmware behavior remains unresolved.
- Nano HD is in the garage (`10.16.69.108`, MAC `68:d7:9a:53:38:ad`). Controlled channel 11 and channel 6 tests with a temporary Panda-only lock produced repeated authentication/disassociation despite strong receptions around −55 to −57 dBm; neither produced a usable connection in the bounded trial. Both trials were rolled back.
- Final Nano configuration: channel **1**, 20 MHz; Panda AP lock **disabled**. IOT SSID BSS Transition remains **OFF** as requested. BSS disable alone did not prevent recurrence in the short observation. Device SSH remains enabled as Nathan requested.
- Some reason-2/reason-6 deauth logs coincided with a global AP radio/service reset. They do not independently prove spontaneous Panda-specific authentication failure. Historical controller 0 dBm entries are missing/invalid signal readings.
- Nathan reported Panda's own setup AP already disabled. Earlier cached rogue-AP sightings must not be presented as current or used to claim hotspot causality.
- The last verified healthy path was the upstairs U6 Lite (`10.16.69.106`, MAC `f4:92:bf:af:bb:04`), channel 11, approximately −79 dBm. Panda's full 212,141-byte page returned HTTP 200 in 0.586 seconds, followed by fresh MQTT and the successful drying cycle. A weaker signal worked; Nano proximity alone did not establish a reliable path. Recheck association before drawing new conclusions.

Existing UniFi cloud API-key access works through the connector's controller route `/network/api/s/default/`; direct local legacy access rejected that same key. Do not request email MFA merely because the direct local endpoint returns 401. Use the existing private profile `~/.config/unifi/agent-access.json` without printing credentials. Saving management SSH settings regenerated UniFi's internal `x_api_token`; supplied cloud-key access remained working. No raw token belongs in this repository.

## Validation evidence and remaining limits

Historical evidence retained outside Git records **260 passed in 23.44 seconds** on healthy Spinx, one isolated Docker test process, `--network none`, 2 CPUs / 2 GB. Tested source was `e84b054`; production revision `c5aa932` differs only by the Dockerfile writable-state-directory preparation. Non-root mounted-volume persistence checks passed. Earlier runs on proxmoxdocker remain explicitly untrusted because of suspected host RAM faults. This documentation closeout does not rerun or newly attest the application suite.

Transient configuration readback may reject the first contradictory post-write packet before a later echo arrives. That is an investigation lead, not an established root cause or an implemented fix. Network root cause and filament dryness remain unresolved. Keep future diagnostics read-only unless the specific action has been authorized.

## Local evidence and cleanup

Full session evidence was moved from `~/scratch/tmp/panda-*` and the two session bed-command scratch directories into private `~/docs/3d-printing/panda-session-evidence-20261111/`. The directory is mode 700 and archived files mode 600; it contains historical logs, full test outcomes (including failures), packet headers, rollback observations and private responses. Do not publish raw archives. The committed CSV contains only timestamps and two temperatures.

The scratch-hosted reconnect worktree was moved, with every file hash preserved, to `~/src/panda-cc2-safe-reconnect`. It still holds uncommitted historical code/test edits and an untracked generated skill; it was preserved rather than treated as disposable. `~/src/panda-preheat-startup` also remains, with its untracked generated skill. These branches are unmerged into `origin/main`; no branch was deleted. Main checkout planning files matched the saved reconnect commit byte for byte and are included in this documentation commit. Runtime `preheat_state.json` is preserved locally and ignored, never committed.
