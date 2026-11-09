# Native MQTT device transport (dedicated device broker)

Status: opt-in. The default deployment keeps using the device WebSocket and
changes nothing here. This page covers running the mod against a **dedicated,
authenticated Mosquitto broker for the Panda Breath itself**, independent of
Home Assistant (openspec change `native-panda-mqtt`).

## Why a second broker

The mod always spoke to the Panda over a WebSocket the Panda hosts itself.
Firmware V1.0.4 can instead *publish/subscribe MQTT* — but an MQTT client is a
client: **it needs a broker**, and the firmware's client must be pointed at one
during its setup step. Device traffic deliberately does **not** go through the
Home Assistant broker: commands and state for the heater never traverse HA, and
an HA restart or HA broker outage cannot touch or forge the device path.

```
                      ┌───────────────────────────┐   HA_MQTT_BROKER:1883
 Home Assistant ◄────►│  panda_backend (the mod)  │────────────────────────► HA broker
 (UI, automations)    │                           │      (mod's existing
                      │  device client (paho #2)  │       topics unchanged)
                      └─────────────┬─────────────┘
                                    │ panda_breath/<id>/state|command|availability
                                    ▼
                      ┌───────────────────────────┐
        Panda Breath ─│  panda_device_mqtt broker │  dedicated eclipse-mosquitto:2
        (V1.0.4)      │  host :1884 on 10.88.88.129│  authenticated, no HA relationship
                      └───────────────────────────┘
```

Two independent client connections leave the mod: the normal HA-broker one
(unchanged) and the device-broker one. Selection is explicit:

| `PANDA_DEVICE_TRANSPORT` | Device path | Notes |
|---|---|---|
| unset / `websocket` (default) | device WebSocket `ws://<PANDA_IP>/ws` | today's behaviour, always available |
| `mqtt` | dedicated device broker only | **requires `CC2_IP`/`CC2_HA_MODE`**; never silently falls back |

With `mqtt` misconfigured (non-CC2 deployment, or a missing host/password-file/
device-id setting) the mod **fails closed**: it logs `FAIL CLOSED`, opens no
device connection at all, publishes `…/device_available = OFF`, and never
substitutes a quiet WebSocket.

## What the native mode guarantees (and does not fake)

* `work_on` is the firmware's *enabled* flag — the device has no readable
  heater-relay field. Physical heating is reported separately: after a dry
  start is confirmed by readback, the mod watches chamber temperature and
  publishes `…/dry_heating_confirmed` (`pending` → `confirmed` after a ≥2 °C
  rise within 5 min, or `at_target` honestly when already at temperature).
* Nothing fabricates `fw_version` or relay states. Retained packets, other
  devices and malformed payloads never establish control. Telemetry older than
  10 s blocks ordinary writes (stops stay allowed).
* Boot and reconnect **never start drying**. A broker reconnect invalidates
  freshness; the mod replays no pending command, resumes no heater automation,
  and cannot restart a running or interrupted drying cycle.
* Emergency stop (HA tile / `heat_stop`) writes native OFF first and confirms
  it by device readback. Only if that is unconfirmed does it open **one**
  bounded device-WebSocket connection that may send exactly the off frame
  (`isrunning 0 / work_on false / set_temp 0`), then closes. If even that
  cannot be confirmed, the log and the status topic say
  `UNCONFIRMED — heater state UNKNOWN`. The mod never claims the heater is off.

## Step 1 — credentials, outside the repository

Choose a secrets directory outside this repo and keep it mode 700:

```bash
mkdir -p /home/nathan/secrets/panda-device && chmod 700 /home/nathan/secrets/panda-device
cd /path/to/BIQU-Panda-Breath-Mod

# 1a. The broker's hashed password file (username panda_mod). Generate the
#     hash with the mosquitto image so nothing installs locally; it writes
#     the file directly, the password is asked interactively:
docker run --rm -it -v /home/nathan/secrets/panda-device:/s:rw eclipse-mosquitto:2 \
  mosquitto_passwd -c /s/panda_device_mqtt_passwords panda_mod

# 1b. The mod's client password, same value, single-line file (no hash):
read -rs -p "same password again: " PW && printf '%s\n' "$PW" \
  > /home/nathan/secrets/panda-device/panda_device_mqtt_password
unset PW

chmod 600 /home/nathan/secrets/panda-device/*
```

Rules: never commit these files, never put passwords in `.env`, never print
them in logs or chat. The compose overlay only *binds* them read-only into the
two containers (`/run/secrets/...`). There is no default password anywhere.

## Step 2 — bring up the device broker

In the deployment's `.env` (this is where the *non-secret* choices live):

```bash
PANDA_SECRETS_DIR=/home/nathan/secrets/panda-device
PANDA_DEVICE_BIND_IP=10.88.88.129      # device-LAN host address; 1884 is published here only
PANDA_DEVICE_MQTT_USER=panda_mod
# PANDA_DEVICE_MQTT_ID — set in step 4, after the id has been seen live.
```

An empty device ID permits broker-only setup while the backend fails closed until the verified ID is supplied. Start only panda_device_mqtt during setup. The broker comes up with the overlay; validating first costs nothing and
cannot print secrets (compose resolves paths and checks schema only):

```bash
docker compose -f docker-compose.yml -f docker-compose.panda-native.yml config -q
docker compose -f docker-compose.yml -f docker-compose.panda-native.yml up -d panda_device_mqtt
```

`config -q` must exit 0. If `PANDA_DEVICE_BIND_IP`, `PANDA_SECRETS_DIR` or
`PANDA_DEVICE_MQTT_ID` are unset, compose stops with the reason in the message
— it never guesses and never shows secret values.

## Step 3 — bind the Panda and verify identity read-only

Binding the firmware to the broker is a **one-time setup write over the
device's own WebSocket** (the `ha` object). The mod itself never does this;
do it once from any MQTT-capable client and keep the credentials identical to
step 1:

```bash
# From any machine that can reach the Panda: send the setup object once.
# <PANDA_IP> is the Panda's address; port 1884 is the device broker's host port.
python3 - <<'EOF'
import asyncio, json, os, websockets
async def main():
    pw = open("/home/nathan/secrets/panda-device/panda_device_mqtt_password").read().strip()
    async with websockets.connect(f"ws://<PANDA_IP>/ws", open_timeout=10) as ws:
        await ws.send(json.dumps({"ha": {
            "ip": "10.88.88.129",       # PANDA_DEVICE_BIND_IP
            "port": 1884,
            "user": "panda_mod",
            "password": pw,
        }}))
        await asyncio.wait_for(ws.recv(), 10)  # consume privately; response may contain credentials
asyncio.run(main())
EOF
```

Watch the dedicated broker read-only using the client secret file. Do not print setup responses or credentials. This needs the same paho package already installed in the mod container; run there with its secret mounted:

```python
import os, pathlib, paho.mqtt.client as mqtt
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.username_pw_set("panda_mod", pathlib.Path("/run/secrets/panda_device_mqtt_password").read_text().strip())
c.on_connect = lambda c, u, f, r, p: c.subscribe("panda_breath/+/state")
c.on_message = lambda c, u, m: print(m.topic, "retained=" + str(m.retain))
c.connect("10.88.88.129", 1884, 30)
c.loop_forever()  # stop after observing the exact topic identity; no payload dump
```

Expect exactly one device id, `... online`/`... {"mode":...}` lines arriving
unsolicited (the firmware pushes ~1 Hz), **nothing marked retained**. Read the
id from the topic (`panda_breath/<THIS>/state`). Do **not** assume it is
MAC-derived — pin the id you actually saw. Note the state fields before
continuing: `mode`, `work_on`, `chamber_temp`, `drying_running`,
`custom_temp`, `custom_timer`, `drying_remaining_min` must all be present
(the mod refuses control until it sees that complete shape too).

If nothing arrives in 20 s: recheck the binding step (wrong port/ip/creds make
the firmware silent, not noisy) and the 1884 host firewall.

## Step 4 — cutover

Choose a moment when the Panda is idle (no drying cycle running; if one *is*
running, the cutover is still safe — the mod will not touch it — but proving
the new path is easier from idle).

```bash
# .env — pin the id verified in step 3:
PANDA_DEVICE_MQTT_ID=<id seen live in panda_breath/+/state>
```

Then restart the backend with the overlay in front of whatever base file the
deployment already uses (plain `docker-compose.yml`, or
`docker-compose.cc2.yml` for the macvlan IOT deployment — the overlay is
identical for both; the existing HA credentials, HA settings, certs and
preheat-state volumes are untouched by it):

```bash
docker compose -f docker-compose.yml -f docker-compose.panda-native.yml up -d
docker compose -f docker-compose.yml -f docker-compose.panda-native.yml logs panda_backend | grep -E "Device transport|NATIVE|FAIL CLOSED"
```

Success looks like: `[CONFIG] Device transport = dedicated MQTT broker ...;
device WebSocket disabled`, then HA entities
`…/device_transport = mqtt`, `…/device_available = ON`,
`…/device_status_age` ticking near zero, `…/dry_start_status = idle`,
`…/dry_heating_confirmed = none`. The old WebSocket diagnostic capture
(`ws_capture.py`) must see no persistent connection from the mod any more.

A drying start from the HA dryer tile then logs
`[DRY-STARTED] Native custom dry ... confirmed by device readback`, publishes
`dry_start_status = started`, and `dry_heating_confirmed` moves
`pending → confirmed` on a real temperature rise (or `at_target`). Rejected or
uncertain starts log `[DRY-REJECTED]`/`[DRY-UNCERTAIN]` and are never retried
behind your back — an uncertain latch blocks the next start until a fresh live
packet resolves it.

## Step 5 — rollback (always available)

Native mode is one env var wide. To go back to the WebSocket:

```bash
docker compose -f docker-compose.yml up -d          # drop the overlay from the command
# — or, keeping the overlay mounted, explicitly select the legacy transport in .env:
PANDA_DEVICE_TRANSPORT=websocket
```

Restart the backend; the mod re-opens the device WebSocket, HA topics keep
their names, and nothing else about the deployment changed. The device broker
container can be left running (`docker compose ... stop panda_device_mqtt`) —
it holds only the device's own topics, and its volume retains availability/
state telemetry only, never commands.

## Limits of v1

* `mqtt` mode requires CC2 mode (`CC2_IP`/`CC2_HA_MODE`); legacy emulation
  explicitly selecting MQTT on unsupported legacy mode fails closed. Default legacy deployments retain WebSocket.
* The mod **never** changes the Panda's broker binding for you — that is the
  one-time `ha` setup object in step 3. Reverting the binding to the old HA
  broker (if that is where it pointed before) is also a manual `ha` object
  write, done only when you decide to abandon native mode entirely.
* Reconnects are observational. A new valid chamber heat request authorizes subsequent chamber control on that connection; saved preheat state and old telemetry cannot resume it. PETG and native timed-drying ownership remain protected.
* No `--env-file` printing, no broker payload logging: nothing in this overlay
  writes secrets or message payloads to logs.
