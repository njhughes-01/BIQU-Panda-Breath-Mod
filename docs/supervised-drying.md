# Supervised drying (native MQTT mode)

What it does: once you deliberately start filament drying, the mod keeps that
drying session alive through Panda network failures and unexpected stops —
including a stop caused by the **physical OFF button on the Panda**, which
Nathan explicitly authorized (automatic restart wins; only the mod Stop
controls cancel). This replaces the earlier "observe only, never resume"
reconnect rule for supervised sessions. Ordinary (non-supervised) heating
behaviour, PETG/CC2 print rules and all device temperature regulation are
unchanged.

## Starting a supervised session

Use the existing drying controls exactly as before:

- HA dry button press — starts with the drying temperature/time currently
  shown in the mod (`dry_temp` 40–60 °C; `dry_time` is displayed in minutes
  and must represent whole hours, 1–99).
- JSON press to `<prefix>/drying/set` —
  `{"temperature_c": 50, "duration_hours": 8}` (whole hours are canonical).

That JSON describes the interface, not permission to run another cycle. The
previously authorized 50°C/eight-hour cycle is complete; its findings do not
authorize another start.

Before anything is armed the mod requires **fresh live device telemetry with
drying OFF** (a device reporting drying running is refused; a stale countdown
number alone does not count as running). Arming only writes the session file —
no device command goes out at press time. The supervisor then performs the
first start itself after two distinct fresh live snapshots confirm the device
idle (a couple of seconds later). A second press while a supervised session is
active is rejected and does **not** move the finish time.

Direct native temperature selection accepts only finite 40–60 °C values before
integer conversion; invalid input leaves the prior selection unchanged and
sends nothing. A valid selection writes only `custom_temp`, without starting
or resetting drying. Generic native `work_on ON` is refused in Dry, during a
running session or pending Stop, with stale telemetry or after an intervening
cancel/lock. An accepted fresh non-Dry ON enables only firmware `work_on`;
explicit Dry is the start control and the supervisor retains timer ownership.

## The finish time is fixed

The session's finish time is `start + duration` in real wall-clock time and
never moves — reconnects, backend restarts and outages do not extend it, and
outage time counts against it. At the finish time the mod requests the device
Stop; if the device is unreachable at that moment the owed Stop is persisted
and delivered as soon as the device is reachable again, without ever restarting
the drying.

The native firmware timer only accepts whole hours, so the device's own backup
timer is rounded up and may run up to about one hour past the deadline (plus
command latency, firmware behaviour unconfirmed). The exact finish is owned by
the mod's host-side countdown and its Stop request.

## Recovery rules (what may restart the drying)

Every ~1 s the supervisor looks at fresh device state:

- Drying running → observe only; the running device timer is never rewritten.
- Drying OFF before the deadline → after **two distinct fresh live
  snapshots** say OFF, the session is restarted at its target temperature with
  the remaining duration (rounded up to whole hours), at most one attempt per
  minute.
- A start whose outcome is uncertain is never blindly repeated: newer fresh
  device telemetry must resolve it first.
- Reported faults inhibit recovery when visible; the current native fields
  cannot prove absence of a thermal fault, so keep that in mind.
- A persistence failure, an invalid session file, a clock anomaly, an elapsed
  deadline or any lock/power-off state prohibits any ON write.
- An idle device is never adopted: only an explicit drying press creates a
  session.

## Cancelling

Any of these cancels the session permanently (the cancellation is persisted
before a device command is requested, and a cancelled session never rearms):

- mod **Stop / heat-stop button** (emergency stop and lock)
- mod **Panda power OFF** switch
- mod **work_on OFF** control
- any **mod mode change** (Manual, Automatic, or the mode-select dropdown)
- a mod lock

The **Panda's physical OFF button alone does not cancel** — that stop is
restarted automatically, as authorized. Under the same existing general OFF
rule, a Panda-panel mode change that reports drying OFF may also restore Dry
before the fixed deadline. The mod does not infer panel cancellation from a
mode label; use mod Stop to cancel. If a cancellation Stop cannot be
confirmed by fresh device telemetry, the mod keeps reporting the owed Stop
(`pending_stop`) instead of claiming success.

## Status and telemetry

New retained topic: `<prefix>/dry_session`, JSON, mirrored when it changes:

```json
{"status": "running|cancelled|completed|inactive", "session_id": "…",
 "target_c": 50, "deadline": 1780000000.0, "pending_stop": false,
 "recovering": false}
```

`running` here means an armed timer session, not proof of physical heating.
The supervisor owns the initial start as well as recovery; ordinary reconnect
alone cannot arm a session. Existing `dry_start_status` uses `idle`,
`confirming`, `confirmed`, `failed` or `uncertain`, never `started`.
`dry_heating_confirmed` separately uses `unknown`, `none`, `pending`,
`confirmed`, `at_target` or `unconfirmed`. `confirmed` means a ≥2 °C rise within
5 minutes during the current continuously observed running Dry episode; it
never measures the physical relay or guarantees the element is energized now.
Fresh Dry OFF, work OFF or non-Dry mode, an accepted Stop or a new start
attempt invalidates old proof. A disconnect, availability loss or telemetry
gap over 10 s also ends that observation. Fresh inactive state reports `none`;
unavailable/stale state reports `unknown`. Binary Heat is ON only for current
confirmed rise evidence, and Heat OFF does not prove physical OFF.
A confirmed timer alone is not rise proof; `at_target` means the temperature
was already at target. Fresh post-write telemetry may confirm device-reported
OFF and clear an owed Stop; physical relay state remains unknown.

Arming logs `[DRY-SUP] armed ...` / `>>> DRY SUPERVISED: ...`; the supervisor
logs `[DRY-SUP] recovery start confirmed by device readback` for a confirmed
initial or recovery start. Do not expect the old direct-start log. A transient
configuration-readback lag remains an unproven lead, not an established cause
of the historical startup trouble or a change made by this integration.

## State file and deployment

- Path: `PANDA_DRY_SESSION_FILE`, default `<install dir>/drying_session.json`
  (next to the preheat state file). It is runtime state, never repo content.
- A backend restart loads the persisted session from this file; recovery still
  requires the fresh-state and uncertainty guards above and retains the same
  deadline. Cancelled/completed sessions never rearm; an owed Stop survives.
  A deployment that **recreates** the container must give the file a
  persistent home, e.g. add a volume and point the env at it:

  ```yaml
  volumes:
    - panda_dry_state:/app/dry_state
  environment:
    PANDA_DRY_SESSION_FILE: /app/dry_state/drying_session.json
  ```

  Without such a mount, an armed session is lost when the container is
  recreated. The reusable stock native overlay does not provision this volume;
  its operator must do so. The deployment recorded in the findings **already
  has** `panda_panda_dry_state` mounted at `/app/dry_state` and the session env
  path set as above. Preserve that volume and the existing settings, secrets,
  preheat persistence and entrypoint/configuration bypass on recreation.
- Packaging: the production Dockerfile copies the whole directory
  (`COPY . .`), including `drying_supervisor.py`, and prepares a writable
  `/app/dry_state` for the non-root runtime user. `.dockerignore` excludes local
  preheat/session files, the runtime state directory and atomic-write remnants;
  runtime intent must come from the deployment's persistence, not an image.

## Rollback for the supervised deployment

Use the [native MQTT rollback procedure](native-mqtt.md#step-5--supervised-image-rollback)
and parent-owned task 6 in the
[integration plan](../openspec/changes/mqtt-timer-integration/tasks.md).
Restore the captured prior supervised image `panda-supervised:20261110-reviewed`
at its verified image ID with the prior effective Compose configuration,
changing only the backend image and retaining **current** settings and state.
Defer recreation for a running/recovering session or pending Stop; require a
naturally terminal session and fresh device-reported OFF. Never roll back runtime
session data, drop the native overlay, switch to the pre-supervisor image,
remove volumes or start a new cycle to verify rollback.

The approved deployment and completed live cycle are recorded in
[Panda findings, 2026-11-11](panda-findings-20261111.md).
