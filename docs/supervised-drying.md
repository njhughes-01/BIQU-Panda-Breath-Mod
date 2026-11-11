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

Before anything is armed the mod requires **fresh live device telemetry with
drying OFF** (a device reporting drying running is refused; a stale countdown
number alone does not count as running). Arming only writes the session file —
no device command goes out at press time. The supervisor then performs the
first start itself after two distinct fresh live snapshots confirm the device
idle (a couple of seconds later). A second press while a supervised session is
active is rejected and does **not** move the finish time.

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
- **Panda power OFF** switch
- **work_on OFF**
- any **mode change** (Manual, Automatic, or the mode-select dropdown)

The **Panda's physical OFF button alone does not cancel** — that stop is
restarted automatically, as authorized. If a cancellation Stop cannot be
confirmed by fresh device telemetry, the mod keeps reporting the owed Stop
(`pending_stop`) instead of claiming success.

## Status and telemetry

New retained topic: `<prefix>/dry_session`, JSON, mirrored when it changes:

```json
{"status": "running|cancelled|completed|inactive", "session_id": "…",
 "target_c": 50, "deadline": 1780000000.0, "pending_stop": false,
 "recovering": false}
```

Existing `dry_start_status` and `dry_heating_confirmed` topics are unchanged.

## State file and deployment

- Path: `PANDA_DRY_SESSION_FILE`, default `<install dir>/drying_session.json`
  (next to the preheat state file). It is runtime state, never repo content.
- A backend restart with the same container resumes the session from this
  file. A deployment that **recreates** the container must give the file a
  persistent home, e.g. add a volume and point the env at it:

  ```yaml
  volumes:
    - panda_dry_state:/app/dry_state
  environment:
    PANDA_DRY_SESSION_FILE: /app/dry_state/drying_session.json
  ```

  Without such a mount, an armed session is lost when the container is
  recreated. This is documented, not pre-configured — live deployment is a
  separate approval.
- Packaging: the production Dockerfile copies the whole directory
  (`COPY . .`), including `drying_supervisor.py`, and prepares a writable
  `/app/dry_state` for the non-root runtime user.

The approved deployment and completed live cycle are recorded in
[Panda findings, 2026-11-11](panda-findings-20261111.md).
