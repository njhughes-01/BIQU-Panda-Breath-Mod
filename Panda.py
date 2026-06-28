#!/usr/bin/env python3
import asyncio, ssl, json, time, requests, websockets, os, socket, threading
import logging
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
# ============================================================
# ✅ FIX / ERWEITERUNG: SLICER MODE + FEHLENDE ENTITÄTEN (HA)
# ------------------------------------------------------------
# - Implementiert "Slicer Priority Mode" (Switch)
# - Implementiert "Heat Stop" (Button)
# - Implementiert Slicer-Auto-Erkennung (G-Code Analyse via Moonraker)
# - Fügt fehlende MQTT Discovery Entities hinzu (damit "Entity not found" weg ist)
# - Entfernt NICHTS: Original bleibt, Erweiterungen sind additiv/ersetzend innerhalb
#   der bestehenden Struktur (nur ergänzt/erweitert).
# ============================================================
PANDA_VERSION = "v2.0.5"
RELAY_ON = 85.0   # relay-on sentinel (sent to Panda device as set_temp when heating)
RELAY_OFF = 20.0  # relay-off sentinel
_discovery_published = False  # publish autodiscovery once; retain keeps it on broker
last_reported_mode = None
mode_change_hint = ""
heating_locked = False
global_lock = False  # NEU: Sicherheits-Sperre für alle Modi
global_heating_state = RELAY_OFF
last_switch_time = 0
last_stop_command_time = 0
last_live_log_state = None
last_live_log_time = 0
_last_heat_status = ""
_last_heat_log_time = 0.0
bed_sensor_error = False
bind_confirmed = False
_ws_bind_time = 0.0  # timestamp of last successful bind; heating suppressed for 5s after
bind_warning_shown = False
# --- POWER CONFIRM (gegen ON->OFF "Bounce") ---
desired_power_state = None           # None / True / False
power_pending_until = 0.0
POWER_CONFIRM_TIMEOUT = 6.0          # Sekunden warten, bis WS "work_on" nachzieht
# ==========================================
# KONFIGURATION - JSON (panda_config.json)
# ==========================================
CONFIG_PATH = BASE_DIR / "panda_config.json"

def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("true", "1", "yes", "on")

def _load_config():
    config = {
        "DEBUG": _env_bool("PANDA_DEBUG", False),
        "DEBUG_TO_FILE": _env_bool("PANDA_DEBUG_TO_FILE", True),
        "HYSTERESE": float(os.environ.get("HYSTERESE", "1.5")),
        "MIN_SWITCH_TIME": float(os.environ.get("MIN_SWITCH_TIME", "10")),
        "MQTT_BROKER": os.environ.get("HA_MQTT_BROKER", "mosquitto"),
        "MQTT_PORT": int(os.environ.get("HA_MQTT_PORT", "1883")),
        "MQTT_USER": os.environ.get("HA_MQTT_USER", ""),
        "MQTT_PASS": os.environ.get("HA_MQTT_PASS", ""),
        "MQTT_TOPIC_PREFIX": os.environ.get("PANDA_MQTT_TOPIC_PREFIX", "panda_breath_mod"),
        "HOST_IP": os.environ.get("PANDA_HOST_IP", ""),
        "PANDA_IP": os.environ.get("PANDA_IP", ""),
        "PRINTER_SN": os.environ.get("PANDA_SN", ""),
        "ACCESS_CODE": os.environ.get("PANDA_ACCESS_CODE", ""),
        "HA_BED_TEMPERATURE_ENTITY": os.environ.get("HA_BED_TEMPERATURE_ENTITY", ""),
        "HA_BASE_URL": os.environ.get("HA_BASE_URL", "http://homeassistant:8123"),
        "HA_URL": os.environ.get("HA_URL", ""),
        "HA_TOKEN": os.environ.get("HA_TOKEN", ""),
        "PRINTER_IP": os.environ.get("PRINTER_IP", os.environ.get("PANDA_IP", "")),
        "CC2_IP": os.environ.get("CC2_IP", ""),
        "CC2_SN": os.environ.get("CC2_SN", ""),
        "CC2_USER": os.environ.get("CC2_USER", "elegoo"),
        "CC2_PASS": os.environ.get("CC2_PASS", "123456"),
        "CC2_TOPIC_PREFIX": os.environ.get("CC2_TOPIC_PREFIX", "cc2"),
        "CC2_HA_MODE": os.environ.get("CC2_HA_MODE", ""),
        "CC2_HA_FALLBACK_TEMP": os.environ.get("CC2_HA_FALLBACK_TEMP", "0"),
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config.update(json.load(f))
    return config

CONFIG = _load_config()

# Konsolen-Ausgabe: True zeigt detaillierte MQTT-Befehle im Terminal, False hält es sauber.
DEBUG = CONFIG["DEBUG"]
# Logging: True speichert alle Ereignisse (Verbindungen, Fehler, Sync) in 'panda_debug.log'.
DEBUG_TO_FILE = CONFIG["DEBUG_TO_FILE"]
# Schaltschwelle: Temperatur muss um diesen Wert unter 'Soll' fallen, bevor wieder geheizt wird.
HYSTERESE = CONFIG["HYSTERESE"]
# Schutzzeit: Mindestpause (in Sek.) zwischen zwei Schaltvorgängen, um die Hardware zu schonen.
MIN_SWITCH_TIME = CONFIG["MIN_SWITCH_TIME"]
# How many °C above the filament target to set the Panda Breath during preheat.
# Overshooting forces faster heat-soak into the CC2 chamber body.
# Once the CC2 sensor is within CC2_RESUME_OFFSET of target, PB is dropped back to real target.
# Default 15 = e.g. ASA 55°C → Panda Breath heats to 70°C during warmup.
# Note: Panda Breath hardware safety caps at 60°C — preheat_temp is clamped there.
PREHEAT_OVERSHOOT_DELTA = float(CONFIG.get("PREHEAT_OVERSHOOT_DELTA",
                                            os.environ.get("PREHEAT_OVERSHOOT_DELTA", "15")))
PANDA_MAX_TEMP = 60.0  # hardware safety limit — device refuses setpoints above this
# Dynamic overshoot: while CC2 is more than CC2_OVERSHOOT_MAX_DELTA below target, PB runs at
# PANDA_MAX_TEMP to push extra heat into the chamber.  Once CC2 climbs to within
# CC2_OVERSHOOT_MIN_DELTA of target the setpoint drops back to target (hysteresis prevents thrash).
CC2_OVERSHOOT_MAX_DELTA = float(CONFIG.get("CC2_OVERSHOOT_MAX_DELTA",
                                            os.environ.get("CC2_OVERSHOOT_MAX_DELTA", "15")))
CC2_OVERSHOOT_MIN_DELTA = float(CONFIG.get("CC2_OVERSHOOT_MIN_DELTA",
                                            os.environ.get("CC2_OVERSHOOT_MIN_DELTA", "7")))
# How many °C below target the CC2 chamber sensor must reach before resuming the paused print.
# Default 5 = resume when CC2 sensor ≥ target - 5 (e.g. ≥ 50°C for ASA 55°C target).
# CC2 box_temp is the authoritative chamber reading; PB sensor is near the heater and runs hotter.
CC2_RESUME_OFFSET = float(CONFIG.get("CC2_RESUME_OFFSET",
                                      os.environ.get("CC2_RESUME_OFFSET", "5")))
# MQTT Broker Adresse: Die IP-Adresse deines Home Assistant oder MQTT-Servers.
MQTT_BROKER = CONFIG["MQTT_BROKER"]
# MQTT Benutzername: In HA unter Einstellungen -> Personen -> Benutzer angelegt.
MQTT_USER = CONFIG["MQTT_USER"]
# MQTT Passwort: Das zugehörige Passwort für den MQTT-Benutzer.
MQTT_PASS = CONFIG["MQTT_PASS"]

# MQTT Präfix: Die Basis für alle Topics (z.B. panda_breath_mod/soll).
# ⚠️ WICHTIG: Deine Screenshots zeigen entity_ids wie:
# - button.panda_heat_stop
# - switch.panda_breath_mod_slicer_priority_mode
# - sensor.panda_breath_mod_slicer_target_temp
# Darum MUSS der Prefix "panda_breath_mod" sein, sonst passt HA/YAML nicht.
MQTT_TOPIC_PREFIX = CONFIG["MQTT_TOPIC_PREFIX"]

# Host IP: Die statische IP-Adresse des Rechners, auf dem dieses Skript läuft.
HOST_IP = CONFIG["HOST_IP"]
# Panda IP: Die IP-Adresse deines Panda Touch Displays im WLAN.
PANDA_IP = CONFIG["PANDA_IP"]
# Seriennummer: Die SN deines Druckers (findest du in der Panda-UI oder auf dem Sticker).
PRINTER_SN = CONFIG["PRINTER_SN"]
# Access Code: Der Sicherheitscode deines Druckers für die WebSocket-Verbindung.
ACCESS_CODE = CONFIG["ACCESS_CODE"]
# HA Bett-Temperatur Entität: Hier nur die Entity-ID ändern, falls dein Sensor anders heißt.
HA_BED_TEMPERATURE_ENTITY = CONFIG["HA_BED_TEMPERATURE_ENTITY"]
# HA API URL: Link zum Bett-Temperatur-Sensor deines Druckers in Home Assistant.
HA_URL = CONFIG["HA_URL"] if CONFIG.get("HA_URL") else f'{CONFIG["HA_BASE_URL"]}/api/states/{HA_BED_TEMPERATURE_ENTITY}'
# HA Token: Ein 'Long-Lived Access Token' (erstellt im HA-Profil ganz unten).
HA_TOKEN = CONFIG["HA_TOKEN"]

# ============================================================
# ✅ SLICER MODE (NEU)
# ------------------------------------------------------------
# PRINTER_IP = IP vom Drucker / Moonraker (für Gcode-File Analyse)
# Funktion: liest beim Druckstart die ersten Bytes der Gcode Datei,
# sucht M191 Sxx / M141 Sxx und setzt slicer_soll.
# ============================================================
PRINTER_IP = CONFIG["PRINTER_IP"]
CC2_IP = CONFIG.get("CC2_IP", os.environ.get("CC2_IP", ""))
CC2_SN = CONFIG.get("CC2_SN", os.environ.get("CC2_SN", ""))
CC2_USER = CONFIG.get("CC2_USER", os.environ.get("CC2_USER", "elegoo"))
CC2_PASS = CONFIG.get("CC2_PASS", os.environ.get("CC2_PASS", "123456"))
CC2_TOPIC_PREFIX = CONFIG.get("CC2_TOPIC_PREFIX", os.environ.get("CC2_TOPIC_PREFIX", "cc2"))
CC2_HA_MODE = CONFIG.get("CC2_HA_MODE", os.environ.get("CC2_HA_MODE", "")).lower() in ("true", "1", "yes")
CC2_ACTIVE = bool(CC2_IP) or CC2_HA_MODE
# Fallback chamber target when filament type is unknown at print start (0 = disabled).
# Set CC2_HA_FALLBACK_TEMP=45 to pause and heat to 45°C for any unknown filament.
CC2_HA_FALLBACK_TEMP = float(CONFIG.get("CC2_HA_FALLBACK_TEMP", os.environ.get("CC2_HA_FALLBACK_TEMP", "0")))
HA_BASE_URL = CONFIG.get("HA_BASE_URL", os.environ.get("HA_BASE_URL", ""))
_ha_cc2_entities: dict = {}
MQTT_PORT = CONFIG.get("MQTT_PORT", int(os.environ.get("HA_MQTT_PORT", 1883)))

# Filament type → chamber target (°C). Override via CC2_FILAMENT_MAP env var (JSON).
_DEFAULT_FILAMENT_MAP = {
    "PLA":    0,   # no chamber heat — heat creep risk
    "PLA+":   0,
    "PLA-CF": 0,
    "PLA-GF": 0,
    "PET":    35,
    "PET-CF": 35,
    "PETG":   35,  # light warmth improves adhesion
    "PETG-CF":35,
    "PETG-GF":35,
    "ABS":    55,  # warp-prone; needs consistent heat
    "ABS-CF": 55,
    "ABS-GF": 55,
    "ASA":    55,  # same family as ABS
    "ASA-CF": 55,  # carbon-fiber ASA still wants ASA-class chamber heat
    "ASA-GF": 55,
    "NYLON":  65,
    "PA":     65,  # nylon; hygroscopic, needs hot chamber
    "PA6":    65,
    "PA12":   65,
    "PA-CF":  70,  # CF variant runs slightly hotter
    "PA-GF":  70,
    "PA6-CF": 70,
    "PA6-GF": 70,
    "PA12-CF":70,
    "PA12-GF":70,
    "PAHT-CF":70,
    "PC":     70,  # polycarbonate; needs aggressive heat
    "PC-ABS": 65,
    "PC-CF":  70,
    "PC-FR":  70,
    "TPU":    0,   # flexible; no chamber heat needed
    "TPE":    0,
}
try:
    _raw = os.environ.get("CC2_FILAMENT_MAP", "")
    FILAMENT_CHAMBER_MAP = {**_DEFAULT_FILAMENT_MAP, **json.loads(_raw)} if _raw else _DEFAULT_FILAMENT_MAP
except Exception:
    FILAMENT_CHAMBER_MAP = _DEFAULT_FILAMENT_MAP

def _resolve_filament_chamber_target(filament_type: str):
    """Return (target, matched_key) for exact or fuzzy filament names."""
    if not filament_type:
        return None, None
    exact = FILAMENT_CHAMBER_MAP.get(filament_type.upper(), FILAMENT_CHAMBER_MAP.get(filament_type))
    if exact is not None:
        return exact, filament_type.upper()
    upper = filament_type.upper()
    match = max((k for k in FILAMENT_CHAMBER_MAP if k in upper), key=len, default=None)
    if match is None:
        return None, None
    return FILAMENT_CHAMBER_MAP[match], match
# ==========================================
# current_data nutzt jetzt die exakten Namen aus der Hardware (filament_temp/timer)
current_data = {
    "chamber_setpoint": 0.0,
    "chamber_temp": 0.0,
    "bed_limit": 50.0,
    "filtertemp": 30.0,
    "bed_temp": 0.0,
    "filament_temp": 45,
    "filament_timer": 3,

    # ========================================================
    # ✅ SLICER MODE STATE (NEU)
    # --------------------------------------------------------
    # slicer_priority_mode:
    #    - True  => Slicer-Wert hat Vorrang (bei erkanntem M191/M141)
    #    - False => HA / Panda Setting (soll) hat Vorrang
    #
    # slicer_soll:
    #    - letzter erkannter Wert aus dem Gcode (nur Anzeige)
    #
    # last_analyzed_file:
    #    - damit wir pro Datei nur einmal analysieren
    # ========================================================
    "slicer_priority_mode": True,
    "slicer_soll": 0.0,
    "last_analyzed_file": ""
}

ha_memory = {"chamber_setpoint": 30.0, "bed_limit": 50.0}
last_ha_change = 0
panda_ws = None
panda_writer = None  # asyncio StreamWriter from v1.0.3 TLS path
main_loop = None
cc2_paused_for_preheat = False  # True when we paused the CC2 to wait for chamber temp
_preheat_overshoot_temp = 0    # set_temp sent to Panda Breath during preheat (target + overshoot delta)
_cc2_dynamic_overshoot_active = False  # True when CC2 far cold mid-print — boost PB to PANDA_MAX_TEMP

PREHEAT_STATE_FILE = BASE_DIR / "preheat_state.json"
PREHEAT_STATE_MAX_AGE = 43200  # 12h — discard persisted state older than this

def _save_preheat_state():
    try:
        with open(PREHEAT_STATE_FILE, "w") as _f:
            json.dump({
                "cc2_paused_for_preheat": cc2_paused_for_preheat,
                "preheat_overshoot_temp": _preheat_overshoot_temp,
                "chamber_setpoint": current_data.get("chamber_setpoint", 0),
                "written_at": time.time(),
            }, _f)
    except Exception as _e:
        log_event(f"[PREHEAT-STATE] Save failed: {_e}")

def _load_preheat_state():
    global cc2_paused_for_preheat, _preheat_overshoot_temp
    try:
        if not PREHEAT_STATE_FILE.exists():
            return
        with open(PREHEAT_STATE_FILE) as _f:
            _s = json.load(_f)
        if time.time() - _s.get("written_at", 0) > PREHEAT_STATE_MAX_AGE:
            log_event("[PREHEAT-STATE] Discarding stale preheat state (>12h old)")
            PREHEAT_STATE_FILE.unlink(missing_ok=True)
            return
        if _s.get("cc2_paused_for_preheat"):
            cc2_paused_for_preheat = True
            _preheat_overshoot_temp = int(_s.get("preheat_overshoot_temp", 0))
            log_event(f"[PREHEAT-STATE] Restored: paused_for_preheat=True overshoot={_preheat_overshoot_temp}°C", force_console=True)
    except Exception as _e:
        log_event(f"[PREHEAT-STATE] Load failed: {_e}")

_load_preheat_state()
terminal_cleared = False
# Merkt sich den letzten vollständigen WS-Settings-Stand
last_ws_settings = {}
power_forced_off = False # Ergänzt für Logik-Vollständigkeit

# ============================================================
# --- LOGGING SETUP (DEBUG / CRITICAL Umschaltbar) ---
# ============================================================

LOG_LEVEL = logging.DEBUG if DEBUG else logging.CRITICAL

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(levelname)s:%(name)s:%(message)s"
)

# 🔇 Externe Libraries ruhigstellen (nur wenn DEBUG=False relevant)
#logging.getLogger("urllib3").setLevel(logging.CRITICAL)
#logging.getLogger("websockets").setLevel(logging.CRITICAL)

file_logger = logging.getLogger("PandaFullLog")
file_logger.propagate = False
file_logger.setLevel(logging.INFO)

if DEBUG_TO_FILE:
    f_handler = logging.FileHandler(str(BASE_DIR / "panda_debug.log"))
    f_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(message)s")
    )
    file_logger.addHandler(f_handler)
    
# --- LOGGING FUNKTION ---
def log_event(msg, force_console=False):

    # 1️⃣ Datei Logging
    if DEBUG_TO_FILE:
        file_logger.info(msg)

    # 2️⃣ Konsole nur bei DEBUG oder Force
    if DEBUG or force_console:
        print(f" INFO:PandaDebug:{msg}")

    # 3️⃣ Remote GUI Live-Log via MQTT
    # Nur senden, wenn DEBUG aktiv ist oder ein wichtiger Force-Log kommt.
    try:
        if (DEBUG or force_console) and "mqtt_client" in globals():
            ts = time.strftime("%H:%M:%S")
            mqtt_client.publish(
                f"{MQTT_TOPIC_PREFIX}/log",
                f"[{ts}] {msg}",
                retain=False
            )
    except Exception:
        pass
            
# --- HELPER ---
def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def _chamber_temp_from_filename(filename: str) -> float:
    """Extract explicit chamber temp from filename. Convention: include NNc or NNC in name.
    Examples: asa_benchy_55C.gcode, [55c]_vase.gcode, print_chamber55.gcode → 55.0
    Returns 0.0 if no temp found or temp outside 20-85°C sanity range."""
    import re
    m = re.search(r'(?<!\d)(\d{2,3})[Cc](?!\d)', filename)
    if m:
        temp = float(m.group(1))
        if 20.0 <= temp <= 85.0:
            return temp
    return 0.0

# ✅ SLICER PARSER (OPTIMIERT: Nutzt run_in_executor gegen Blockaden)
async def slicer_auto_parser():
    loop = asyncio.get_event_loop()
    while True:
        try:
            # OPTIMIERUNG: requests in Thread auslagern, damit das Hauptskript (Heartbeat) nicht stoppt
            def fetch_moonraker():
                return requests.get(f"http://{PRINTER_IP}/printer/objects/query?print_stats", timeout=2).json()
            
            r = await loop.run_in_executor(None, fetch_moonraker)
            filename = r.get("result", {}).get("status", {}).get("print_stats", {}).get("filename", "")

            if filename and filename != current_data["last_analyzed_file"]:
                log_event(f"[SLICER] New file detected: {filename}")
                
                def fetch_gcode():
                    return requests.get(f"http://{PRINTER_IP}/server/files/gcodes/{filename}", 
                                        headers={'Range': 'bytes=0-50000'}, timeout=5)
                
                resp = await loop.run_in_executor(None, fetch_gcode)

                if resp.status_code in [200, 206]:
                    import re
                    match = re.search(r'(?:M191|M141)\s+S(\d+)', resp.text)
                    if match:
                        new_target = safe_float(match.group(1), 0.0)
                        current_data["slicer_soll"] = new_target
                        current_data["last_analyzed_file"] = filename

                        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_soll", int(new_target), retain=True)
                        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_target_temp", int(new_target), retain=True)
                        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_file", filename, retain=True)

                        if current_data["slicer_priority_mode"] and new_target > 15:
                            current_data["chamber_setpoint"] = new_target
                            if panda_ws:
                                asyncio.run_coroutine_threadsafe(
                                    panda_ws.send(json.dumps({"settings": {"set_temp": int(new_target)}})),
                                    main_loop
                                )
                            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/soll", int(new_target), retain=True)
                    else:
                        current_data["last_analyzed_file"] = filename
                        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_file", filename, retain=True)

        except Exception as e:
            if DEBUG: log_event(f"DEBUG:SLICER-ERR:{e}")
        await asyncio.sleep(5)

# --- MQTT LOGIK ---
def _handle_cc2_data(cc2_key, val):
    """Process a single CC2 key/value pair — called from MQTT handler and HA poller."""
    global cc2_paused_for_preheat, _preheat_overshoot_temp, global_heating_state
    _cc2_printing_states = {"printing", "preheating", "paused", "pausing", "resuming", "stopping"}
    if cc2_key == "bed_temp":
        current_data["bed_temp"] = safe_float(val, current_data.get("bed_temp", 0.0))
    elif cc2_key == "print_status":
        prev_status = current_data.get("cc2_print_status", "idle")
        new_status = val.lower()
        current_data["cc2_print_status"] = new_status
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/cc2_print_status", val, retain=True)
        # Turn off chamber heater when print ends
        if prev_status in _cc2_printing_states and new_status not in _cc2_printing_states:
            current_data["chamber_setpoint"] = 0.0
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/soll", 0, retain=True)
            # Clear retained active_filament_type so next print doesn't preheat-pause immediately
            mqtt_client.publish(f"{CC2_TOPIC_PREFIX}/active_filament_type", "", retain=True)
            current_data["cc2_pending_filament"] = ""
            current_data["slicer_soll"] = 0.0
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_soll", 0, retain=True)
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_target_temp", 0, retain=True)
            log_event(f"[CC2-SLICER] Print ended ({prev_status}→{new_status}), turning off chamber heater", force_console=True)
            if cc2_paused_for_preheat:
                cc2_paused_for_preheat = False
                _preheat_overshoot_temp = 0
                _save_preheat_state()
            async def _cc2_off():
                try:
                    await panda_send(json.dumps({"settings": {"isrunning": 0, "work_on": False, "set_temp": 0}}))
                except Exception as e:
                    log_event(f"[CC2-OFF-ERR] Failed to turn off heater at print end: {e}", force_console=True)
            asyncio.run_coroutine_threadsafe(_cc2_off(), main_loop)
        # Re-arm heating when print starts — handles retained active_filament_type
        # arriving before print_status on MQTT reconnect or container restart
        elif prev_status not in _cc2_printing_states and new_status in _cc2_printing_states:
            chamber_target = float(current_data.get("chamber_setpoint", 0))
            # If chamber_setpoint not set yet, apply any buffered filament type
            if chamber_target == 0:
                pending = current_data.get("cc2_pending_filament", "")
                if pending:
                    fil_target, fil_match = _resolve_filament_chamber_target(pending)
                    if fil_target is not None and fil_target > 0:
                        current_data["chamber_setpoint"] = float(fil_target)
                        current_data["slicer_soll"] = float(fil_target)
                        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/soll", int(fil_target), retain=True)
                        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_soll", int(fil_target), retain=True)
                        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_target_temp", int(fil_target), retain=True)
                        chamber_target = float(fil_target)
                        if fil_match and fil_match != pending.upper():
                            log_event(f"[CC2-SLICER] Fuzzy buffered filament match: {pending!r} → {fil_match!r} → {chamber_target:.0f}°C", force_console=True)
                        log_event(f"[CC2-SLICER] Applied buffered filament {pending} → {chamber_target:.0f}°C on print start", force_console=True)
            # Last resort: use fallback temp when filament type is completely unknown
            if chamber_target == 0 and CC2_HA_FALLBACK_TEMP > 0:
                chamber_target = CC2_HA_FALLBACK_TEMP
                current_data["chamber_setpoint"] = chamber_target
                log_event(f"[CC2-SLICER] Filament type unknown — using fallback target {chamber_target:.0f}°C (set CC2_HA_FALLBACK_TEMP=0 to disable)", force_console=True)
            elif chamber_target == 0:
                log_event(f"[CC2-SLICER] Print started but no chamber target set — filament type not resolved, no preheat. Set CC2_HA_FALLBACK_TEMP to enable safety fallback.", force_console=True)
            if chamber_target > 0 and (panda_ws or panda_writer):
                chamber_now = safe_float(current_data.get("chamber_temp", 0), 0)
                cc2_chamber_now = safe_float(current_data.get("cc2_chamber_temp", 0.0), 0.0)
                _pb_ok = chamber_now > 1.0
                _cc2_ok = cc2_chamber_now > 5.0
                # CC2 box_temp is authoritative for the chamber ambient (PB sensor is near the
                # heater element and runs hotter than the actual print environment).
                if _cc2_ok:
                    effective_chamber = cc2_chamber_now
                elif _pb_ok:
                    effective_chamber = chamber_now
                else:
                    # No sensor data yet — assume cold. A false pause is safer than printing
                    # without chamber heat (e.g. mid-print restart with 43°C chamber for ASA).
                    # Resume logic releases the pause once real temp confirms warm enough.
                    effective_chamber = 0.0
                needs_preheat = effective_chamber < (chamber_target - 5) and not cc2_paused_for_preheat
                async def _cc2_heat_on_start(t=int(chamber_target), pause=needs_preheat):
                    global cc2_paused_for_preheat, global_heating_state, _preheat_overshoot_temp
                    if cc2_paused_for_preheat:
                        # Preheat already in progress (overshoot active) — don't override
                        log_event(f"[CC2-SLICER] Preheat already active ({_preheat_overshoot_temp}°C overshoot) — skipping redundant heat command", force_console=True)
                        return
                    preheat_temp = min(t + int(PREHEAT_OVERSHOOT_DELTA), int(PANDA_MAX_TEMP)) if pause else t
                    try:
                        await panda_send(json.dumps({"settings": {"isrunning": 0}}))
                        await asyncio.sleep(0.2)
                        await panda_send(json.dumps({
                            "settings": {"work_mode": 2, "work_on": True, "set_temp": preheat_temp, "isrunning": 1}
                        }))
                        await asyncio.sleep(0.3)
                        await panda_send(json.dumps({"get_settings": 1}))
                        global_heating_state = RELAY_ON
                        if pause:
                            _preheat_overshoot_temp = preheat_temp
                    except Exception as e:
                        log_event(f"[CC2-START-HEAT-ERR] {e}", force_console=True)
                        return
                    if pause and not cc2_paused_for_preheat:
                        log_event(f"[CC2-SLICER] Chamber cold ({chamber_now:.0f}°C), pausing CC2 — heating to {preheat_temp}°C (overshoot), target {t}°C", force_console=True)
                        cc2_paused_for_preheat = True  # set before sleep to survive WS reconnect in the gap
                        _save_preheat_state()
                        await asyncio.sleep(1.5)
                        _cc2_press_button("pause")
                if main_loop:
                    asyncio.run_coroutine_threadsafe(_cc2_heat_on_start(), main_loop)
                log_event(f"[CC2-SLICER] Print started, re-arming chamber heat to {chamber_target:.0f}°C", force_console=True)
            elif chamber_target > 0:
                log_event(f"[CC2-SLICER] Print started, Panda not connected — heat queued at chamber_setpoint={chamber_target:.0f}°C, will fire on WS connect", force_console=True)
        # CC2 transitioned preheating → printing while we're still waiting for chamber warmup.
        # The pause button is ignored during preheating — re-send now that it's actually printing.
        elif cc2_paused_for_preheat and prev_status == "preheating" and new_status == "printing":
            chamber_target = float(current_data.get("chamber_setpoint", 0))
            chamber_now = safe_float(current_data.get("chamber_temp", 0), 0)
            cc2_chamber_now = safe_float(current_data.get("cc2_chamber_temp", 0.0), 0.0)
            _pb_ok = chamber_now > 1.0
            _cc2_ok = cc2_chamber_now > 5.0
            if _cc2_ok:
                _eff = cc2_chamber_now
            elif _pb_ok:
                _eff = chamber_now
            else:
                _eff = 0.0
            if chamber_target > 0 and _eff < (chamber_target - 5):
                log_event(f"[CC2-SLICER] CC2 started printing (was preheating) but chamber cold ({_eff:.0f}°C) — re-sending pause", force_console=True)
                _cc2_press_button("pause")
    elif cc2_key == "filename":
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/cc2_filename", val, retain=True)
        if val:
            current_data["last_analyzed_file"] = val
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_file", val, retain=True)
    elif cc2_key in (
        "nozzle_temp", "print_progress",
        "filament_detected", "remaining_time", "current_layer",
        "z_height", "fan_speed", "box_fan_speed", "chamber_temp",
        "led", "has_error", "speed_mode", "file_count", "latest_filename",
    ):
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/cc2_{cc2_key}", val, retain=True)
        if cc2_key == "chamber_temp":
            current_data["cc2_chamber_temp"] = safe_float(val, 0.0)
    elif cc2_key == "active_filament_type":
        # Empty val = our own print-end clear message; ignore it
        if not val.strip():
            return
        if not current_data.get("slicer_priority_mode"):
            log_event("[CC2-SLICER] Ignoring active_filament_type — slicer_priority_mode off", force_console=True)
            return
        # Deduplicate — the HA poller delivers this every 3s; only act on changes.
        # If a print has started and the target is still unset, continue so a
        # buffered fuzzy match like ASA-CF can still arm the chamber heat.
        if (
            val == current_data.get("cc2_pending_filament", "")
            and not (
                current_data.get("cc2_print_status", "idle") in _cc2_printing_states
                and float(current_data.get("chamber_setpoint", 0)) == 0
            )
        ):
            return
        # Always buffer the filament type — print_status may not have arrived yet
        # on container restart or MQTT reconnect. The print_start handler will apply it.
        current_data["cc2_pending_filament"] = val
        _cc2_active = current_data.get("cc2_print_status", "idle")
        if _cc2_active not in _cc2_printing_states:
            log_event(f"[CC2-SLICER] Buffered filament={val!r} (status={_cc2_active!r}, waiting for print start)", force_console=True)
            return
        target, match = _resolve_filament_chamber_target(val)
        if target is None:
            log_event(f"[CC2-SLICER] Unknown filament type {val!r}, no chamber target", force_console=True)
            return
        if match and match != val.upper():
            log_event(f"[CC2-SLICER] Fuzzy filament match: {val!r} → {match!r} → {target}°C", force_console=True)
        current_data["chamber_setpoint"] = float(target)
        current_data["slicer_soll"] = float(target)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/soll", int(target), retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_soll", int(target), retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_target_temp", int(target), retain=True)
        log_event(f"[CC2-SLICER] {val} → chamber {target}°C", force_console=True)
        if (panda_ws or panda_writer) and int(target) > 0:
            chamber_now = safe_float(current_data.get("chamber_temp", 0), 0)
            cc2_chamber_now = safe_float(current_data.get("cc2_chamber_temp", 0.0), 0.0)
            _pb_ok = chamber_now > 1.0
            _cc2_ok = cc2_chamber_now > 5.0
            if _cc2_ok:
                effective_chamber = cc2_chamber_now
            elif _pb_ok:
                effective_chamber = chamber_now
            else:
                # No sensor data — assume cold. Triggers preheat/pause if needed.
                effective_chamber = 0.0
            # Don't re-pause on MQTT reconnect delivering retained active_filament_type
            needs_preheat = effective_chamber < (float(target) - 5) and not cc2_paused_for_preheat
            async def _cc2_heat(t=int(target), pause=needs_preheat):
                global cc2_paused_for_preheat, global_heating_state, _preheat_overshoot_temp
                preheat_temp = min(t + int(PREHEAT_OVERSHOOT_DELTA), int(PANDA_MAX_TEMP)) if pause else t
                try:
                    await panda_send(json.dumps({"settings": {"isrunning": 0}}))
                    await asyncio.sleep(0.2)
                    await panda_send(json.dumps({
                        "settings": {
                            "work_mode": 2,
                            "work_on": True,
                            "set_temp": preheat_temp,
                            "isrunning": 1
                        }
                    }))
                    await asyncio.sleep(0.3)
                    await panda_send(json.dumps({"get_settings": 1}))
                    global_heating_state = RELAY_ON  # update immediately; GUI shows ON without waiting for WS confirm
                    if pause:
                        _preheat_overshoot_temp = preheat_temp
                except Exception as e:
                    log_event(f"[CC2-HEAT-ERR] Failed to send heat command (target={t}°C): {e}", force_console=True)
                    return
                if pause and not cc2_paused_for_preheat:
                    log_event(f"[CC2-SLICER] Chamber cold ({chamber_now:.0f}°C), pausing CC2 — heating to {preheat_temp}°C (overshoot), target {t}°C", force_console=True)
                    cc2_paused_for_preheat = True  # set before sleep to survive WS reconnect in the gap
                    _save_preheat_state()
                    await asyncio.sleep(1.5)
                    _cc2_press_button("pause")
            if main_loop:
                asyncio.run_coroutine_threadsafe(_cc2_heat(), main_loop)
            else:
                log_event(f"[CC2-SLICER] main_loop not ready — heat queued in chamber_setpoint={target}°C", force_console=True)
        elif int(target) == 0:
            current_data["chamber_setpoint"] = 0.0
            current_data["slicer_soll"] = 0.0
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/soll", 0, retain=True)
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_soll", 0, retain=True)
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_target_temp", 0, retain=True)
            async def _cc2_off_no_heat():
                try:
                    await panda_send(json.dumps({"settings": {"isrunning": 0, "work_on": False, "set_temp": 0}}))
                except Exception as e:
                    log_event(f"[CC2-OFF-ERR] {e}", force_console=True)
            if main_loop:
                asyncio.run_coroutine_threadsafe(_cc2_off_no_heat(), main_loop)
            log_event(f"[CC2-SLICER] {val} needs no chamber heat — heater off", force_console=True)
        else:
            log_event("[CC2-SLICER] Panda not connected, heating queued in chamber_setpoint", force_console=True)


def _cc2_press_button(action: str) -> None:
    """Pause or resume the CC2 — via MQTT in direct mode, HA service call in HA mode."""
    method = 1021 if action == "pause" else 1023
    if CC2_IP and CC2_SN:
        def _direct():
            client_id = f"pb{format(int(time.time() * 1000), 'x')[-8:]}"
            request_id = f"{format(int(time.time() * 1000), 'x')}{client_id}"
            client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=client_id,
                clean_session=True,
            )
            try:
                client.username_pw_set(CC2_USER, CC2_PASS)
                client.connect(CC2_IP, 1883, keepalive=30)
                client.loop_start()
                client.publish(
                    f"elegoo/{CC2_SN}/api_register",
                    json.dumps({"client_id": client_id, "request_id": request_id}),
                    qos=1,
                ).wait_for_publish(timeout=2.0)
                time.sleep(0.4)
                result = client.publish(
                    f"elegoo/{CC2_SN}/{client_id}/api_request",
                    json.dumps({"id": int(time.time() * 1000) % 1000000, "method": method}),
                    qos=1,
                )
                result.wait_for_publish(timeout=2.0)
                log_event(f"[CC2-DIRECT] {action} command sent method={method}", force_console=True)
                return True
            except Exception as e:
                log_event(f"[CC2-DIRECT] {action} command failed: {e}", force_console=True)
                return False
            finally:
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass
        threading.Thread(target=_direct, daemon=True).start()
        return

    if CC2_HA_MODE:
        key = "_pause_button" if action == "pause" else "_resume_button"
        eid = _ha_cc2_entities.get(key)
        if eid:
            def _do():
                try:
                    response = requests.post(
                        f"{HA_BASE_URL}/api/services/button/press",
                        headers={"Authorization": f"Bearer {HA_TOKEN}"},
                        json={"entity_id": eid},
                        timeout=(3, 10),
                    )
                    if response.status_code >= 400:
                        log_event(
                            f"[CC2-HA] {action} button call failed: HTTP {response.status_code} {response.text[:200]}",
                            force_console=True,
                        )
                except Exception as e:
                    log_event(f"[CC2-HA] {action} button call failed: {e}", force_console=True)
            threading.Thread(target=_do, daemon=True).start()
        else:
            log_event(f"[CC2-HA] No {action} button entity discovered — {action} skipped", force_console=True)
    else:
        topic = "pause_print" if action == "pause" else "resume_print"
        mqtt_client.publish(f"{CC2_TOPIC_PREFIX}/{topic}/press", "", qos=1)


def on_mqtt_message(client, userdata, msg):
    # ✅ FIX: Alle globalen Deklarationen MÜSSEN am Anfang der Funktion stehen
    global current_data
    global last_ha_change
    global ha_memory
    global heating_locked
    global power_forced_off
    global global_lock
    global desired_power_state
    global power_pending_until
    global cc2_paused_for_preheat
    global _preheat_overshoot_temp
    global global_heating_state
    global _discovery_published

    # HA birth message — republish autodiscovery when HA restarts
    if msg.topic == "homeassistant/status" and msg.payload.decode().strip() == "online":
        log_event("[MQTT] HA restarted — republishing autodiscovery", force_console=True)
        setup_mqtt_discovery(client)
        _discovery_published = True
        return

    # ============================================================
    # CC2 METRICS — only active when CC2_IP is configured in the environment
    # ------------------------------------------------------------
    if CC2_IP and msg.topic.startswith(f"{CC2_TOPIC_PREFIX}/"):
        cc2_key = msg.topic[len(CC2_TOPIC_PREFIX) + 1:]
        try:
            val = msg.payload.decode().strip()
            _handle_cc2_data(cc2_key, val)
        except Exception as e:
            log_event(f"[CC2-SLICER] Error processing cc2/{cc2_key}: {e}", force_console=True)
        return

    # ============================================================
    # ✅ UNLOCK LOGIK (Muss VOR dem Lock-Check kommen!)
    # ------------------------------------------------------------
    if msg.topic == f"{MQTT_TOPIC_PREFIX}/unlock/set":
        log_event(">>> SYSTEM UNLOCKED <<<", force_console=True)
        global_lock = False
        heating_locked = False
        power_forced_off = False
        if cc2_paused_for_preheat:
            cc2_paused_for_preheat = False
            _preheat_overshoot_temp = 0
            _save_preheat_state()
            _cc2_press_button("resume")
            log_event("[CC2] Resuming CC2 after unlock", force_console=True)

        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/lock_status", "UNLOCKED", retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/status", "Ready", retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/panda_modus", "Standby", retain=True)
        return

# 🛑 GLOBAL LOCK CHECK: Wenn gesperrt (Emergency Stop), wird alles andere ignoriert
    if global_lock:
        # Erlaube NUR das Unlock-Topic, alles andere wird blockiert
        if msg.topic.endswith("/set") and msg.topic != f"{MQTT_TOPIC_PREFIX}/unlock/set":
            log_event(f"[BLOCKED] System is LOCKED! Command ignored: {msg.topic}", force_console=True)
            return

    # ✅ FEHLENDE ENTITÄT 1: switch.panda_breath_mod_slicer_priority_mode
    # ============================================================
    # ✅ SLICER PRIORITY MODE
    # ============================================================
    if msg.topic == f"{MQTT_TOPIC_PREFIX}/slicer_priority_mode/set":

        payload = msg.payload.decode().strip().lower()
        is_on = payload in ("on", "1", "true")
        if CC2_ACTIVE and not is_on:
            # CC2 mode always requires slicer_priority_mode to read filament type from CC2
            log_event("[CC2] slicer_priority_mode cannot be disabled in CC2 mode — ignoring OFF command", force_console=True)
            is_on = True
        current_data["slicer_priority_mode"] = is_on

        log_event(">>> SLICER MODE ENTERED <<<", force_console=True)

        mqtt_client.publish(
            f"{MQTT_TOPIC_PREFIX}/slicer_priority_mode",
            "ON" if is_on else "OFF",
            retain=True
        )

        if is_on:
            slicer_val = float(current_data.get("slicer_soll", 0))
            # In CC2 mode slicer_soll is always 0 (Klipper M191 parser never runs).
            # Fall back to chamber_setpoint which CC2 active_filament_type already set.
            if slicer_val <= 0:
                slicer_val = float(current_data.get("chamber_setpoint", 0))

            if slicer_val > 0:
                current_data["chamber_setpoint"] = slicer_val

            if panda_ws and slicer_val > 0 and not power_forced_off:
                asyncio.run_coroutine_threadsafe(
                    panda_ws.send(json.dumps({
                        "settings": {
                            "set_temp": int(slicer_val),
                            "work_on": 1,
                            "isrunning": 1
                        }
                    })),
                    main_loop
                )

                mqtt_client.publish(
                    f"{MQTT_TOPIC_PREFIX}/soll",
                    int(slicer_val),
                    retain=True
                )

                log_event(
                    f"[SLICER] Chamber target set to {slicer_val}°",
                    force_console=True
                )
            elif slicer_val > 0:
                log_event(f"[SLICER-WARN] Panda not connected — target {slicer_val}°C queued, will send when WS reconnects", force_console=True)
            else:
                log_event("[SLICER] Mode ON — waiting for CC2 filament detection to set target", force_console=True)

        else:
            # Slicer mode OFF — restore temperature to last HA-set value
            restore = float(ha_memory.get("chamber_setpoint", 0))
            if restore > 0:
                current_data["chamber_setpoint"] = restore
                mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/soll", int(restore), retain=True)
                log_event(f"[SLICER] Mode OFF — restored chamber target to {restore:.0f}°C", force_console=True)

        return


    # ============================================================
    # ✅ HEIZUNG STOP (NOT-AUS MIT LOCK) - VERBESSERT
    # ------------------------------------------------------------
    if msg.topic == f"{MQTT_TOPIC_PREFIX}/heat_stop/set":
        log_event(">>> !!! EMERGENCY STOP & LOCK !!! <<<", force_console=True)

        global_lock = True
        heating_locked = True
        global_heating_state = RELAY_OFF  # 🔥 FIX: Heizung SOFORT logisch ausschalten
        if cc2_paused_for_preheat:
            cc2_paused_for_preheat = False
            _preheat_overshoot_temp = 0
            _save_preheat_state()
            _cc2_press_button("resume")
            log_event("[CC2] Resuming CC2 before emergency stop", force_console=True)

        async def stop_flow():
            if panda_ws:
                try:
                    await panda_ws.send(json.dumps({"settings": {"isrunning": 0, "work_on": False, "work_mode": 0, "set_temp": 0}}))
                except Exception as e:
                    log_event(f"[EMERGENCY-STOP-ERR] WS send failed: {e} — physical heater may still run!", force_console=True)
            else:
                log_event("[EMERGENCY-STOP-WARN] Panda not connected — physical heater may still run!", force_console=True)
        
        asyncio.run_coroutine_threadsafe(stop_flow(), main_loop)

        # Status an HA melden
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/lock_status", "LOCKED", retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/panda_modus", "LOCKED", retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/status", "Emergency Lock", retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/work_on", "0", retain=True) 
        return

    # --- MODE SELECT (HA dropdown → route to existing handlers) ---
    if msg.topic == f"{MQTT_TOPIC_PREFIX}/mode_select/set":
        mode_map = {"Automatic": "auto", "Manual": "manual", "Dry": "drying"}
        target = mode_map.get(msg.payload.decode().strip())
        if target:
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/{target}/set", "", retain=False)
        return

    # --- MANUELL MODUS ---
    if msg.topic.endswith("/manual/set"):
        log_event(">>> MANUAL MODE ENTERED <<<", force_console=True)
        heating_locked = False
        power_forced_off = False
        current_data["slicer_priority_mode"] = False

        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/panda_modus", "Manual", retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_priority_mode", "OFF", retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/panda_power", "ON", retain=True)

        async def flow():
            if panda_ws:
                await panda_ws.send(json.dumps({"settings": {"isrunning": 0}}))
                await asyncio.sleep(0.2)
                await panda_ws.send(json.dumps({
                    "settings": {
                        "work_mode": 2,
                        "work_on": True,
                        "set_temp": int(current_data.get("chamber_setpoint", 45)),
                        "isrunning": 1
                    }
                }))
                await asyncio.sleep(0.3)
                await panda_ws.send(json.dumps({"get_settings": 1}))

        asyncio.run_coroutine_threadsafe(flow(), main_loop)
        return

    # --- AUTO MODUS ---
    if msg.topic == f"{MQTT_TOPIC_PREFIX}/auto/set":
        log_event(">>> AUTO MODE ENTERED <<<", force_console=True)
        heating_locked = False
        power_forced_off = False
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/panda_modus", "Automatic", retain=True)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/panda_power", "ON", retain=True)
        # Do NOT clear slicer_priority_mode — CC2 slicer sets targets independently.
        # In CC2 mode the Panda Breath device can't reach the CC2's Klipper for its
        # bed-temp-based AUTO logic, so always use Manual (work_mode=2) there.
        device_mode = 2 if CC2_ACTIVE else 1

        async def flow():
            if panda_ws:
                await panda_ws.send(json.dumps({"settings": {"isrunning": 0}}))
                await asyncio.sleep(0.1)
                await panda_ws.send(json.dumps({
                    "settings": {
                        "work_mode": device_mode,
                        "work_on": True,
                        "set_temp": int(current_data.get("chamber_setpoint", 30)),
                        "isrunning": 1
                    },
                    "ui_action": "auto"
                }))
                await asyncio.sleep(0.3)
                await panda_ws.send(json.dumps({"get_settings": 1}))

        asyncio.run_coroutine_threadsafe(flow(), main_loop)
        return

    # --- DRY MODUS ---
    if msg.topic.endswith("/drying/set"):
        log_event(">>> DRYER MODE ENTERED <<<", force_console=True)

        heating_locked = False
        power_forced_off = False

        mqtt_client.publish(
            f"{MQTT_TOPIC_PREFIX}/panda_modus",
            "Dry",
            retain=True
        )
        mqtt_client.publish(
            f"{MQTT_TOPIC_PREFIX}/panda_power",
            "ON",
            retain=True
        )

        async def flow():
            if panda_ws:
                await panda_ws.send(json.dumps({
                    "settings": {
                        "work_mode": 3,
                        "work_on": True,
                        "isrunning": 1
                    }
                }))

        asyncio.run_coroutine_threadsafe(flow(), main_loop)
        return
        
    # --- START / STOP ---
    if msg.topic == f"{MQTT_TOPIC_PREFIX}/work_on/set":
        payload = msg.payload.decode().strip().lower()
        is_on = payload in ("on", "1", "true")
        async def p_flow():
            if panda_ws:
                try:
                    if not is_on:
                        await panda_ws.send(json.dumps({"settings": {"isrunning": 0, "work_on": False, "work_mode": 0}}))
                    else:
                        await panda_ws.send(json.dumps({"settings": {"work_on": 1, "isrunning": 1}}))
                except Exception as e:
                    log_event(f"[WORK-ON-ERR] WS send failed ({'ON' if is_on else 'OFF'}): {e}", force_console=True)
            else:
                log_event(f"[WORK-ON-WARN] Panda not connected — {'ON' if is_on else 'OFF'} command dropped, MQTT state updated only", force_console=True)
        asyncio.run_coroutine_threadsafe(p_flow(), main_loop)
        mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/work_on", "1" if is_on else "0", retain=True)
        return
        
    # PANDA POWER SWITCH
    if msg.topic == f"{MQTT_TOPIC_PREFIX}/panda_power/set":
 

        payload = msg.payload.decode().strip().upper()
        is_on = payload == "ON"

        # Optimistic / Pending setzen (damit WS-Status nicht sofort zurückflippt)
        desired_power_state = is_on
        power_pending_until = time.time() + POWER_CONFIRM_TIMEOUT

        mqtt_client.publish(
            f"{MQTT_TOPIC_PREFIX}/panda_power",
            "ON" if is_on else "OFF",
            retain=True
        )

        if not is_on:
            log_event(">>> PANDA POWER OFF <<<", force_console=True)
            heating_locked = True
            power_forced_off = True
            if cc2_paused_for_preheat:
                cc2_paused_for_preheat = False
                _preheat_overshoot_temp = 0
                _save_preheat_state()
                _cc2_press_button("resume")
                log_event("[CC2] Resuming CC2 before heater power off", force_console=True)

            async def hard_power_off():
                try:
                    if panda_ws:
                        await panda_ws.send(json.dumps({"settings": {"isrunning": 0}}))
                        await asyncio.sleep(0.2)

                        await panda_ws.send(json.dumps({"settings": {"work_mode": 0}}))
                        await asyncio.sleep(0.2)

                        await panda_ws.send(json.dumps({"settings": {"work_on": False}}))
                        await asyncio.sleep(0.2)
                        log_event("[POWER-OFF] Heater shutdown complete", force_console=True)
                    else:
                        log_event("[POWER-OFF-WARN] Panda not connected — shutdown command not sent", force_console=True)
                except Exception as e:
                    log_event(f"[POWER-OFF-ERR] {e}")

            asyncio.run_coroutine_threadsafe(hard_power_off(), main_loop)

            mqtt_client.publish(
                f"{MQTT_TOPIC_PREFIX}/panda_modus",
                "Standby",
                retain=True
            )

            return

        else:
            log_event(">>> PANDA POWER ON <<<", force_console=True)
            heating_locked = False
            power_forced_off = False

            async def power_on():
                try:
                    if panda_ws:
                        # ON als bool True
                        await panda_ws.send(json.dumps({"settings": {"work_on": True}}))
                        await asyncio.sleep(0.2)
                except Exception as e:
                    log_event(f"[POWER-ON-ERR] {e}")

            asyncio.run_coroutine_threadsafe(power_on(), main_loop)
            return
        
    # TEMPERATUREN & NUMERISCHE SET-WERTE
    try:
        if not msg.topic.endswith("/set"): return
        val_str = msg.payload.decode().strip()
        try:
            val = float(val_str)
        except ValueError: return
        last_ha_change = time.time()
        if msg.topic.endswith("/dry_temp/set"):
            current_data["filament_temp"] = int(val)
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/dry_temp", int(val), retain=True)
            return
        if msg.topic.endswith("/dry_time/set"):
            current_data["filament_timer"] = int(val)
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/dry_time", int(val), retain=True)
            return
        if msg.topic.endswith("/soll/set"):
            key, data_key = "set_temp", "chamber_setpoint"
        elif msg.topic.endswith("/limit/set"):
            key, data_key = "hotbedtemp", "bed_limit"
        elif msg.topic.endswith("/filtertemp/set"):
            key, data_key = "filtertemp", "filtertemp"
        else: return
        if data_key == "chamber_setpoint" and current_data.get("slicer_priority_mode", False):
            ha_memory["chamber_setpoint"] = val
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/soll", int(current_data.get("chamber_setpoint", 0)), retain=True)
            return
        current_data[data_key] = val
        mqtt_client.publish(msg.topic.replace("/set", ""), int(val), retain=True)
        if panda_ws:
            asyncio.run_coroutine_threadsafe(
                panda_ws.send(json.dumps({"settings": {key: int(val)}})),
                main_loop
            )
    except Exception as e:
        log_event(f"[TEMP-SET-ERR] {e}", force_console=True)

def setup_mqtt_discovery(client):
    base = MQTT_TOPIC_PREFIX
    dev = {"identifiers": [PRINTER_SN], "name": "Panda Breath Mod", "model": "V6.8 Final", "manufacturer": "Biqu"}
    sn = PRINTER_SN

    # Numbers
    for sfx, name, obj, unit, icon, mn, mx in [
        ("soll",       "Chamber Target",       "panda_chamber_target", "°C",  "mdi:thermometer",   0,   85),
        ("limit",      "Bed Limit",             "panda_bed_limit",      "°C",  "mdi:thermometer",   1,  120),
        ("filtertemp", "Filter Fan Activation", "panda_filter_temp",    "°C",  "mdi:fan-clock",     1,  120),
        ("dry_temp",   "Drying Temp",           "panda_dry_temp",       "°C",  "mdi:thermometer",   1,   80),
        ("dry_time",   "Drying Time",           "panda_dry_time",       "min", "mdi:timer-outline", 1,  480),
    ]:
        client.publish(f"homeassistant/number/{base}_{obj}/config", json.dumps({
            "name": name, "unique_id": f"{sn}_{obj}", "object_id": obj,
            "state_topic": f"{base}/{sfx}", "command_topic": f"{base}/{sfx}/set",
            "device": dev, "min": mn, "max": mx, "unit_of_measurement": unit, "icon": icon, "mode": "box"
        }), retain=True)

    # Select — mode
    client.publish(f"homeassistant/select/{base}_mode/config", json.dumps({
        "name": "Panda Mode", "unique_id": f"{sn}_mode", "object_id": "panda_mode",
        "state_topic": f"{base}/panda_modus", "command_topic": f"{base}/mode_select/set",
        "options": ["Automatic", "Manual", "Dry", "Standby", "LOCKED"],
        "device": dev, "icon": "mdi:state-machine"
    }), retain=True)

    # Sensors
    client.publish(f"homeassistant/sensor/{base}_chamber_temp/config", json.dumps({
        "name": "Chamber Temp", "unique_id": f"{sn}_chamber_temp", "object_id": "panda_chamber_temp",
        "state_topic": f"{base}/ist", "unit_of_measurement": "°C", "device_class": "temperature", "device": dev
    }), retain=True)

    client.publish(f"homeassistant/sensor/{base}_heat_status/config", json.dumps({
        "name": "Heat Status", "unique_id": f"{sn}_heat_status", "object_id": "panda_heat_status",
        "state_topic": f"{base}/status", "device": dev, "icon": "mdi:fire-circle"
    }), retain=True)

    client.publish(f"homeassistant/sensor/{base}_slicer_target_temp/config", json.dumps({
        "name": "Slicer Target Temp", "unique_id": f"{sn}_slicer_target_temp", "object_id": "panda_slicer_target_temp",
        "state_topic": f"{base}/slicer_target_temp", "unit_of_measurement": "°C",
        "device_class": "temperature", "device": dev
    }), retain=True)

    # CC2 temperature sensors (values converted to °C before publish)
    for sfx, name, obj in [
        ("cc2_chamber_temp", "CC2 Chamber Temp", "panda_cc2_chamber_temp"),
        ("cc2_nozzle_temp",  "CC2 Nozzle Temp",  "panda_cc2_nozzle_temp"),
        ("cc2_bed_temp",     "CC2 Bed Temp",      "panda_cc2_bed_temp"),
    ]:
        client.publish(f"homeassistant/sensor/{base}_{obj}/config", json.dumps({
            "name": name, "unique_id": f"{sn}_{obj}", "object_id": obj,
            "state_topic": f"{base}/{sfx}", "unit_of_measurement": "°C",
            "device_class": "temperature", "state_class": "measurement", "device": dev
        }), retain=True)

    client.publish(f"homeassistant/sensor/{base}_version/config", json.dumps({
        "name": "Panda Version", "unique_id": f"{sn}_version", "object_id": "panda_version",
        "state_topic": f"{base}/version", "device": dev, "icon": "mdi:information-outline"
    }), retain=True)

    client.publish(f"homeassistant/sensor/{base}_lock_status/config", json.dumps({
        "name": "Lock Status", "unique_id": f"{sn}_lock_status", "object_id": "panda_lock_status",
        "state_topic": f"{base}/lock_status", "device": dev, "icon": "mdi:lock"
    }), retain=True)

    # Binary sensors
    client.publish(f"homeassistant/binary_sensor/{base}_heating_active/config", json.dumps({
        "name": "Heating Active", "unique_id": f"{sn}_heating_active", "object_id": "panda_heating_active",
        "state_topic": f"{base}/heating", "payload_on": "ON", "payload_off": "OFF",
        "device_class": "heat", "device": dev, "icon": "mdi:radiator"
    }), retain=True)

    client.publish(f"homeassistant/binary_sensor/{base}_filter_fan/config", json.dumps({
        "name": "Filter Fan", "unique_id": f"{sn}_filter_fan", "object_id": "panda_filter_fan",
        "state_topic": f"{base}/fan", "payload_on": "ON", "payload_off": "OFF", "device": dev
    }), retain=True)

    # Switches
    client.publish(f"homeassistant/switch/{base}_power/config", json.dumps({
        "name": "Panda Power", "unique_id": f"{sn}_power", "object_id": "panda_power",
        "state_topic": f"{base}/panda_power", "command_topic": f"{base}/panda_power/set",
        "payload_on": "ON", "payload_off": "OFF", "device": dev, "icon": "mdi:power"
    }), retain=True)

    client.publish(f"homeassistant/switch/{base}_slicer_priority/config", json.dumps({
        "name": "Slicer Priority Mode", "unique_id": f"{sn}_slicer_priority", "object_id": "panda_slicer_priority",
        "state_topic": f"{base}/slicer_priority_mode", "command_topic": f"{base}/slicer_priority_mode/set",
        "payload_on": "ON", "payload_off": "OFF", "device": dev, "icon": "mdi:priority-high"
    }), retain=True)

    # Buttons
    client.publish(f"homeassistant/button/{base}_heat_stop/config", json.dumps({
        "name": "Heat Stop", "unique_id": f"{sn}_heat_stop", "object_id": "panda_heat_stop",
        "command_topic": f"{base}/heat_stop/set", "device": dev, "icon": "mdi:radiator-off"
    }), retain=True)

    client.publish(f"homeassistant/button/{base}_unlock/config", json.dumps({
        "name": "Unlock", "unique_id": f"{sn}_unlock", "object_id": "panda_unlock",
        "command_topic": f"{base}/unlock/set", "device": dev, "icon": "mdi:lock-open-variant"
    }), retain=True)


def _on_mqtt_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    log_event(f"[MQTT] Disconnected — reason={reason_code}", force_console=True)


def _on_mqtt_connect(client, userdata, flags, reason_code, properties):
    global _discovery_published
    if not reason_code.is_failure:
        try:
            sock = client.socket()
            if sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                log_event("[MQTT] TCP keepalive set (idle=10s interval=5s count=3)", force_console=True)
        except Exception as e:
            log_event(f"[MQTT-WARN] Could not set TCP keepalive: {e}", force_console=True)
        # With clean_session=False the broker retains subscriptions, but re-subscribing
        # here ensures they're fresh after any broker restart.
        client.subscribe(f"{MQTT_TOPIC_PREFIX}/#")
        if CC2_IP:
            client.subscribe(f"{CC2_TOPIC_PREFIX}/#")
        client.subscribe("homeassistant/status")  # HA birth message for discovery republish
        # Only publish autodiscovery on first connect — retained messages persist on the
        # broker and don't need to be re-sent on every reconnect (same pattern as cc2_backend).
        if not _discovery_published:
            setup_mqtt_discovery(client)
            _discovery_published = True
            log_event("[MQTT] HA autodiscovery published (first connect)", force_console=True)
        # Republish all runtime state so HA reflects current values after broker restart
        slicer_state = "ON" if current_data.get("slicer_priority_mode") else "OFF"
        client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_priority_mode", slicer_state, retain=True)
        client.publish(f"{MQTT_TOPIC_PREFIX}/panda_power", "OFF" if power_forced_off else "ON", retain=True)
        client.publish(f"{MQTT_TOPIC_PREFIX}/lock_status", "LOCKED" if global_lock else "UNLOCKED", retain=True)
        client.publish(f"{MQTT_TOPIC_PREFIX}/backend_mode", "CC2" if CC2_ACTIVE else "Klipper", retain=True)
        client.publish(f"{MQTT_TOPIC_PREFIX}/version", PANDA_VERSION, retain=True)
        # Reset volatile CC2 print state so stale retained MQTT messages from a prior session
        # don't trigger immediate heating on the next WS connect. Fresh data from cc2_backend
        # will arrive within seconds and overwrite these.
        if CC2_ACTIVE:
            current_data["cc2_print_status"] = "idle"
            current_data["chamber_setpoint"] = 0.0
        if global_lock:
            client.publish(f"{MQTT_TOPIC_PREFIX}/panda_modus", "LOCKED", retain=True)
        elif power_forced_off:
            client.publish(f"{MQTT_TOPIC_PREFIX}/panda_modus", "Standby", retain=True)


def setup_mqtt():
    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2,
                         client_id=f"PandaNative_{PRINTER_SN}",
                         clean_session=False)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_mqtt_message
    client.on_connect = _on_mqtt_connect
    client.on_disconnect = _on_mqtt_disconnect
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)
    mqtt_thread = threading.Thread(target=client.loop_forever, daemon=True, name="panda-mqtt-loop")
    mqtt_thread.start()
    return client

mqtt_client = setup_mqtt()
log_event("[MQTT] Backend logging topic active", force_console=True)
if CC2_IP:
    log_event(f"[CONFIG] CC2 mode — CC2_IP={CC2_IP} CC2_TOPIC={CC2_TOPIC_PREFIX} FILAMENT_MAP={list(FILAMENT_CHAMBER_MAP.keys())}", force_console=True)
elif CC2_HA_MODE:
    log_event("[CONFIG] CC2-HA mode — reading CC2 data from elegoo-homeassistant entities", force_console=True)
else:
    log_event(f"[CONFIG] Traditional mode — PRINTER_IP={PRINTER_IP}", force_console=True)
    



async def panda_send(payload: str) -> None:
    """Send a JSON command to the Panda device via whichever path is active."""
    if panda_ws:
        await panda_ws.send(payload)
    elif panda_writer and not panda_writer.is_closing():
        data = payload.encode()
        # WebSocket text frame: opcode 0x81, length, payload
        if len(data) < 126:
            panda_writer.write(bytes([0x81, len(data)]) + data)
        else:
            panda_writer.write(bytes([0x81, 126, len(data) >> 8, len(data) & 0xFF]) + data)
        await panda_writer.drain()
    else:
        log_event(f"[PANDA-DISCONNECTED] No WS connection — command dropped: {payload[:120]}", force_console=True)


# --- WS LOOP (OPTIMIERT: Hält Verbindung bei WiFi-Paketen offen) ---
async def update_limits_from_ws():
    global panda_ws, bind_confirmed, bind_warning_shown
    global global_heating_state, last_switch_time
    global last_live_log_state, last_live_log_time
    global last_stop_command_time, cc2_paused_for_preheat, _preheat_overshoot_temp
    global _cc2_dynamic_overshoot_active
    global _last_heat_status, _last_heat_log_time
    global _ws_bind_time
    uri = f"ws://{PANDA_IP}/ws"

    while True:

        # 🔒 LOCK HANDLING
        if global_lock:

            try:
                async with websockets.connect(f"ws://{PANDA_IP}/ws", ping_interval=20, ping_timeout=None, close_timeout=1) as ws:
                    await ws.send(json.dumps({
                        "printer": {
                            "ip": HOST_IP,
                            "sn": PRINTER_SN,
                            "access_code": ACCESS_CODE
                        }
                    }))

                    await asyncio.sleep(0.3)

                    # 2️⃣ HARD POWER OFF (MASTER SWITCH!)
                    await ws.send(json.dumps({
                        "settings": {
                            "work_on": False,
                            "work_mode": 0,
                            "set_temp": 0
                        }
                    }))

                    await asyncio.sleep(0.5)

            except Exception as e:
                log_event(f"[LOCK STOP ERROR] {e}")

            panda_ws = None
            await asyncio.sleep(2)
            continue

        # ===== NORMALER WS BETRIEB =====
        try:
            async with websockets.connect(uri, ping_interval=None, ping_timeout=None, close_timeout=1, open_timeout=10) as websocket:

                log_event(f"[WS] Connected to Panda {PANDA_IP}")
                panda_ws = websocket

                # Nur binden wenn NICHT power_forced_off
                if not power_forced_off:

                    await websocket.send(json.dumps({
                        "printer": {
                            "ip": HOST_IP,
                            "sn": PRINTER_SN,
                            "access_code": ACCESS_CODE
                        }
                    }))

                    await websocket.send(json.dumps({
                        "get_settings": 1
                    }))

                # ⏳ Bind Watchdog starten
                asyncio.create_task(bind_watchdog())

                while True:
                    try:
                        msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                    except asyncio.TimeoutError:
                        # Device went quiet — poll for current settings so temperature
                        # comparisons and heating logic keep running every ≤10s.
                        try:
                            await websocket.send(json.dumps({"get_settings": 1}))
                        except Exception:
                            pass
                        continue
                    data = json.loads(msg)

                    if global_lock:
                        continue

                    # Nur verarbeiten wenn settings enthalten
                    if 'settings' in data:

                        # ✅ Bind bestätigt
                        if not bind_confirmed:
                            bind_confirmed = True
                            bind_warning_shown = False
                            _ws_bind_time = time.time()
                            log_event(f"[WS] Bind confirmed — Panda {PANDA_IP} connected", force_console=True)
                            # NOTE: do NOT send work_mode=2 here. The original code never did.
                            # Sending commands immediately after bind causes ECONNRESET on the
                            # ESP32. work_mode=2 is set explicitly when a heat command fires
                            # (inside _cc2_heat / _cc2_heat_on_start) — it's not needed at idle.
                            # Re-sync heating state after reconnect, but only if there is
                            # an active CC2 print. Stale retained MQTT chamber_setpoint from a
                            # previous session must not trigger heating when the printer is idle.
                            chamber_target = float(current_data.get("chamber_setpoint", 0))
                            _reconnect_printing = (not CC2_ACTIVE) or current_data.get("cc2_print_status", "idle") in {
                                "printing", "preheating", "paused", "pausing", "resuming", "stopping"
                            }
                            if chamber_target > 0 and not power_forced_off and not global_lock and _reconnect_printing:
                                _reconnect_temp = (
                                    _preheat_overshoot_temp if (cc2_paused_for_preheat and _preheat_overshoot_temp > 0)
                                    else int(PANDA_MAX_TEMP) if (_cc2_dynamic_overshoot_active and CC2_ACTIVE)
                                    else int(chamber_target)
                                )
                                log_event(f"[WS-RECONNECT] Resuming heat to {_reconnect_temp}°C after WS reconnect{'  (dynamic overshoot)' if (_cc2_dynamic_overshoot_active and _reconnect_temp == int(PANDA_MAX_TEMP)) else '  (preheat overshoot)' if _reconnect_temp != int(chamber_target) else ''}", force_console=True)
                                await websocket.send(json.dumps({
                                    "settings": {
                                        "work_mode": 2,
                                        "work_on": True,
                                        "set_temp": _reconnect_temp,
                                        "isrunning": 1
                                    }
                                }))
                                await asyncio.sleep(0.3)
                                await websocket.send(json.dumps({"get_settings": 1}))
                                global_heating_state = RELAY_ON

                        incoming_settings = data['settings']

                    # NEU: fw_version extrahieren und publishen falls vorhanden
                        if 'fw_version' in incoming_settings:
                            mqtt_client.publish(
                                f"{MQTT_TOPIC_PREFIX}/fw_version",
                                str(incoming_settings['fw_version']),
                                retain=True
                            )

                        last_ws_settings.update(incoming_settings)
                        s = last_ws_settings

                        # Ist-Temperatur
                        if 'warehouse_temper' in incoming_settings:
                            current_data["chamber_temp"] = float(
                                incoming_settings['warehouse_temper']
                            )
                            mqtt_client.publish(
                                f"{MQTT_TOPIC_PREFIX}/ist",
                                incoming_settings['warehouse_temper'],
                                retain=True
                            )

                        # Auto-resume CC2 if we paused it waiting for chamber to heat.
                        # Gate on CC2 chamber sensor — it reflects actual air temp near the print.
                        # Panda Breath was set to target + PREHEAT_OVERSHOOT_DELTA during warmup
                        # to accelerate heat soak. Once CC2 is within CC2_RESUME_OFFSET of target,
                        # drop PB back to real target then resume the print.
                        if cc2_paused_for_preheat:
                            _target = float(current_data.get("chamber_setpoint", 0))
                            _pb_ist = float(current_data.get("chamber_temp", 0))
                            _cc2_ist_raw = float(current_data.get("cc2_chamber_temp", 0.0))
                            _cc2_has_data = _cc2_ist_raw > 5.0
                            _pb_ok = _pb_ist > 1.0
                            # CC2 box_temp is authoritative (PB sensor runs hotter near heater)
                            if _cc2_has_data:
                                _effective_ist = _cc2_ist_raw
                            elif _pb_ok:
                                _effective_ist = _pb_ist
                            else:
                                _effective_ist = 0.0
                            _cc2_ready = _effective_ist >= (_target - CC2_RESUME_OFFSET)
                            if _target > 0 and _cc2_ready:
                                _resume_reason = f"PB:{_pb_ist:.0f}°C CC2:{_cc2_ist_raw:.0f}°C (threshold {_target - CC2_RESUME_OFFSET:.0f}°C)"
                                log_event(f"[CC2-SLICER] Chamber ready {_resume_reason} — dropping PB to {_target:.0f}°C and resuming", force_console=True)
                                try:
                                    await panda_send(json.dumps({
                                        "settings": {"work_mode": 2, "work_on": True, "set_temp": int(_target), "isrunning": 1}
                                    }))
                                except Exception as _e:
                                    log_event(f"[CC2-SLICER] Drop-to-target error: {_e}", force_console=True)
                                _cc2_status_now = current_data.get("cc2_print_status", "")
                                if _cc2_status_now in ("paused", "pausing"):
                                    cc2_paused_for_preheat = False
                                    _preheat_overshoot_temp = 0
                                    _save_preheat_state()
                                    _cc2_press_button("resume")
                                elif _cc2_status_now in ("printing", "resuming"):
                                    # User already resumed manually — clear preheat state, no-op
                                    cc2_paused_for_preheat = False
                                    _preheat_overshoot_temp = 0
                                    _save_preheat_state()
                                    log_event(f"[CC2-SLICER] Chamber ready, CC2 already {_cc2_status_now} — clearing preheat state", force_console=True)
                                else:
                                    # Transient state (stopping, initializing, etc.) — keep flag, retry next cycle
                                    log_event(f"[CC2-SLICER] Chamber ready but CC2 status={_cc2_status_now!r} — retrying next cycle", force_console=True)

                        # CC2 mode: bed_temp updated via MQTT subscription
                        # Traditional mode: fetch from HA REST API
                        if not CC2_ACTIVE:
                            try:
                                ha_resp = requests.get(
                                    HA_URL,
                                    headers={"Authorization": f"Bearer {HA_TOKEN}"},
                                    timeout=2
                                )
                                ha_resp.raise_for_status()
                                ha_json = ha_resp.json()
                                if isinstance(ha_json, dict):
                                    raw_state = str(ha_json.get("state", "")).strip()
                                else:
                                    raw_state = str(ha_json).strip()
                                current_data["bed_temp"] = safe_float(raw_state, 0.0)
                            except Exception:
                                pass

                        mqtt_client.publish(
                            f"{MQTT_TOPIC_PREFIX}/bed",
                            f"{safe_float(current_data.get('bed_temp', 0)):.1f}",
                            retain=True
                        )

                        # ===== SET_TEMP SYNC =====
                        if 'set_temp' in incoming_settings:

                            ws_temp = float(incoming_settings['set_temp'])
                            slicer_active = current_data.get("slicer_priority_mode", False)
                            ws_work_mode = int(s.get("work_mode", 0) or 0)
                            ws_work_on = s.get("work_on") in (1, True, "1")

                            # In CC2 mode, don't sync the device's remembered set_temp into
                            # chamber_setpoint when there is no active print. The device retains
                            # the last set_temp across reboots; picking it up at idle would trigger
                            # heating immediately after every WS connect (ECONNRESET loop).
                            _cc2_active_now = CC2_ACTIVE and current_data.get("cc2_print_status", "idle") in {
                                "printing", "preheating", "paused", "pausing", "resuming", "stopping"
                            }
                            if CC2_ACTIVE and not _cc2_active_now:
                                pass  # skip set_temp sync when idle in CC2 mode
                            elif slicer_active:
                                if ws_temp > 0:
                                    current_data["chamber_setpoint"] = ws_temp
                                    mqtt_client.publish(
                                        f"{MQTT_TOPIC_PREFIX}/soll",
                                        int(ws_temp),
                                        retain=True
                                    )
                            else:
                                if (
                                    (time.time() - last_ha_change) > 5.0
                                    and ws_temp > 0
                                    and ws_work_mode in (1, 2, 3)
                                    and ws_work_on
                                ):
                                    current_data["chamber_setpoint"] = ws_temp
                                    mqtt_client.publish(
                                        f"{MQTT_TOPIC_PREFIX}/soll",
                                        int(ws_temp),
                                        retain=True
                                    )

                        if 'hotbedtemp' in s:
                            current_data["bed_limit"] = float(s['hotbedtemp'])

                        if 'filtertemp' in s:
                            current_data["filtertemp"] = float(s['filtertemp'])

                        if 'filament_temp' in s:
                            current_data["filament_temp"] = int(s['filament_temp'])

                        if 'filament_timer' in s:
                            current_data["filament_timer"] = int(s['filament_timer'])

                        # ===== MODUS =====
                        global last_reported_mode, mode_change_hint

                        work_mode = s.get("work_mode")
                        work_on = s.get("work_on")

                        if global_lock:
                            modus = "LOCKED"
                        elif power_forced_off:
                            modus = "Standby"
                        else:
                            if work_mode == 1:
                                modus = "Automatic"
                            elif work_mode == 2:
                                modus = "Manual"
                            elif work_mode == 3:
                                modus = "Dry"
                            else:
                                modus = "Standby"

                        if modus != last_reported_mode:
                            mqtt_client.publish(
                                f"{MQTT_TOPIC_PREFIX}/panda_modus",
                                modus,
                                retain=True
                            )
                            last_reported_mode = modus

                        # ===== MQTT Sync =====
                        if (time.time() - last_ha_change) > 8.0:

                            if 'filtertemp' in s:
                                mqtt_client.publish(
                                    f"{MQTT_TOPIC_PREFIX}/filtertemp",
                                    int(s['filtertemp']),
                                    retain=True
                                )

                            if 'hotbedtemp' in s:
                                mqtt_client.publish(
                                    f"{MQTT_TOPIC_PREFIX}/limit",
                                    int(s['hotbedtemp']),
                                    retain=True
                                )

                            if 'work_on' in s:
                                global desired_power_state, power_pending_until

                                ws_is_on = s['work_on'] in (True, 1, "1")
                                now = time.time()

                                # Während Pending: nicht zurückflippen
                                if desired_power_state is not None and now < power_pending_until:
                                    p_val = "1" if desired_power_state else "0"

                                    # Sobald bestätigt → Pending löschen
                                    if ws_is_on == desired_power_state:
                                        desired_power_state = None
                                        power_pending_until = 0.0

                                else:
                                    # Normalbetrieb
                                    p_val = "0" if power_forced_off else ("1" if ws_is_on else "0")

                                mqtt_client.publish(
                                    f"{MQTT_TOPIC_PREFIX}/work_on",
                                    p_val,
                                    retain=True
                                )


                            if 'filament_temp' in s:
                                mqtt_client.publish(
                                    f"{MQTT_TOPIC_PREFIX}/dry_temp",
                                    int(s['filament_temp']),
                                    retain=True
                                )

                            if 'filament_timer' in s:
                                mqtt_client.publish(
                                    f"{MQTT_TOPIC_PREFIX}/dry_time",
                                    int(s['filament_timer']),
                                    retain=True
                                )

                            mqtt_client.publish(
                                f"{MQTT_TOPIC_PREFIX}/slicer_priority_mode",
                                "ON" if current_data.get("slicer_priority_mode", False) else "OFF",
                                retain=True
                            )

                            mqtt_client.publish(
                                f"{MQTT_TOPIC_PREFIX}/slicer_soll",
                                int(current_data.get("slicer_soll", 0)),
                                retain=True
                            )

                            mqtt_client.publish(
                                f"{MQTT_TOPIC_PREFIX}/slicer_target_temp",
                                int(current_data.get("slicer_soll", 0)),
                                retain=True
                            )

                            mqtt_client.publish(
                                f"{MQTT_TOPIC_PREFIX}/slicer_file",
                                current_data.get("last_analyzed_file", ""),
                                retain=True
                            )

                        target = float(current_data.get("chamber_setpoint", 0))
                        ist = float(current_data.get("chamber_temp", 0))
                        limit = float(current_data.get("bed_limit", 50))
                        bed_ist = float(current_data.get("bed_temp", 0))
                        work_mode_live = int(s.get("work_mode", 0) or 0)
                        work_on_live = s.get("work_on")
                        panda_running = s.get("isrunning") in (1, True, "1")

                        _cc2_printing = CC2_ACTIVE and current_data.get("cc2_print_status", "idle") in {
                            "printing", "preheating", "paused", "pausing", "resuming", "stopping"
                        }

                        if global_lock:
                            target_state, info = RELAY_OFF, "LOCKED"

                        elif power_forced_off or work_mode_live not in (1, 2, 3):
                            target_state, info = RELAY_OFF, "Standby"

                        elif work_mode_live == 3:
                            if ist < (target - HYSTERESE):
                                target_state, info = RELAY_ON, "Heating..."
                            else:
                                target_state, info = RELAY_OFF, "At Temperature"

                        elif work_mode_live == 1:
                            # CC2 mode: skip bed sensor check — device can't see CC2's Klipper,
                            # so bed_ist stays 0 and the "Done" branch would block heating.
                            if not CC2_ACTIVE and bed_ist <= limit:
                                target_state, info = RELAY_OFF, "Done"
                            elif ist < (target - HYSTERESE):
                                target_state, info = RELAY_ON, "Heating..."
                            else:
                                target_state, info = RELAY_OFF, "At Temperature"

                        elif work_mode_live == 2:
                            if CC2_ACTIVE and (target == 0 or not _cc2_printing):
                                _cc2_dynamic_overshoot_active = False
                                target_state, info = RELAY_OFF, "Idle"
                            elif ist < (target - HYSTERESE):
                                # PB still climbing — set overshoot flag so heat cmd uses right set_temp
                                if CC2_ACTIVE and _cc2_printing and target > 0:
                                    _cc2_now = float(current_data.get("cc2_chamber_temp", 0.0))
                                    _cc2_delta = target - _cc2_now
                                    if _cc2_delta > CC2_OVERSHOOT_MAX_DELTA:
                                        _cc2_dynamic_overshoot_active = True
                                    elif _cc2_delta <= CC2_OVERSHOOT_MIN_DELTA:
                                        _cc2_dynamic_overshoot_active = False
                                target_state, info = RELAY_ON, "Heating..."
                            elif CC2_ACTIVE and _cc2_printing and target > 0:
                                _cc2_now = float(current_data.get("cc2_chamber_temp", 0.0))
                                _cc2_delta = target - _cc2_now
                                _prev_overshoot = _cc2_dynamic_overshoot_active
                                if _cc2_delta > CC2_OVERSHOOT_MAX_DELTA:
                                    _cc2_dynamic_overshoot_active = True
                                elif _cc2_delta <= CC2_OVERSHOOT_MIN_DELTA:
                                    _cc2_dynamic_overshoot_active = False
                                # send live set_temp update only on overshoot state change
                                if _cc2_dynamic_overshoot_active != _prev_overshoot and panda_ws:
                                    try:
                                        _new_st = int(PANDA_MAX_TEMP) if _cc2_dynamic_overshoot_active else int(target)
                                        await panda_ws.send(json.dumps({"settings": {"set_temp": _new_st}}))
                                        log_event(
                                            f"[CC2-OVERSHOOT-{'ON' if _cc2_dynamic_overshoot_active else 'OFF'}] "
                                            f"CC2:{_cc2_now:.0f}°C target:{target:.0f}°C Δ{_cc2_delta:.0f}°C → set_temp {_new_st}°C",
                                            force_console=True
                                        )
                                    except Exception as _oe:
                                        log_event(f"[CC2-OVERSHOOT-ERR] set_temp update failed: {_oe}", force_console=True)
                                if _cc2_now > 5.0 and _cc2_delta > CC2_OVERSHOOT_MIN_DELTA:
                                    # PB at setpoint but CC2 still cold — keep heater on
                                    target_state, info = RELAY_ON, "CC2 Warming..."
                                else:
                                    # Both sensors satisfied — device PID maintains setpoint autonomously
                                    target_state, info = RELAY_ON, "At Temperature"
                            else:
                                _cc2_dynamic_overshoot_active = False
                                target_state, info = RELAY_OFF, "At Temperature"

                        else:
                            target_state, info = RELAY_OFF, "Standby"

                        time_passed = (time.time() - last_switch_time)

                        if target_state == RELAY_OFF:
                            now_stop = time.time()

                            # V1.0.3 / Klipper bind:
                            # isrunning=0 alleine reicht nicht mehr, weil die Panda-Firmware
                            # im Klipper-Auto-Modus den Heizlauf selbst wieder starten kann.
                            # Darum wird der aktive Lauf mit work_on=False pausiert.
                            # WICHTIG: work_mode und set_temp bleiben unverändert, damit
                            # Kammer-Soll und gewählter Modus NICHT verloren gehen.
                            if global_heating_state != RELAY_OFF:
                                global_heating_state = RELAY_OFF
                                last_switch_time = now_stop

                            if (panda_running or work_on_live in (1, True, "1")) and (now_stop - last_stop_command_time) >= 2.0:
                                last_stop_command_time = now_stop
                                try:
                                    if panda_ws:
                                        await panda_ws.send(json.dumps({
                                            "settings": {
                                                "isrunning": 0,
                                                "work_on": False
                                            }
                                        }))
                                    else:
                                        log_event(f"[AUTO-OFF-WARN] Panda not connected — cannot stop heating (chamber={ist:.1f}°C)", force_console=True)
                                except Exception as e:
                                    log_event(f"[AUTO-OFF-ERR] chamber={ist:.1f}°C target={target:.1f}°C: {e}", force_console=True)

                        elif (
                            # Suppress heating for 5s after bind — gives cc2_backend time to
                            # publish fresh CC2 state that overwrites stale retained MQTT values.
                            CC2_ACTIVE and (time.time() - _ws_bind_time) < 5.0
                        ):
                            pass  # grace period: observe state but don't command

                        elif (
                            target_state != global_heating_state
                            and (
                                current_data.get("slicer_priority_mode", False)
                                or time_passed > MIN_SWITCH_TIME
                            )
                        ) or (target_state == RELAY_ON and not panda_running and info == "Heating..."):
                            if not panda_ws:
                                log_event(f"[AUTO-ON-WARN] Panda not connected — cannot start heating to {int(target)}°C (chamber={ist:.1f}°C)", force_console=True)
                            else:
                                global_heating_state = target_state
                                last_switch_time = time.time()
                                last_stop_command_time = 0
                                try:
                                    _effective_target = (
                                        _preheat_overshoot_temp if (cc2_paused_for_preheat and _preheat_overshoot_temp > 0)
                                        else int(PANDA_MAX_TEMP) if (_cc2_dynamic_overshoot_active and CC2_ACTIVE)
                                        else int(target)
                                    )
                                    heat_cmd: dict = {
                                        "work_on": True,
                                        "set_temp": _effective_target,
                                        "isrunning": 1
                                    }
                                    if CC2_ACTIVE:
                                        heat_cmd["work_mode"] = 2
                                    await panda_ws.send(json.dumps({"settings": heat_cmd}))
                                    # Poll immediately so device confirms isrunning=1 and
                                    # heating flips to ON within ~0.5s instead of ~10s.
                                    await asyncio.sleep(0.3)
                                    await panda_ws.send(json.dumps({"get_settings": 1}))
                                except Exception as e:
                                    log_event(f"[AUTO-ON-ERR] target={int(target)}°C chamber={ist:.1f}°C: {e}", force_console=True)
                                    global_heating_state = RELAY_OFF  # Reset so next cycle retries

                        fan_state = "ON" if bed_ist >= float(current_data.get("filtertemp", 30.0)) else "OFF"
                        actual_heating = (panda_running and work_on_live in (1, True, "1"))
                        # In CC2 mode device confirmation (isrunning) can lag; use our commanded
                        # state for the Heat tile. In legacy mode use device confirmation.
                        heating_active = (global_heating_state == RELAY_ON) if CC2_ACTIVE else actual_heating

                        # State-transition logging — always on, not gated by DEBUG
                        _now_log = time.time()
                        if info != _last_heat_status:
                            if info == "Heating...":
                                _cc2_log = float(current_data.get("cc2_chamber_temp", 0.0))
                                log_event(
                                    f"[HEAT-ON] PB:{ist:.0f}°C CC2:{_cc2_log:.0f}°C — target {target:.0f}°C "
                                    f"(threshold {target - HYSTERESE:.0f}°C) — heater starting",
                                    force_console=True
                                )
                            elif info == "CC2 Warming...":
                                _cc2_log = float(current_data.get("cc2_chamber_temp", 0.0))
                                log_event(
                                    f"[CC2-HEAT] PB:{ist:.0f}°C at setpoint but CC2:{_cc2_log:.0f}°C still cold "
                                    f"(target {target:.0f}°C) — keeping heater on",
                                    force_console=True
                                )
                            elif _last_heat_status == "Heating..." and info == "At Temperature":
                                if CC2_ACTIVE and _cc2_printing:
                                    _cc2_log = float(current_data.get("cc2_chamber_temp", 0.0))
                                    log_event(
                                        f"[AT-TEMP] PB:{ist:.0f}°C CC2:{_cc2_log:.0f}°C target {target:.0f}°C — heater holding",
                                        force_console=True
                                    )
                                else:
                                    log_event(
                                        f"[HEAT-OFF] Chamber {ist:.0f}°C reached target {target:.0f}°C — heater off",
                                        force_console=True
                                    )
                            elif info == "Idle":
                                log_event("[IDLE] No active CC2 print — heater standby", force_console=True)
                            elif info == "At Temperature" and _last_heat_status not in ("Heating...", "CC2 Warming...", ""):
                                _cc2_log = float(current_data.get("cc2_chamber_temp", 0.0))
                                log_event(
                                    f"[AT-TEMP] PB:{ist:.0f}°C CC2:{_cc2_log:.0f}°C target:{target:.0f}°C Δ{ist - _cc2_log:+.0f}°C — within hysteresis, holding",
                                    force_console=True
                                )
                            _last_heat_status = info
                            _last_heat_log_time = _now_log
                        elif (_now_log - _last_heat_log_time) >= 60:
                            _cc2_log = float(current_data.get("cc2_chamber_temp", 0.0))
                            log_event(
                                f"[STATUS] PB:{ist:.0f}°C CC2:{_cc2_log:.0f}°C target:{target:.0f}°C Δ{ist - _cc2_log:+.0f}°C | "
                                f"Heat:{'ON' if heating_active else 'OFF'} | {info}",
                                force_console=True
                            )
                            _last_heat_log_time = _now_log

                        mqtt_client.publish(
                            f"{MQTT_TOPIC_PREFIX}/heating",
                            "ON" if heating_active else "OFF",
                            retain=True
                        )
                        mqtt_client.publish(
                            f"{MQTT_TOPIC_PREFIX}/status",
                            info,
                            retain=True
                        )
                        mqtt_client.publish(
                            f"{MQTT_TOPIC_PREFIX}/panda_heiz_status",
                            info,
                            retain=True
                        )
                        mqtt_client.publish(
                            f"{MQTT_TOPIC_PREFIX}/fan",
                            fan_state,
                            retain=True
                        )

                        if DEBUG:
                            now = time.time()

                            current_live_log_state = (
                                round(bed_ist, 1),
                                round(target, 1),
                                round(ist, 1),
                                actual_heating,
                                fan_state,
                                work_mode_live,
                                info
                            )

                            if (
                                current_live_log_state != last_live_log_state
                                or (now - last_live_log_time) >= 30
                            ):
                                last_live_log_state = current_live_log_state
                                last_live_log_time = now

                                log_event(
                                    f"LIVE | Bed:{bed_ist:.1f}°C | Chamber:{target:.1f}/{ist:.1f}°C | "
                                    f"Heat:{'ON' if actual_heating else 'OFF'} | "
                                    f"Fan:{fan_state} | Mode:{work_mode_live} | Status:{info}"
                                )


                    else:
                        continue

        except Exception as e:
            err = str(e)
            if "no close frame received or sent" not in err:
                log_event(f"[WS] Connection error ({PANDA_IP}): {err}", force_console=True)

            panda_ws = None
            bind_confirmed = False  # Force work_mode=2 re-sync on next connect
            global_heating_state = RELAY_OFF  # Clear stale state so HA doesn't show Heat=ON while offline
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/heating", "OFF", retain=True)
            # Publish status while WS is down so HA MQTT keepalive timer resets every 5s
            # (without this, no data flows to HA during reconnect → keepalive timeout at 60s)
            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/status", "Panda Breath offline", retain=True)
            await asyncio.sleep(10)

async def bind_watchdog():
    global bind_confirmed, bind_warning_shown

    # In CC2 mode the device auto-binds from credentials — no Panda Touch UI exists.
    # Give it more time since slow WiFi can delay the first settings response.
    await asyncio.sleep(30 if CC2_ACTIVE else 10)

    if not bind_confirmed and not bind_warning_shown:
        if CC2_ACTIVE:
            log_event("[WS] Waiting for Panda Breath bind (auto-bind via credentials)...", force_console=True)
        else:
            log_event("⚠️ Please press Bind in the Panda UI!", force_console=True)
            mqtt_client.publish(
                f"{MQTT_TOPIC_PREFIX}/status",
                "Please press Bind in the Panda UI",
                retain=True
            )

        bind_warning_shown = True
        
# --- EMULATION ---
async def handle_panda(reader, writer):

    global last_switch_time, global_heating_state, terminal_cleared, mode_change_hint, bed_sensor_error, panda_writer, cc2_paused_for_preheat
    setup_mqtt_discovery(mqtt_client)
    panda_writer = writer
    log_event("[SERVER] Panda client connected", force_console=True)
    try:
        # Initialer Handshake
        await reader.read(1024); writer.write(b'\x20\x02\x00\x00'); await writer.drain()
        sub_data = await reader.read(1024)
        if sub_data and sub_data[0] == 0x82:
            writer.write(b'\x90\x03' + sub_data[2:4] + b'\x00'); await writer.drain()

        while not writer.is_closing():
            try:
                if CC2_ACTIVE:
                    # In CC2 mode bed temp arrives via MQTT from cc2_connector — no HA REST needed.
                    # Never reset global_heating_state here; the WS loop owns heating in CC2 mode.
                    bed_ist = safe_float(current_data.get("bed_temp", 0), 0.0)
                else:
                    # ============================================================
                    # HA REST bed sensor fetch (traditional / non-CC2 mode only)
                    # ============================================================
                    loop = asyncio.get_running_loop()

                    def fetch_ha():
                        return requests.get(
                            HA_URL,
                            headers={"Authorization": f"Bearer {HA_TOKEN}"},
                            timeout=2
                        )

                    try:
                        h_resp = await loop.run_in_executor(None, fetch_ha)
                        h_resp.raise_for_status()

                        try:
                            ha_data = h_resp.json()
                        except Exception:
                            raw_text = h_resp.text.strip()
                            if raw_text in ("unknown", "unavailable", ""):
                                raise ValueError(f"Invalid bed sensor state: {raw_text}")
                            bed_ist = float(raw_text)
                        else:
                            if isinstance(ha_data, dict):
                                raw_state = str(ha_data.get("state", "")).strip()
                            else:
                                raw_state = str(ha_data).strip()

                            if raw_state in ("unknown", "unavailable", ""):
                                raise ValueError(f"Invalid bed sensor state: {raw_state}")
                            bed_ist = float(raw_state)

                        if bed_sensor_error:
                            log_event("[BED-SENSOR] Connection restored", force_console=True)
                            mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/status", "Ready", retain=True)
                            bed_sensor_error = False

                    except Exception as ha_err:
                        bed_ist = 0.0
                        global_heating_state = RELAY_OFF

                        if not bed_sensor_error:
                            log_event(f"[BED-SENSOR-ERR] {ha_err}", force_console=True)
                            mqtt_client.publish(
                                f"{MQTT_TOPIC_PREFIX}/status",
                                "Check Bed Temperature Sensor",
                                retain=True
                            )
                            bed_sensor_error = True

                        await asyncio.sleep(2)
                        continue
                # ============================================================
                
                # 2. Variablen laden
                target, ist, limit = current_data["chamber_setpoint"], current_data["chamber_temp"], current_data["bed_limit"]
                f_threshold = current_data.get("filtertemp", 30.0)
                work_mode = int(last_ws_settings.get("work_mode", 0) or 0)
                work_on = last_ws_settings.get("work_on")

                # ============================================================
                # ✅ GLOBAL LOCK LOGIK (FIXED & STABILE HYSTERESE)
                # ------------------------------------------------------------
                if global_lock:
                    target_state, info = RELAY_OFF, "LOCKED"
                    global_heating_state = RELAY_OFF

                else:
                    _cc2_printing = CC2_ACTIVE and current_data.get("cc2_print_status", "idle") in {
                        "printing", "preheating", "paused", "pausing", "resuming", "stopping"
                    }

                    if power_forced_off or work_mode not in (1, 2, 3):
                        target_state, info = RELAY_OFF, "Standby"

                    elif work_mode == 3:
                        target = float(last_ws_settings.get("custom_temp", current_data.get("filament_temp", target)))
                        remaining = int(last_ws_settings.get("remaining_seconds", 0) or 0)

                        if remaining <= 0:
                            target_state, info = RELAY_OFF, "Done"
                        elif ist < (target - HYSTERESE):
                            target_state, info = RELAY_ON, "Heating..."
                        else:
                            target_state, info = RELAY_OFF, "At Temperature"

                    elif work_mode == 1:
                        if not CC2_ACTIVE and bed_ist <= limit:
                            target_state, info = RELAY_OFF, "Done"
                        elif ist < (target - HYSTERESE):
                            target_state, info = RELAY_ON, "Heating..."
                        else:
                            target_state, info = RELAY_OFF, "At Temperature"

                    elif work_mode == 2:
                        if CC2_ACTIVE and (target == 0 or not _cc2_printing):
                            target_state, info = RELAY_OFF, "Idle"
                        elif ist < (target - HYSTERESE):
                            target_state, info = RELAY_ON, "Heating..."
                        elif CC2_ACTIVE and _cc2_printing and target > 0:
                            target_state, info = RELAY_ON, "At Temperature"
                        else:
                            target_state, info = RELAY_OFF, "At Temperature"

                    else:
                        target_state, info = RELAY_OFF, "Standby"

                    # ========================================================
                    # ⏱ SWITCH-TIMER LOGIK
                    # ========================================================
                    time_passed = (time.time() - last_switch_time)

                    # In CC2 mode the WS loop (update_limits_from_ws) owns global_heating_state.
                    # TLS emulation loop only reads it for display; never mutates it here.
                    if not CC2_ACTIVE:
                        if target_state == RELAY_OFF and global_heating_state != RELAY_OFF:
                            global_heating_state = RELAY_OFF
                            last_switch_time = time.time()

                        elif (
                            target_state != global_heating_state
                            and (
                                current_data.get("slicer_priority_mode", False)
                                or time_passed > MIN_SWITCH_TIME
                            )
                        ):
                            global_heating_state = target_state
                            last_switch_time = time.time()

                # ============================================================

                # 4. Lüfter-Logik (Filter Fan)
                fan_state = "ON" if bed_ist >= f_threshold else "OFF"
                
# 5. Anzeige & MQTT Update
                sl = int(current_data.get("slicer_soll", 0))
                sl_prio = "SL-PRIO" if current_data.get("slicer_priority_mode", False) else "NORMAL"
                lock_indicator = "⚠️ LOCKED ⚠️" if global_lock else "READY"
                line = f"\r🟢 {lock_indicator} | Bed:{bed_ist}° | Chamber:{target}/{ist}° | Heat:{'ON' if global_heating_state == RELAY_ON else 'OFF'} | Fan:{fan_state} | {info} | {sl_prio}:{sl}°"
                
                mode_change_hint = ""
                if not terminal_cleared: os.system('clear'); terminal_cleared = True
                print(f"{line}\033[K", end="", flush=True)

                # Status-Entitäten an HA senden
                mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/status", info, retain=True)
                mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/panda_heiz_status", info, retain=True)
                mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/fan", fan_state, retain=True)
                mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/version", PANDA_VERSION, retain=True)

                # 6. Report-Paket für den Panda bauen
                data = {
                    "print": {
                        "command": "push_status",
                        "msg": 1,
                        "sequence_id": str(int(time.time())),
                        "warehouse_temper": float(ist),
                        "bed_temper": float(bed_ist),
                        "chamber_temper": float(ist),
                        "bed_target_temper": float(int(target)) if global_heating_state == RELAY_ON else 0.0,
                        "gcode_state": "RUNNING" if global_heating_state == RELAY_ON else "IDLE",
                        "mc_percent": 50
                    }
                }
                
                payload = json.dumps(data).encode()
                topic = f"device/{PRINTER_SN}/report".encode()
                vh = len(topic).to_bytes(2, 'big') + topic
                rem = len(vh) + len(payload)

                # MQTT Variable Length Encoding für das Display-Protokoll
                pkt = b'\x30'
                X = rem
                while X > 0:
                    eb = X % 128
                    X //= 128
                    if X > 0: eb |= 128
                    pkt += eb.to_bytes(1, 'big')

                writer.write(pkt + vh + payload); await writer.drain()

            except Exception as e:
                log_event(f"[EMU-LOOP-ERR] {e}", force_console=True); break
            
            await asyncio.sleep(2)

    finally:
        panda_writer = None
        writer.close()
        log_event("[SERVER] Panda client disconnected", force_console=True)

def _discover_ha_cc2_entities() -> dict:
    """Query HA /api/states and find CC2 entities from elegoo-homeassistant."""
    try:
        resp = requests.get(
            f"{HA_BASE_URL}/api/states",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            timeout=5,
        )
        resp.raise_for_status()
        all_states = resp.json()
    except Exception as e:
        log_event(f"[CC2-HA] HA state query failed: {e}", force_console=True)
        return {}

    # Explicit env var overrides take priority
    found = {}
    for key, env_var in [
        ("chamber_temp",        "HA_CC2_CHAMBER_ENTITY"),
        ("nozzle_temp",         "HA_CC2_NOZZLE_ENTITY"),
        ("bed_temp",            "HA_CC2_BED_ENTITY"),
        ("print_status",        "HA_CC2_STATUS_ENTITY"),
        ("print_progress",      "HA_CC2_PROGRESS_ENTITY"),
        ("active_filament_type","HA_CC2_FILAMENT_ENTITY"),
        ("filename",            "HA_CC2_FILENAME_ENTITY"),
        ("_pause_button",       "HA_CC2_PAUSE_ENTITY"),
        ("_resume_button",      "HA_CC2_RESUME_ENTITY"),
    ]:
        v = os.environ.get(env_var, "")
        if v:
            found[key] = v

    def is_panda_published_entity(state: dict) -> bool:
        attrs = state.get("attributes", {})
        label = " ".join(
            str(v).lower()
            for v in (
                state.get("entity_id", ""),
                attrs.get("friendly_name", ""),
                attrs.get("object_id", ""),
                attrs.get("unique_id", ""),
            )
        )
        return any(
            marker in label
            for marker in (
                "panda_breath_mod",
                "panda_cc2_",
                "panda chamber temp",
                "panda nozzle temp",
                "panda bed temp",
            )
        )

    # Auto-discover remaining by filtering to Elegoo/CC2 candidates. Ignore
    # Panda's own HA mirror sensors so CC2 data cannot loop back into itself.
    cc2_markers = {"elegoo", "centauri", "carbon", "cc2"}
    candidates = [
        s for s in all_states
        if not is_panda_published_entity(s) and any(
            m in s["entity_id"].lower() or
            m in s.get("attributes", {}).get("friendly_name", "").lower()
            for m in cc2_markers
        )
    ]

    for s in candidates:
        eid = s["entity_id"]
        label = (eid + " " + s.get("attributes", {}).get("friendly_name", "")).lower()
        dc = s.get("attributes", {}).get("device_class", "")
        domain = eid.split(".")[0]

        if "chamber_temp" not in found and dc == "temperature" and any(k in label for k in ("chamber", "box_temp", "enclosure")):
            found["chamber_temp"] = eid
        if "nozzle_temp" not in found and dc == "temperature" and "nozzle" in label:
            found["nozzle_temp"] = eid
        if "bed_temp" not in found and dc == "temperature" and "bed" in label:
            found["bed_temp"] = eid
        if "print_status" not in found and domain == "sensor" and any(k in label for k in ("print_status", "print_state")):
            found["print_status"] = eid
        if "print_progress" not in found and domain == "sensor" and any(k in label for k in ("progress", "percent_complete")):
            found["print_progress"] = eid
        if "filename" not in found and domain == "sensor" and any(k in label for k in ("filename", "file_name", "current_file", "print_file")):
            found["filename"] = eid
        if "_pause_button" not in found and domain == "button" and "pause" in label:
            found["_pause_button"] = eid
        if "_resume_button" not in found and domain == "button" and "resume" in label:
            found["_resume_button"] = eid
        # Slot-based filament discovery (elegoo-homeassistant uses a1-a4 slots)
        if "_active_filament_color" not in found and domain == "sensor" and "active_filament_color" in label:
            found["_active_filament_color"] = eid
        for slot in ("a1", "a2", "a3", "a4"):
            if f"_{slot}_color" not in found and domain == "sensor" and f"{slot}_color" in label and "active" not in label:
                found[f"_{slot}_color"] = eid
            if f"_{slot}_name" not in found and domain == "sensor" and f"{slot}_name" in label:
                found[f"_{slot}_name"] = eid

    log_event(f"[CC2-HA] Entity discovery: {found}", force_console=True)
    return found


async def ha_cc2_poller():
    """Poll HA entities for CC2 data when CC2_HA_MODE is enabled (elegoo-homeassistant)."""
    global _ha_cc2_entities
    loop = asyncio.get_event_loop()

    # Retry discovery until key entities are found (up to ~2 min)
    for attempt in range(12):
        entities = await loop.run_in_executor(None, _discover_ha_cc2_entities)
        if entities.get("chamber_temp") or entities.get("print_status"):
            _ha_cc2_entities = entities
            log_event(
                f"[CC2-HA] Mode active — polling {len([k for k in entities if not k.startswith('_')])} entities every 3s",
                force_console=True,
            )
            break
        log_event(f"[CC2-HA] Discovery attempt {attempt + 1}: no CC2 entities found, retrying in 10s", force_console=True)
        await asyncio.sleep(10)
    else:
        log_event(
            "[CC2-HA] Could not discover CC2 entities after 2 min. "
            "Set HA_CC2_CHAMBER_ENTITY / HA_CC2_STATUS_ENTITY env vars to override.",
            force_console=True,
        )
        return

    # cc2_key → (transform_fn) — maps HA entity state string to the value _handle_cc2_data expects
    KEY_TRANSFORMS = {
        "chamber_temp":         lambda v: v,
        "nozzle_temp":          lambda v: v,
        "bed_temp":             lambda v: v,
        "print_status":         lambda v: v.lower(),
        "print_progress":       lambda v: str(int(float(v))),
        "active_filament_type": lambda v: v,
        "filename":             lambda v: v,
    }

    _log_state = {"last_filename": None, "last_color_unavail": None, "last_filament": None}

    while True:
        try:
            def fetch_all():
                results = {}

                # ── Step 1: resolve filament type FIRST ──────────────────────────
                # Must happen before print_status so chamber_setpoint is set when
                # the idle→printing transition fires. Otherwise preheat silently
                # skips because chamber_target=0 at transition time.

                # 1a. Filename-encoded chamber temp (highest priority, no name matching)
                # Convention: include NNc or NNC in filename, e.g. asa_benchy_55C.gcode
                _fname_eid = _ha_cc2_entities.get("filename")
                if _fname_eid:
                    try:
                        _fr = requests.get(
                            f"{HA_BASE_URL}/api/states/{_fname_eid}",
                            headers={"Authorization": f"Bearer {HA_TOKEN}"},
                            timeout=3,
                        )
                        _fname = _fr.json().get("state", "")
                        if _fname and _fname not in ("unknown", "unavailable", ""):
                            results["filename"] = _fname
                            _temp_from_name = _chamber_temp_from_filename(_fname)
                            if _temp_from_name > 0:
                                results["_filename_chamber_temp"] = _temp_from_name
                                if _fname != _log_state["last_filename"]:
                                    log_event(f"[CC2-HA] Chamber temp from filename: {_fname!r} → {_temp_from_name:.0f}°C", force_console=True)
                                    _log_state["last_filename"] = _fname
                    except Exception as _fe:
                        log_event(f"[CC2-HA] Filename fetch error: {_fe}", force_console=True)

                # 1b. Color cross-ref filament name (fallback if no temp in filename)
                if "_filename_chamber_temp" not in results and "_active_filament_color" in _ha_cc2_entities:
                    try:
                        r = requests.get(
                            f"{HA_BASE_URL}/api/states/{_ha_cc2_entities['_active_filament_color']}",
                            headers={"Authorization": f"Bearer {HA_TOKEN}"},
                            timeout=3,
                        )
                        active_color = r.json().get("state", "").upper().strip()
                        if active_color and active_color not in ("UNKNOWN", "UNAVAILABLE", ""):
                            for slot in ("a1", "a2", "a3", "a4"):
                                color_eid = _ha_cc2_entities.get(f"_{slot}_color")
                                name_eid = _ha_cc2_entities.get(f"_{slot}_name")
                                if not color_eid or not name_eid:
                                    continue
                                rc = requests.get(
                                    f"{HA_BASE_URL}/api/states/{color_eid}",
                                    headers={"Authorization": f"Bearer {HA_TOKEN}"},
                                    timeout=3,
                                )
                                if rc.json().get("state", "").upper().strip() == active_color:
                                    rn = requests.get(
                                        f"{HA_BASE_URL}/api/states/{name_eid}",
                                        headers={"Authorization": f"Bearer {HA_TOKEN}"},
                                        timeout=3,
                                    )
                                    slot_name = rn.json().get("state", "")
                                    if slot_name and slot_name not in ("unknown", "unavailable", ""):
                                        results["active_filament_type"] = slot_name
                                        filament_sig = (slot, active_color, slot_name)
                                        if filament_sig != _log_state["last_filament"]:
                                            log_event(
                                                f"[CC2-HA] Active filament resolved: slot={slot} color={active_color} name={slot_name!r}",
                                                force_console=True,
                                            )
                                            _log_state["last_filament"] = filament_sig
                                    else:
                                        log_event(f"[CC2-HA] Color match on slot={slot} but name unavailable", force_console=True)
                                    break
                        else:
                            if active_color != _log_state["last_color_unavail"]:
                                log_event(f"[CC2-HA] Active filament color unavailable ({active_color!r}) — filament type unknown", force_console=True)
                                _log_state["last_color_unavail"] = active_color
                    except Exception as _fe:
                        log_event(f"[CC2-HA] Filament color cross-ref error: {_fe}", force_console=True)

                # ── Step 2: fetch status/sensor entities ─────────────────────────
                for key, eid in _ha_cc2_entities.items():
                    if key.startswith("_") or key not in KEY_TRANSFORMS or key == "active_filament_type":
                        continue  # active_filament_type already resolved above
                    if key == "filename" and "filename" in results:
                        continue  # already fetched in step 1a
                    try:
                        r = requests.get(
                            f"{HA_BASE_URL}/api/states/{eid}",
                            headers={"Authorization": f"Bearer {HA_TOKEN}"},
                            timeout=3,
                        )
                        state_data = r.json()
                        state = state_data.get("state", "")
                        if state not in ("unknown", "unavailable", ""):
                            if key in ("chamber_temp", "nozzle_temp", "bed_temp"):
                                unit = state_data.get("attributes", {}).get("unit_of_measurement", "")
                                if unit == "°F":
                                    try:
                                        state = str(round((float(state) - 32) * 5 / 9, 1))
                                    except ValueError:
                                        pass
                            results[key] = state
                    except Exception:
                        pass

                return results

            raw = await loop.run_in_executor(None, fetch_all)
            # Apply filename-encoded chamber temp (priority 1) before status processing
            _fn_temp = raw.pop("_filename_chamber_temp", None)
            if _fn_temp and _fn_temp != current_data.get("chamber_setpoint", 0):
                current_data["chamber_setpoint"] = float(_fn_temp)
                current_data["cc2_pending_filament"] = f"FILENAME-{int(_fn_temp)}C"
                mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/soll", int(_fn_temp), retain=True)
                mqtt_client.publish(f"{MQTT_TOPIC_PREFIX}/slicer_target_temp", int(_fn_temp), retain=True)
            # Log raw print_status whenever it changes so we can debug unexpected values
            _raw_status = raw.get("print_status", "")
            if _raw_status and _raw_status != current_data.get("_last_ha_status_raw", ""):
                current_data["_last_ha_status_raw"] = _raw_status
                log_event(f"[CC2-HA] print_status changed → {_raw_status!r}", force_console=True)
            for key, state_val in raw.items():
                try:
                    val = KEY_TRANSFORMS[key](state_val)
                    _handle_cc2_data(key, val)
                except Exception as e:
                    log_event(f"[CC2-HA] Transform/handle error for {key}={state_val!r}: {e}", force_console=True)
        except Exception as e:
            log_event(f"[CC2-HA] Poll loop error: {e}", force_console=True)

        await asyncio.sleep(3)


async def main():
    global main_loop
    main_loop = asyncio.get_running_loop()
    asyncio.create_task(update_limits_from_ws())
    if CC2_HA_MODE:
        asyncio.create_task(ha_cc2_poller())
    if not CC2_ACTIVE:
        asyncio.create_task(slicer_auto_parser())

    print(f"\n🚀 Panda-Logic-Sync {PANDA_VERSION}\n")

    if CC2_ACTIVE:
        # CC2 mode: no Panda Touch in the loop — TLS emulation server not needed.
        # WS loop (update_limits_from_ws) and MQTT thread handle everything.
        log_event("[SERVER] CC2 mode — TLS server disabled (no Panda Touch)", force_console=True)
        await asyncio.Event().wait()
    else:
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(certfile=str(BASE_DIR / "cert.pem"), keyfile=str(BASE_DIR / "key.pem"))
        ssl_ctx.set_ciphers('DEFAULT@SECLEVEL=0:ALL')
        server = await asyncio.start_server(handle_panda, '0.0.0.0', 8883, ssl=ssl_ctx)
        log_event(f"[SERVER] TLS server started on 8883 (SECLEVEL=0)")
        async with server: await server.serve_forever()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n🛑 Stopp.")
