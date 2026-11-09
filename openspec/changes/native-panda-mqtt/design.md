# Native Panda MQTT migration

## Intent and scope

Nathan requested native MQTT because repeated WebSocket connections and unacknowledged writes have prevented reliable drying. Keep the existing mod UI, MQTT controls, CC2 rules, saved settings and timed-drying safeguards. Change the device connection; do not flash firmware or change Wi-Fi, AP, printer, bed, nozzle or leveling settings.

## Approach

Nathan clarified after approval that the device path must not use the HA broker. Use a dedicated authenticated Mosquitto broker alongside the mod for the firmware V1.0.4 native client and the mod's separate device client. Keep the existing HA broker connection only for the mod's existing UI/HA controls and telemetry. Device command/state delivery never traverses HA or its broker. Select exactly one routine device transport through explicit configuration. In MQTT mode do not open a persistent device WebSocket or silently fall back to it. Keep legacy WebSocket mode selectable for rollback and older firmware.

A bridge that kept WebSocket control and added MQTT telemetry would leave the unacknowledged command path in place. Direct Home Assistant control alone would bypass the mod's existing CC2 safeguards. Neither meets this task.

## Protocol evidence

The official V1.0.4 binary's Home Assistant discovery templates and the upstream OctoPrint-PandaBreath MQTT adapter agree on these device topics and flat command fields:

- `panda_breath/<device-id>/state`, `/command`, `/availability`.
- `target_temp`, `filter_temp`, `heater_temp`, `custom_temp`, `custom_timer` are numeric. `custom_timer` is whole hours, 1–99.
- `mode`: `auto mode`, `power on`, `filament drying`.
- `filament_drying_mode`: `pla`, `petg`, `custom`.
- `work_on` and `drying_running`: `ON` or `OFF`.
- State reports `chamber_temp` and `drying_remaining_min` (minutes).

MQTT messages are not WebSocket `settings` envelopes. Binding the firmware to the broker uses its setup-only WebSocket `ha` object with `ip`, `port`, `user`, `password`. Runtime state must establish the exact device topic and field shapes before writes are enabled; do not select the first wildcard device.

Sources: official BIGTREETECH Panda Breath V1.0.4 firmware, https://global.bttwiki.com/Panda_Breath.html, https://github.com/ajimaru/OctoPrint-PandaBreath/blob/main/octoprint_pandabreath/mqtt_bridge.py.

## Control and state

Translate existing device operations through one transport boundary, including stop and safety-lock paths. Feed normalized native telemetry into the same state/control policy used today. Keep native availability and last live telemetry time separate from the mod's broker connection status.

Pin the verified device identity; ignore other devices and retained control messages. Commands are non-retained and are never queued for replay after reconnect. Reject ordinary writes when non-retained device telemetry is older than 10 seconds, or native availability is offline. A broker reconnect invalidates previous freshness until a new live packet arrives. Broker acknowledgement proves broker delivery only; it is not device acceptance. Publish timer-start commands at QoS 0 with no offline queue; their outcome is established by readback, not retransmission.

For an explicit drying start, set the validated custom temperature/hours and mode, wait up to 10 seconds for matching live telemetry received after the configuration write, then start the timer once. Wait up to 10 seconds for subsequent device-reported drying running and positive remaining time bounded by the requested duration. Observe a rise of at least 2°C above the pre-start temperature within five minutes before reporting heating; if already at target, report timer running without claiming a new temperature rise. On ambiguity, report unconfirmed and do not retry the start. Keep an uncertain-start latch until fresh telemetry resolves running/idle; duplicate requests cannot clear it or reset the cycle. Reject a new start if a fresh snapshot already shows an active cycle. Preserve explicit stop cancellation ordering.

Subscribe/reconnect is observational: no printer binding, mode change, timer reset, heater-on, restart or automatic resume. Do not stop a timed dry cycle because the printer is idle or finished. Preserve the PETG no-chamber-heating rule for print-driven operation.

Explicit stop and emergency locks remain available through the selected transport, even when ordinary writes are blocked for stale telemetry. Preserve an independent, bounded one-shot WebSocket off path for an explicitly requested emergency stop if native MQTT stop cannot be delivered or confirmed. It may only send off/stop, never setup, binding, mode, target or on/start; it closes afterward and is not a persistent fallback. Broker/telemetry loss alone marks the device unavailable and blocks new starts; it does not send heater commands or interrupt a native timed cycle. A failed stop on both transports must be clearly reported as unconfirmed; never claim the heater is off without device evidence.

## Credentials and cutover

Generate a dedicated device-broker credential outside the repository with restrictive permissions; use Docker secret files rather than printing values or putting them in committed environment files. The existing HA credentials and connection are unchanged. Read and preserve the current device configuration. Configure only the Panda's native broker binding and the mod's transport selection. Keep exact rollback configuration outside the repository with restrictive permissions. Do not enable duplicate HA automations.

First subscribe read-only and verify native state and identity. Prepare and review the container change before requesting the required production deployment approval. At cutover ensure no competing persistent WebSocket client is running. Preserve an existing drying cycle; do not reset or restart it as a migration test. Live heater testing uses only the previously authorized 50°C / 8-hour drying request, with fresh idle state established first.

## Done when

1. Protocol translation, device identity filtering, retained/stale-state rejection, no command replay, cancellation/stop, and reconnect-without-control are covered by meaningful containerized tests.
2. Existing CC2 drying and PETG safeguards pass regression tests on the final source tree.
3. The reviewed container can run in MQTT mode with no persistent device WebSocket and settings preserved; rollback is concrete.
4. Live native device telemetry proves broker connectivity, selected identity, temperature and timer. A drying start is reported successful only after running/timer readback, with heating separately confirmed by rising temperature.

## Open facts before implementation packet

Read-only broker subscription succeeded, but no native Panda state arrived in ten seconds. Current device broker binding and the firmware's exact live device ID still need verification. Source exploration must identify every direct WebSocket write and safety path before the implementation tasks are closed.
