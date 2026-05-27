# Panda Breath Mod Setup Guide

This guide covers Docker setup for the Panda Breath mod. The Panda backend automates your Panda Breath chamber heater using real-time Home Assistant data. If your printer is an Elegoo Centauri Carbon 2, run the CC2 backend to feed its sensor data into the automation.

## Prerequisites

- **Docker** with **buildx** and **Docker Compose** installed (buildx is included with Docker Desktop and Docker Engine 23+)
- **Home Assistant** running with **Mosquitto MQTT add-on** enabled
- **BIQU Panda Breath** hardware (required)
- **Elegoo Centauri Carbon 2** (optional — for CC2 sensor data in HA)
- Network connectivity between your Docker host and printers/HA instance

## Quick Start

```bash
git clone https://github.com/njhughes-01/BIQU-Panda-Breath-Mod.git
cd BIQU-Panda-Breath-Mod

# Optional for local runs: copy .env.example to .env and fill device credentials.
# Portainer/Git stacks can set required values as stack environment variables.

docker compose --profile panda up -d
```

Compose provides stable defaults for internal stack addresses: `HA_MQTT_BROKER=mosquitto`, `HA_BASE_URL=http://homeassistant:8123`, and `HA_MQTT_PORT=1883`. Override them only if Home Assistant or Mosquitto are outside this stack. TLS certificates are generated automatically on first run and persisted in a Docker volume — no manual cert generation needed.

For the full Panda Breath + CC2 setup, use both profiles:

```bash
docker compose --profile panda --profile cc2 up -d
```

## Using an Elegoo Centauri Carbon 2?

If your printer is an Elegoo CC2, run the CC2 backend alongside the Panda backend to publish its sensor data to Home Assistant. The Panda Breath automation can then use the CC2's live temps.

> **LAN-only mode required** — on the CC2: **Settings → Network → LAN Only Mode → Enable**
> Without this, the CC2's MQTT broker is not reachable.

Provide `CC2_IP` and `CC2_SN` as stack environment variables or in `.env`, then:

```bash
docker compose --profile cc2 up -d cc2_backend
```

---

See individual setup sections below for full configuration details.

---

## Panda Backend Setup

The Panda backend uses **Panda.py** to communicate with your BIQU Panda printer.

### 1. Configure runtime environment

Docker generates `panda_config.json` inside the container from runtime environment variables. Do not edit `panda_config.json` for Docker deployments. For local Compose, put overrides in `.env`; for Portainer/Git stacks, set them in the stack environment.

| Variable | Required? | Description | Default |
|----------|-----------|-------------|---------|
| `PANDA_IP` | yes | Panda device IP, WebSocket at `ws://{IP}/ws` | none |
| `PANDA_SN` | yes | Panda/Bambu serial number used for binding | none |
| `PANDA_ACCESS_CODE` | yes | Panda/Bambu access code | none |
| `PANDA_HOST_IP` | usually no | Docker host LAN IP to register in Panda UI | auto-detected from `PANDA_IP` when possible |
| `HA_BASE_URL` | no | Home Assistant URL | `http://homeassistant:8123` |
| `HA_TOKEN` | yes | Long-lived access token from HA | none |
| `HA_MQTT_BROKER` | no | MQTT service hostname or broker IP | `mosquitto` |
| `HA_MQTT_PORT` | no | MQTT broker port | `1883` |
| `HA_MQTT_USER` / `HA_MQTT_PASS` | if broker requires auth | HA MQTT credentials | empty |
| `PANDA_MQTT_TOPIC_PREFIX` | no | Topic base prefix | `panda_breath_mod` |

### 2. Register in Panda UI

1. Open Panda Touch UI
2. Use Klipper/direct binding; do not use scan
3. Set `Printer IP` to `PANDA_HOST_IP`
4. Connect — Panda will establish WebSocket to the Docker host on port 8883

The serial number and access code are provided by the Docker backend from `.env` as `PANDA_SN` and `PANDA_ACCESS_CODE`. If a Bambu printer-type screen does not expose SN/access-code fields, use the Klipper/direct binding path and let the backend send those values.

### 3. Verify Connection

```bash
docker compose logs -f panda_backend
```

Look for the generated config message, the auto-detected `PANDA_HOST_IP` if you did not set it manually, and `[WS] Verbunden mit Panda`.

### Panda Control GUI

Docker Compose runs the headless backend only. The upstream **Panda Control GUI** is `PandaGui.py`, a PySide desktop app, and is not exposed as a web UI by Compose.

For Docker deployments, use Home Assistant MQTT entities for control and monitoring. If you need the desktop GUI, run it separately on a machine with Python, PySide6, and access to the same MQTT broker.

---

## CC2 Backend Setup

The CC2 backend uses **cc2_connector.py** to communicate with your Elegoo Centauri Carbon 2 printer.

> **⚠️ LAN-only mode required**
> The CC2's MQTT broker is only accessible when the printer is in LAN-only mode.
> On the printer: **Settings → Network → LAN Only Mode → Enable**
> Without this, the MQTT port will not be reachable and the connector will fail to connect.

### 1. Configure runtime environment

Only `CC2_IP` and `CC2_SN` normally need to be supplied. Other values have Compose defaults:

```env
CC2_IP=<CC2_IP>
CC2_SN=<YOUR_SN>
# Defaults:
# CC2_USER=elegoo
# CC2_PASS=123456
# CC2_TOPIC_PREFIX=cc2
# HA_MQTT_BROKER=mosquitto
# HA_MQTT_PORT=1883
```

**CC2 Requirements:**
- Printer must be on the same LAN as the Docker host
- CC2 must be in **LAN-only mode** (see warning above)
- Default credentials: `elegoo` / `123456` — if you've set an access code, use that as `CC2_PASS`

### 2. Verify CC2 Network Connectivity

```bash
ping <CC2_IP>
```

If unreachable, check:
- Printer is powered on
- Connected to 2.4 GHz WiFi (CC2 may not support 5 GHz)
- Firewall allows MQTT (port 1883)

### 3. Start CC2 Backend

```bash
docker compose --profile cc2 up -d cc2_backend
```

### 4. Verify Connection

```bash
docker logs cc2_backend
```

Look for: `Connected to CC2 MQTT broker` and `Registration successful`.

### 5. Verify Home Assistant Autodiscovery

In Home Assistant:
- Go to **Settings** → **Devices & Services** → **MQTT**
- Look for **Centauri Carbon 2** device
- Verify these sensors appeared:
  - `cc2_nozzle_temp` (temperature)
  - `cc2_nozzle_target` (temperature)
  - `cc2_bed_temp` (temperature)
  - `cc2_bed_target` (temperature)
  - `cc2_chamber_temp` (temperature)
  - `cc2_print_status` (text)
  - `cc2_print_progress` (%)

---

## Home Assistant Sensors Created by CC2 Backend

| Entity ID | Description | Unit | Device Class |
|-----------|-------------|------|----------------|
| `sensor.centauri_carbon_2_nozzle_temperature` | Nozzle temp | °C | temperature |
| `sensor.centauri_carbon_2_nozzle_target_temperature` | Nozzle setpoint | °C | – |
| `sensor.centauri_carbon_2_bed_temperature` | Bed temp | °C | temperature |
| `sensor.centauri_carbon_2_bed_target_temperature` | Bed setpoint | °C | – |
| `sensor.centauri_carbon_2_chamber_temperature` | Chamber temp | °C | temperature |
| `sensor.centauri_carbon_2_print_status` | Print state | – | – |
| `sensor.centauri_carbon_2_print_progress` | Progress | % | – |

---

## Running Both Services

Start both Panda and CC2 backends together:

```bash
docker compose --profile panda --profile cc2 up -d
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

### CC2 MQTT connection fails

**Error:** `Connection refused` or `timed out`

- Verify CC2 IP with `ping <CC2_IP>`
- Check CC2 is on 2.4 GHz WiFi (SSH to verify: `iwconfig`)
- Verify MQTT is enabled in CC2 settings (default: enabled)
- Try resetting credentials in printer settings to `elegoo` / `123456`

### Panda WebSocket fails

**Error:** `Connection refused` or `ws: unexpected close`

- Verify Panda Touch registered in its UI with correct `PANDA_HOST_IP`
- Check firewall allows inbound port 8883
- Verify the `panda_certs` Docker volume exists and the container can copy `/app/certs/cert.pem` and `/app/certs/key.pem`

### HA autodiscovery doesn't create sensors

**Error:** Sensors not appearing in HA MQTT

- Verify `HA_MQTT_BROKER` and credentials in `.env`
- Check HA Mosquitto add-on is running: **Settings** → **Add-ons** → **Mosquitto broker**
- Restart CC2 backend: `docker compose --profile cc2 restart cc2_backend`
- Check Home Assistant MQTT integration is enabled: **Settings** → **Devices & Services** → **MQTT** → **Configure**

### No data arriving at HA

**Error:** Sensors exist but show "unavailable"

- Check printer is on and printing/heating (idle printers may not report all fields)
- Verify `/cc2/status` topic is `online`: in HA MQTT integration, test publish to `cc2/status` with value `online`
- Check logs: `docker logs cc2_backend`

### TLS certificate errors (Panda)

**Error:** `SSL: CERTIFICATE_VERIFY_FAILED`

- Panda Touch may not verify self-signed certs — this is normal
- Verify the `panda_certs` Docker volume exists and the container can copy `/app/certs/cert.pem` and `/app/certs/key.pem`

---

## Further Help

- Check container logs: `docker compose logs -f [service_name]`
- Inspect running env: `docker compose exec [service] env | grep -E "CC2_|HA_MQTT"`
- Test MQTT connectivity: `mqtt_sub -h <HA_IP> -u user -P pass -t "cc2/#"`
