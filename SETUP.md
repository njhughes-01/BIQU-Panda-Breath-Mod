# Panda Breath Mod Setup Guide

This guide covers Docker setup for the Panda Breath mod. The Panda backend automates your BIQU Panda Breath chamber heater using real-time Home Assistant data.

## Prerequisites

- **Docker** with **buildx** and **Docker Compose** installed (buildx is included with Docker Desktop and Docker Engine 23+)
- **Home Assistant** running with **Mosquitto MQTT add-on** enabled
- **BIQU Panda Breath** hardware (required)
- **Elegoo Centauri Carbon 2** (optional — for CC2 sensor data and slicer-priority automation)
- Network connectivity between your Docker host and printers/HA instance

## Quick Start

The pre-built multi-arch image (`linux/amd64`, `linux/arm64`) is published to GitHub Container Registry. No local build required.

---

### Setup A — Klipper/Moonraker (Panda Touch optional)

```bash
curl -O https://raw.githubusercontent.com/njhughes-01/BIQU-Panda-Breath-Mod/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/njhughes-01/BIQU-Panda-Breath-Mod/main/.env.example
cp .env.example .env
nano .env        # set PANDA_IP, PANDA_SN, PANDA_ACCESS_CODE, HA_TOKEN

docker compose up -d
```

---

### Setup B — Elegoo Centauri Carbon 2 (no Panda Touch required)

Curl the CC2 compose file once, choose a sub-option below, fill in `.env`, then start.

```bash
curl https://raw.githubusercontent.com/njhughes-01/BIQU-Panda-Breath-Mod/cc2-docker-integration/docker-compose.cc2.yml -o docker-compose.yml
curl -O https://raw.githubusercontent.com/njhughes-01/BIQU-Panda-Breath-Mod/cc2-docker-integration/.env.example
cp .env.example .env
```

> **CC2 LAN-only mode required** (both sub-options) — on the printer:
> **Settings → Network → LAN Only Mode → Enable**

#### Sub-option A — cc2_connector (direct CC2 bridge, no HA integration needed)

Use this if you do **not** have the `danielcherubini/elegoo-homeassistant` HA integration.
The `cc2_backend` container connects directly to the CC2's MQTT broker and bridges sensor data
into Home Assistant.

In `.env`, uncomment and fill:

```env
CC2_IP=<your CC2 IP>
CC2_SN=<your CC2 serial>          # Settings → About on the printer
HA_MQTT_BROKER=<your HA IP>
HA_MQTT_USER=your_mqtt_username
HA_MQTT_PASS=your_mqtt_password   # quote if the password contains #: HA_MQTT_PASS="P@ss#word"
PANDA_SN=01P00A123456789          # dummy — leave as-is for CC2 mode
PANDA_ACCESS_CODE=01P00A12        # dummy — leave as-is for CC2 mode
```

Start with the `cc2-connector` profile:

```bash
docker compose --profile cc2-connector up -d
```

#### Sub-option B — elegoo-homeassistant (read CC2 data via HA REST API)

Use this if you already have [`danielcherubini/elegoo-homeassistant`](https://github.com/danielcherubini/elegoo-homeassistant) installed in HA.
No `cc2_backend` container is needed — the Panda backend reads CC2 sensor data directly from
HA entities via the REST API.

**HA setup (one-time):**
1. In HA: **Settings → People → Users** (not People — it must be the Users tab) → **Add User**
2. Create a user (e.g. `pandamod`) for MQTT auth — this is separate from your personal HA account
3. In HA: **Profile → Long-Lived Access Tokens → Create Token** — copy it for `HA_TOKEN`

In `.env`, uncomment and fill:

```env
CC2_HA_MODE=true
HA_TOKEN=<your long-lived access token>
HA_BASE_URL=http://<your HA IP>:8123   # or http://homeassistant:8123 if on same Docker network
HA_MQTT_BROKER=<your HA IP>
HA_MQTT_USER=pandamod
HA_MQTT_PASS=yourpassword              # quote if the password contains #: HA_MQTT_PASS="P@ss#word"
PANDA_SN=01P00A123456789               # dummy — leave as-is for CC2 mode
PANDA_ACCESS_CODE=01P00A12             # dummy — leave as-is for CC2 mode
```

Start (no profile needed — cc2_backend is not used):

```bash
docker compose up -d
```

The backend auto-discovers all required CC2 entities from HA on startup. Check the logs:

```bash
docker compose logs panda_backend | grep CC2-HA
```

You should see `[CC2-HA] Mode active — polling 6 entities every 3s` within a few seconds.

---

The stack exposes a browser control surface at:

```text
http://<DOCKER_HOST>:8088
```

Override the host port with `PANDA_WEB_PORT` if needed.

To build from source instead of pulling from GHCR, clone the repo and add `build: { context: . }` to the services in `docker-compose.yml`.

---

## Environment variables: quick reference

### Setup A (standard / Klipper)

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `PANDA_IP` | **yes** | — | IP of the device running Klipper/Moonraker |
| `PANDA_SN` | **yes** | — | Printer serial number |
| `PANDA_ACCESS_CODE` | **yes** | — | Printer access code |
| `HA_TOKEN` | **yes** | — | HA long-lived access token |
| `HA_MQTT_BROKER` | no | `mosquitto` | Override if MQTT is on a separate host |
| `HA_MQTT_PORT` | no | `1883` | |
| `HA_MQTT_USER` / `HA_MQTT_PASS` | if broker requires auth | _(empty)_ | Quote passwords containing `#` |
| `HA_BASE_URL` | no | `http://homeassistant:8123` | Override for external HA |
| `PANDA_HOST_IP` | no | _auto-detected_ | Docker host LAN IP; auto-detected from `PANDA_IP` |
| `PANDA_WEB_PORT` | no | `8088` | Host port for browser control UI |

### Setup B — Sub-option A (cc2_connector)

All of the above, plus:

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `CC2_IP` | **yes** | — | Elegoo CC2 printer IP |
| `CC2_SN` | **yes** | — | CC2 serial number (Settings → About on the printer) |
| `PANDA_SN` | no | `01P00A123456789` | Dummy value — required by entrypoint but unused in CC2 mode |
| `PANDA_ACCESS_CODE` | no | `01P00A12` | Dummy value — required by entrypoint but unused in CC2 mode |
| `CC2_USER` | no | `elegoo` | CC2 MQTT username |
| `CC2_PASS` | no | `123456` | CC2 MQTT password |
| `CC2_TOPIC_PREFIX` | no | `cc2` | Must match in both `cc2_backend` and `panda_backend` |

### Setup B — Sub-option B (elegoo-homeassistant)

| Variable | Required? | Default | Description |
|----------|-----------|---------|-------------|
| `CC2_HA_MODE` | **yes** | — | Set to `true` to enable HA REST polling instead of direct CC2 bridge |
| `HA_TOKEN` | **yes** | — | HA long-lived access token (Profile → Long-Lived Access Tokens) |
| `HA_BASE_URL` | **yes** | `http://homeassistant:8123` | URL of your HA instance |
| `HA_MQTT_BROKER` | **yes** | — | IP or hostname of your Mosquitto broker |
| `HA_MQTT_USER` / `HA_MQTT_PASS` | if broker requires auth | _(empty)_ | Quote passwords containing `#` |
| `PANDA_SN` | no | `01P00A123456789` | Dummy value — required by entrypoint but unused in CC2 mode |
| `PANDA_ACCESS_CODE` | no | `01P00A12` | Dummy value — required by entrypoint but unused in CC2 mode |
| `HA_CC2_CHAMBER_ENTITY` | no | _auto-discovered_ | Override auto-discovered CC2 chamber temp entity |
| `HA_CC2_STATUS_ENTITY` | no | _auto-discovered_ | Override CC2 print status entity |
| `HA_CC2_PAUSE_ENTITY` | no | _auto-discovered_ | Override CC2 pause button entity |
| `HA_CC2_RESUME_ENTITY` | no | _auto-discovered_ | Override CC2 resume button entity |

---

## Panda Backend Setup

The Panda backend uses **Panda.py** to communicate with your BIQU Panda device.

### Configure runtime environment

Docker generates `panda_config.json` inside the container from runtime environment variables. Do not edit `panda_config.json` for Docker deployments. For local Compose, put overrides in `.env`; for Portainer/Git stacks, set them in the stack environment.

### Register in Panda UI (optional — only if you have a Panda Touch display)

A Panda Touch is **not required**. The chamber heater automation, MQTT control, and the browser UI all work without one.

If you do have a Panda Touch:
1. Open the Panda Touch UI
2. Use Klipper/direct binding — do not scan
3. Set `Printer IP` to `PANDA_HOST_IP` (shown in `panda_backend` logs)
4. The backend handles the WebSocket connection on port 8883

### Verify Connection

```bash
docker compose logs -f panda_backend
```

Look for the generated config message and `[WS] Bind confirmed`.

---

## CC2 Backend Setup (Sub-option A — cc2_connector only)

> Skip this section if you are using Sub-option B (CC2_HA_MODE).

The `cc2_backend` service uses **cc2_connector.py** to bridge your Elegoo CC2's internal MQTT broker into Home Assistant.

> **⚠️ LAN-only mode required**
> The CC2's MQTT broker is only accessible when the printer is in LAN-only mode.
> On the printer: **Settings → Network → LAN Only Mode → Enable**

### Configure

Only `CC2_IP` and `CC2_SN` normally need to be supplied. Other values have Compose defaults:

```env
CC2_IP=<CC2_IP>
CC2_SN=<YOUR_SN>
# Defaults (override only if needed):
# CC2_USER=elegoo
# CC2_PASS=123456
# CC2_TOPIC_PREFIX=cc2
```

**CC2 Requirements:**
- Printer on the same LAN as the Docker host
- CC2 in **LAN-only mode**
- Default credentials: `elegoo` / `123456` — if you've set a custom access code, use it as `CC2_PASS`

### Start

```bash
docker compose --profile cc2-connector up -d
```

### Verify CC2 Connection

```bash
docker logs cc2_backend
```

Look for: `Connected to CC2 MQTT broker` and `Registration successful`.

### Verify Home Assistant Autodiscovery

In Home Assistant: **Settings → Devices & Services → MQTT** — look for the **Centauri Carbon 2** device with these sensors:

- `cc2_nozzle_temp`, `cc2_bed_temp`, `cc2_chamber_temp`
- `cc2_print_status`, `cc2_print_progress`

---

## Home Assistant Sensors Created by cc2_backend (Sub-option A)

| Entity ID | Description | Unit |
|-----------|-------------|------|
| `sensor.centauri_carbon_2_nozzle_temperature` | Nozzle temp | °C |
| `sensor.centauri_carbon_2_nozzle_target_temperature` | Nozzle setpoint | °C |
| `sensor.centauri_carbon_2_bed_temperature` | Bed temp | °C |
| `sensor.centauri_carbon_2_bed_target_temperature` | Bed setpoint | °C |
| `sensor.centauri_carbon_2_chamber_temperature` | Chamber temp | °C |
| `sensor.centauri_carbon_2_print_status` | Print state | — |
| `sensor.centauri_carbon_2_print_progress` | Progress | % |

---

## Running the CC2 variant

Start all services (Sub-option A with cc2_backend):

```bash
docker compose --profile cc2-connector up -d
```

Start Sub-option B (elegoo-homeassistant, no cc2_backend):

```bash
docker compose up -d
```

View logs:

```bash
docker compose logs -f
```

Stop all:

```bash
docker compose down
```

---

## Troubleshooting

### CC2 MQTT connection fails (Sub-option A)

**Error:** `Connection refused` or `timed out`

- Verify CC2 IP with `ping <CC2_IP>`
- Check CC2 is on 2.4 GHz WiFi
- Verify LAN-only mode is enabled on the printer
- Try resetting credentials to `elegoo` / `123456`

### HA REST API unreachable (Sub-option B)

**Error:** `[CC2-HA] HA state query failed` in panda_backend logs

- Check `HA_BASE_URL` is reachable from the Docker host: `curl http://<HA_IP>:8123/api/`
- Verify `HA_TOKEN` is valid — test: `curl -H "Authorization: Bearer <token>" http://<HA_IP>:8123/api/`
- Ensure the `danielcherubini/elegoo-homeassistant` integration is installed and the CC2 device shows sensors in HA

### No CC2 entities discovered (Sub-option B)

**Log:** `[CC2-HA] Discovery attempt N: no CC2 entities found`

- Check the elegoo-homeassistant integration has entities with "elegoo", "centauri", or "carbon" in their entity IDs
- Verify the integration is connected — look for `sensor.elegoo_centauri_carbon_2_*` entities in HA
- If entity naming differs, set manual overrides: `HA_CC2_CHAMBER_ENTITY`, `HA_CC2_STATUS_ENTITY`, etc.

### MQTT "Not authorized" (Sub-option B)

**Log:** `CONNECT rc=Not authorized`

- In HA: **Settings → People → Users** (the Users tab, not People) — confirm the user exists
- Passwords containing `#` must be quoted in `.env`: `HA_MQTT_PASS="Killpop2#"`
- Verify the MQTT user has access enabled in the Mosquitto add-on configuration

### Panda WebSocket fails

**Error:** `Connection refused` or `ws: unexpected close`

- Check firewall allows inbound port 8883
- Verify the `panda_certs` Docker volume exists

### HA autodiscovery doesn't create sensors

- Verify `HA_MQTT_BROKER` and credentials in `.env`
- Check Mosquitto add-on is running: **Settings → Add-ons → Mosquitto broker**
- Restart backend: `docker compose restart panda_backend`

### How TLS works in this stack

The Panda backend listens on port 8883 with TLS. Certificates are generated at **container startup**, not at build time, and persisted across restarts in the `panda_certs` Docker volume. Nothing to configure. To force regeneration: `docker volume rm panda_certs`, then restart.

---

## Further Help

- Check container logs: `docker compose logs -f [service_name]`
- Inspect env: `docker compose exec panda_backend env | grep -E "CC2_|HA_"`
- Test MQTT: `mosquitto_sub -h <HA_IP> -u user -P pass -t "panda_breath_mod/#"`
- Open web control: `http://<DOCKER_HOST>:8088`
