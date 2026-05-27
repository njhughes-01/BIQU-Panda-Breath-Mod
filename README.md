<!-- REPO_VIEWS_BADGE_START -->
![Repository Views](https://img.shields.io/badge/Repository%20Views-7616-blue?style=flat-square)
<!-- REPO_VIEWS_BADGE_END -->

# BIQU Panda Breath Mod 🚀
### Panda Logic Sync v2.0.0

Intelligent control system for the **BIQU Panda Breath** chamber heater.

This project simulates a **Bambu Lab printer** on a host system (PC / server) and enables fully synchronized chamber heating using **real-time Home Assistant data**.

<img width="1839" height="912" src="https://github.com/user-attachments/assets/50aab4bf-ccf9-4eea-8567-7ef40c84fd36" />

<img width="1329" height="897" src="https://github.com/user-attachments/assets/89309991-4a4c-4611-bd0c-f5e7c97c90c9" />
<img width="1329" height="897" src="https://github.com/user-attachments/assets/e15c096e-c885-4e7c-b7a1-fb6e755795f8" />

<img width="526" height="780" src="https://github.com/user-attachments/assets/cb2ca112-8e07-41d9-a7cc-d286a7d684fa" />

<img width="1261" height="953" alt="gui-1" src="https://github.com/user-attachments/assets/90471014-9753-4d1b-a004-66d0f439cf7d" />
<img width="1261" height="953" alt="gui-2" src="https://github.com/user-attachments/assets/1188fb61-830d-4218-b6a7-e62999cfdc75" />
<img width="1261" height="958" alt="gui-3" src="https://github.com/user-attachments/assets/73b04da5-b450-473a-af27-99695b913f9a" />


---

# ✨ Key Features (v2.0.0)

- 🔥 Immediate heating in all modes (no bed wait)
- 🔐 Global lock / unlock safety system
- ⚡ Stable power sync (no UI bounce or reset)
- 🧠 Slicer Priority Mode (M191 / M141 detection via Moonraker)
- 🔄 Full bidirectional MQTT sync (Home Assistant auto-discovery)
- 🎛 Dry mode support
- 📊 Live terminal monitor (flicker-free)
- 🔒 TLS secure connection (Port 8883)

---

# ➕ Firmware v1.0.3 Support

## Direct Klipper Binding
Firmware v1.0.3 allows direct connection to Klipper or a remote backend.

## Remote Backend (Raspberry Pi)
`Panda.py` can run on a separate system while the GUI connects via MQTT.

## Remote GUI Mode
- No local backend required
- GUI acts as control + monitoring interface only
- Start/Stop disabled in this mode

## MQTT Improvements
New topics:
- `panda_breath_mod/bed` → Bed temperature
- `panda_breath_mod/heizung` → Heater state (ON/OFF)

## Instant Mode Switching
- Immediate updates when switching Auto / Manual / Dry
- No stuck or outdated states

---

# 🔗 Binding (v1.0.3)

When using **Klipper + Panda Backend**:

👉 `Printer IP` = system running `Panda.py`

Example:

Printer IP → 192.168.8.8


⚠ Important:
- Panda connects to backend (not directly to Klipper)
- Backend handles MQTT, logic, Moonraker

---

# 🛠 How It Works

The script emulates a **Bambu-compatible printer** using Panda WebSocket protocol.

**Data flow:**

Moonraker → Home Assistant → Panda Logic Sync → Panda Touch


---

# 🧠 Heating Logic

## Immediate Heating (All Modes)

Heating starts instantly when:

Chamber Temp < Target - Hysteresis


- No bed wait
- No start delay

## Bed Temperature Role

Used only for:
- Safety limit
- Filter fan activation

If exceeded:

Bed Limit reached


(Heating still continues)

---

# 🔐 Lock System

**Button:** Heater Stop

Activates global lock:
- work_on = 0
- work_mode = 0
- set_temp = 0
- MQTT ignored

Unlock only via **Unlock button**

---

# ⚡ Power System

Switch:

switch.panda_breath_mod_panda_power


Fixes:
- No UI bounce
- No feedback loops
- Stable sync

---

# 🧩 Slicer Integration (OrcaSlicer)

Supports:

M191 Sxx
M141 Sxx


When **Slicer Priority Mode = ON**:
- Automatically sets chamber target

---

# 🐳 Docker Deployment (Recommended)

No Python environment setup required on the host. A pre-built multi-arch image (`linux/amd64`, `linux/arm64`) is published to GitHub Container Registry.

## Quick start

**Standard deployment** (Panda Breath only):

```bash
curl -O https://raw.githubusercontent.com/njhughes-01/BIQU-Panda-Breath-Mod/cc2-docker-integration/docker-compose.yml
curl -O https://raw.githubusercontent.com/njhughes-01/BIQU-Panda-Breath-Mod/cc2-docker-integration/.env.example
cp .env.example .env
nano .env                       # set PANDA_IP, PANDA_SN, PANDA_ACCESS_CODE, HA_TOKEN

docker compose up -d
```

Docker has stable defaults for internal service addresses: `HA_MQTT_BROKER=mosquitto` and `HA_BASE_URL=http://homeassistant:8123`. Override them in `.env` or Portainer stack environment variables if Home Assistant or Mosquitto are outside this stack. TLS certificates are generated automatically on first run.

To pin to a specific release instead of `:latest`, edit the `image:` line in your compose file:

```yaml
image: ghcr.io/njhughes-01/biqu-panda-breath-mod:2.0.0
```

Available tags: [ghcr.io/njhughes-01/biqu-panda-breath-mod](https://github.com/njhughes-01/BIQU-Panda-Breath-Mod/pkgs/container/biqu-panda-breath-mod) — see [CHANGELOG.md](CHANGELOG.md) for what's in each version.

The Docker stack also starts a browser-based Panda control surface:

```text
http://<DOCKER_HOST>:8088
```

This web UI controls the backend through the same MQTT topics as Home Assistant. It is not the upstream desktop PySide window; the upstream repo documents `PandaGui.py`, but the checked-in source is backend logic, not a runnable Qt app.

Set the Panda/Bambu binding values as environment variables, either in Portainer or in a local `.env`:

```env
PANDA_IP=YOUR_PANDA_IP
PANDA_SN=YOUR_PANDA_SERIAL
PANDA_ACCESS_CODE=YOUR_ACCESS_CODE
```

When binding from the Panda UI, do not scan. Use Klipper/direct binding and set `Printer IP` to the Docker host IP (`PANDA_HOST_IP`, or the auto-detected value shown in the container logs). The backend sends `PANDA_SN` and `PANDA_ACCESS_CODE` to the Panda over WebSocket.

The browser UI is exposed by the `panda_web` service on `PANDA_WEB_PORT` (`8088` by default). Control and monitoring are also available through Home Assistant MQTT entities.

## Also have an Elegoo Centauri Carbon 2?

If your printer is an Elegoo Centauri Carbon 2, also run the CC2 backend to feed its sensor data into Home Assistant for use in the Panda Breath automation.

> **LAN-only mode required** — on the CC2: Settings → Network → LAN Only Mode → Enable

Provide `CC2_IP` and `CC2_SN` as stack environment variables or in `.env`. Also fetch the CC2 override file, then start with both compose files:

```bash
curl -O https://raw.githubusercontent.com/njhughes-01/BIQU-Panda-Breath-Mod/cc2-docker-integration/docker-compose.cc2.yml

docker compose -f docker-compose.yml -f docker-compose.cc2.yml up -d
```

CC2 defaults are already set for `CC2_USER=elegoo`, `CC2_PASS=123456`, and `CC2_TOPIC_PREFIX=cc2`; normally only `CC2_IP` and `CC2_SN` need to be supplied.

This publishes 7 sensors to Home Assistant via autodiscovery:

| Sensor | Unit |
|--------|------|
| Nozzle Temperature | °C |
| Nozzle Target | °C |
| Bed Temperature | °C |
| Bed Target | °C |
| Chamber Temperature | °C |
| Print Status | — |
| Print Progress | % |

See **[SETUP.md](SETUP.md)** for full configuration details, Panda Touch binding, and Home Assistant verification steps.

---

# 📦 Manual Installation

## 1. Clone
```bash
git clone https://github.com/jeng37/BIQU-Panda-Breath-Mod.git
cd BIQU-Panda-Breath-Mod
2. Install Dependencies
sudo apt update
sudo apt install python3-pip -y
pip install asyncio websockets requests paho-mqtt
3. Generate SSL Certificates
chmod +x cert_gen.sh
./cert_gen.sh

Or manually:

openssl req -x509 -newkey rsa:4096 \
-keyout key.pem \
-out cert.pem \
-sha256 -days 3650 -nodes \
-subj "/C=DE/ST=Panda/L=Panda/O=Bambu/OU=Printer/CN=bambulab.local"
⚙ Configuration

Edit:

nano Panda.py

Configure:

MQTT (Broker, User, Password)
Panda (IP, SN, Access Code)
Home Assistant (Token, Sensor URL)
▶ Start
sudo python3 Panda.py
🔗 Binding

Open:

http://<PANDA_IP>

Enter:

Printer SN
Access Code
Printer IP → HOST_IP

⚠ Do NOT use Scan

📊 Live Monitor Example
READY | Bed:61° | Chamber:50/43° | Heat:ON | Fan:ON
🏠 Home Assistant Entities
Numbers
Chamber Target
Bed Limit
Filter Temp
Dry Temp
Dry Time
Switches
Panda Power
Slicer Priority Mode
Buttons
Auto
Manual
Dry
Heater Stop
Unlock
Sensors
Chamber Current
Slicer Target
Status
Mode
Lock State
Version
🛡 Safety Behavior
Situation	Result
HA sensor failure	Heating OFF
Lock active	Everything OFF
Work mode 0	Standby
Panda Power OFF	Shutdown
🖥 Panda Control GUI

Desktop GUI for controlling the Panda Breath Mod via MQTT.

Features
Start/Stop backend script
Live monitoring (temps, status, power, lock)
MQTT control (Auto, Manual, Dry, Power, Unlock)
Set values (temps, limits, timers)
Live log output
MQTT connection status display
Requirements
Python 3.10+
PySide6
paho-mqtt
Linux with pkexec

Install:

pip install PySide6 paho-mqtt
Start GUI
python3 PandaGui.py

Default script:

~/Panda/Panda.py
📡 MQTT Configuration
Broker: 192.168.x.xxx
Port: 1883
Topic prefix:
panda_breath_mod

Subscribes to:

panda_breath_mod/#
⚠ Current State

Still partially dependent on Home Assistant.

Planned Improvements
Remove Home Assistant dependency
Full MQTT-only system
Config file instead of hardcoded values
Better error handling
Cleaner architecture
Optional standalone Linux app
📝 License

MIT License

⚠ Disclaimer

Use at your own risk.
Always follow fire safety regulations when operating heated 3D printer enclosures.
