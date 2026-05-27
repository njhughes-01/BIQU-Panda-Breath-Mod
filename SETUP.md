# Panda Breath Mod Setup Guide

This guide covers setup for both the Panda and Elegoo CC2 backends in Docker.

## Prerequisites

- **Docker** with **buildx** and **Docker Compose** installed (buildx is included with Docker Desktop and Docker Engine 23+)
- **Home Assistant** running with **Mosquitto MQTT add-on** enabled
- **Panda Touch** printer (for Panda backend) or **Elegoo Centauri Carbon 2** (for CC2 backend)
- Network connectivity between your Docker host and printers/HA instance

## Quick Start

```bash
git clone https://github.com/njhughes-01/BIQU-Panda-Breath-Mod.git
cd BIQU-Panda-Breath-Mod

# Fill in your Panda printer IP, SN, access code, HA URL and token
nano panda_config.json

# Generate TLS certs
mkdir -p certs && bash cert_gen.sh && mv cert.pem key.pem certs/

docker compose --profile panda up -d
```

## Also have an Elegoo Centauri Carbon 2?

Also run the CC2 backend to publish its sensor data to Home Assistant.

> **⚠️ LAN-only mode required**
> On the printer: **Settings → Network → LAN Only Mode → Enable**
> Without this, the CC2's MQTT broker is not reachable.

```bash
cp .env.example .env
# Fill in CC2_IP, CC2_SN, and HA MQTT credentials
nano .env

docker compose --profile panda --profile cc2 up -d
```

---

See individual setup sections below for full configuration details.

---

## Panda Backend Setup

The Panda backend uses **Panda.py** to communicate with your BIQU Panda printer.

### 1. Edit panda_config.json

Replace placeholder values in `panda_config.json`:

| Field | Description | Example |
|-------|-------------|---------|
| `PANDA_IP` | Printer IP (WebSocket at ws://{IP}/ws) | `<PANDA_IP>` |
| `PRINTER_SN` | Panda printer serial number | `01P00A123456789` |
| `ACCESS_CODE` | Printer access code (6-8 chars) | `01P00A12` |
| `HA_BASE_URL` | Home Assistant URL | `http://<HA_IP>:8123` |
| `HA_TOKEN` | Long-lived access token from HA | (generate in HA settings) |
| `MQTT_BROKER` | HA MQTT broker IP | `<HA_IP>` |
| `MQTT_USER` / `MQTT_PASS` | HA MQTT credentials | (from Mosquitto add-on) |
| `MQTT_TOPIC_PREFIX` | Topic base prefix | `panda_breath_mod` |
| `HOST_IP` | This PC's IP (register in Panda UI) | `192.168.x.xxx` |

### 2. Generate TLS Certificates

The Panda backend runs a TLS server on port 8883. Generate certificates:

```bash
mkdir -p certs
cd certs

# Generate private key
openssl genrsa -out key.pem 2048

# Generate self-signed certificate (valid 365 days)
openssl req -new -x509 -key key.pem -out cert.pem -days 365 \
  -subj "/C=US/ST=State/L=City/O=Org/CN=localhost"

cd ..
```

### 3. Register in Panda UI

1. Open Panda Touch UI
2. Go to **Settings** → **Network** → **Access Code**
3. In the IP field, enter `HOST_IP` from `panda_config.json`
4. Connect — Panda will establish WebSocket to your host

### 4. Verify Connection

```bash
docker logs panda_backend
```

Look for: `Connected to Panda` or similar success messages.

---

## CC2 Backend Setup

The CC2 backend uses **cc2_connector.py** to communicate with your Elegoo Centauri Carbon 2 printer.

> **⚠️ LAN-only mode required**
> The CC2's MQTT broker is only accessible when the printer is in LAN-only mode.
> On the printer: **Settings → Network → LAN Only Mode → Enable**
> Without this, the MQTT port will not be reachable and the connector will fail to connect.

### 1. Configure .env

Set these values in `.env`:

```env
CC2_IP=<CC2_IP>
CC2_USER=elegoo
CC2_PASS=123456
CC2_SN=<YOUR_SN>
CC2_TOPIC_PREFIX=cc2
HA_MQTT_BROKER=<HA_IP>
HA_MQTT_PORT=1883
HA_MQTT_USER=your_mqtt_username
HA_MQTT_PASS=your_mqtt_password
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
docker compose --profile cc2 up -d
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

- Verify Panda Touch registered in its UI with correct `HOST_IP`
- Check firewall allows inbound port 8883
- Verify TLS certificates in `./certs/` exist

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
- Verify `./certs/cert.pem` and `./certs/key.pem` exist and are readable by Docker

---

## Further Help

- Check container logs: `docker compose logs -f [service_name]`
- Inspect running env: `docker compose exec [service] env | grep -E "CC2_|HA_MQTT"`
- Test MQTT connectivity: `mqtt_sub -h <HA_IP> -u user -P pass -t "cc2/#"`
