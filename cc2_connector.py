#!/usr/bin/env python3
"""
CC2 MQTT Connector for Elegoo Centauri Carbon 2
Bridges printer sensor data to Home Assistant via MQTT.
"""

import os
import json
import time
import random
import socket
import threading
import logging
import sys
from typing import Optional, Dict, Any, List
from paho.mqtt.client import Client
from paho.mqtt.enums import CallbackAPIVersion

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("CC2Connector")

# Configuration from environment variables
CC2_IP = os.environ.get("CC2_IP", "")
CC2_USER = os.environ.get("CC2_USER", "elegoo")
CC2_PASS = os.environ.get("CC2_PASS", "123456")
CC2_SN = os.environ.get("CC2_SN", "")
CC2_TOPIC_PREFIX = os.environ.get("CC2_TOPIC_PREFIX", "cc2")
HA_MQTT_BROKER = os.environ.get("HA_MQTT_BROKER", "")
HA_MQTT_PORT_ENV = os.environ.get("HA_MQTT_PORT", "1883")
HA_MQTT_PORT = int(HA_MQTT_PORT_ENV) if HA_MQTT_PORT_ENV.isdigit() else 1883
HA_MQTT_USER = os.environ.get("HA_MQTT_USER", "")
HA_MQTT_PASS = os.environ.get("HA_MQTT_PASS", "")

# Global state
printer_state: Dict[str, Any] = {}
printer_state_lock = threading.Lock()

# MQTT clients
cc2_client: Optional[Client] = None
ha_client: Optional[Client] = None

# Client identifiers
client_id: str = ""
request_id: str = ""
_request_counter: int = 100

# State cache for publish_to_ha — only publish changed values
_last_published: Dict[str, str] = {}

# Track print state to detect transitions
_last_print_state: str = ""

# AMS canvas state
_canvas_info: Dict[str, Any] = {}

# CC2 machine_status.status numeric codes (from elegoo-homeassistant cc2/const.py)
_MACHINE_STATUS_NAMES: Dict[int, str] = {
    0:  "initializing",
    1:  "idle",
    2:  "printing",
    3:  "filament_loading",
    4:  "filament_loading",
    5:  "leveling",
    6:  "calibrating",
    7:  "resonance_testing",
    8:  "self_checking",
    9:  "updating",
    10: "homing",
    11: "file_transferring",
    12: "composing",
    13: "extruder_operating",
    14: "error",
    15: "recovering",
}

# Sub-status codes when machine_status.status == 2 (printing)
_SUB_STATUS_NAMES: Dict[int, str] = {
    1045: "preheating", 1096: "preheating",
    1405: "preheating", 1906: "preheating",
    2075: "printing",   2077: "complete",
    2501: "pausing",    2502: "paused",   2505: "paused",
    2401: "resuming",   2402: "resuming",
    2503: "stopping",   2504: "stopped",
    2801: "homing",     2802: "homing",
    2901: "leveling",   2902: "leveling",
}

# gcode_move.speed_mode values
_SPEED_MODE_NAMES: Dict[int, str] = {
    0: "Silent",
    1: "Balanced",
    2: "Sport",
    3: "Ludicrous",
}

# Cached file list from CC2
_file_list: List[str] = []
# Last published filament type — prevents re-publishing on every publish_to_ha tick
_last_filament_type: str = ""
# Lock for all auxiliary shared state (_canvas_info, _file_list, _last_print_state, _last_filament_type)
# publish_to_ha is called from both the cc2 thread and ha thread; this prevents concurrent mutations.
_cc2_state_lock = threading.Lock()


def generate_client_id() -> str:
    """Generate client ID: '0cli' + 5 hex timestamp digits + 3 hex random, truncated to 10 chars."""
    ts_hex = format(int(time.time() * 1000), "x")[-5:]
    rand_hex = format(random.randint(0, 4095), "x")
    cid = f"0cli{ts_hex}{rand_hex}"
    return cid[:10]


def generate_request_id() -> str:
    """Generate request ID matching the web interface format: 16 random hex chars + full hex timestamp."""
    rand_hex = f"{random.getrandbits(64):016x}"
    ts_hex = format(int(time.time() * 1000), "x")
    return rand_hex + ts_hex


def deep_merge(base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge update dict into base dict, modifying base in place."""
    for key, value in update.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def send_cc2_command(method: int, params: Optional[Dict[str, Any]] = None) -> None:
    global _request_counter
    if not cc2_client or not cc2_client.is_connected():
        logger.warning(f"CC2 not connected, cannot send method {method}")
        return
    msg: Dict[str, Any] = {"id": _request_counter, "method": method}
    if params is not None:
        msg["params"] = params
    cc2_client.publish(f"elegoo/{CC2_SN}/{client_id}/api_request", json.dumps(msg), qos=1)
    logger.info(f"Sent CC2 command method={method} id={_request_counter}")
    _request_counter += 1


def request_canvas_info() -> None:
    send_cc2_command(2005)




def request_file_list() -> None:
    send_cc2_command(1044)


def _filament_from_nozzle_temp(target: float) -> str:
    """Best-guess filament type from nozzle target temperature."""
    if target <= 0:
        return ""
    if target <= 220:
        return "PLA"
    if target <= 245:
        return "PETG"
    if target <= 265:
        return "ABS"
    if target <= 280:
        return "PA"
    return "PC"


def _filament_from_filename(filename: str) -> str:
    """Extract filament type from filename if the slicer embedded it."""
    upper = filename.upper()
    for material in ("PA-CF", "PA12-CF", "PC-ABS", "PLA+", "ASA", "ABS", "PETG", "PA", "PC", "TPU", "TPE", "PLA"):
        if material.replace("-", "_") in upper or material in upper:
            return material
    return ""


def publish_active_filament(tray_id: int) -> None:
    global _last_filament_type
    if not ha_client or not ha_client.is_connected():
        return

    with _cc2_state_lock:
        canvas_snap = dict(_canvas_info)
        last_ft = _last_filament_type

    canvas = canvas_snap.get("canvas_list", [{}])[0] if canvas_snap else {}
    trays = canvas.get("tray_list", [])

    # Log raw tray data once so we can see the actual field names the CC2 uses
    if trays and not last_ft:
        logger.info(f"[CANVAS DEBUG] raw tray_list[0] keys: {list(trays[0].keys())}, data: {trays[0]}")

    filament_type = ""

    # 1. Try by active_tray_id index
    if 0 <= tray_id < len(trays):
        tray = trays[tray_id]
        filament_type = (tray.get("filament_type") or tray.get("material") or
                         tray.get("type") or tray.get("filament") or "")

    # 2. Filename-based detection — more reliable than tray scan when active_tray_id is unknown
    if not filament_type:
        current_file = printer_state.get("print_status", {}).get("filename", "")
        filament_type = _filament_from_filename(current_file)
        if filament_type:
            logger.info(f"Filament type from filename '{current_file}': {filament_type}")

    # 3. Scan all trays for any non-empty filament field (last-resort tray fallback)
    if not filament_type:
        for t in trays:
            ft = (t.get("filament_type") or t.get("material") or
                  t.get("type") or t.get("filament") or "")
            if ft:
                filament_type = ft
                logger.info(f"Filename unavailable, using first tray with filament: {filament_type}")
                break

    # 4. Nozzle target temp as last resort
    if not filament_type:
        nozzle_target = (printer_state.get("extruder") or {}).get("target", 0)
        filament_type = _filament_from_nozzle_temp(float(nozzle_target or 0))
        if filament_type:
            logger.info(f"Filament type inferred from nozzle target {nozzle_target}°C: {filament_type}")

    if filament_type and filament_type != last_ft:
        with _cc2_state_lock:
            if filament_type != _last_filament_type:  # re-check under lock
                _last_filament_type = filament_type
                ha_client.publish(f"{CC2_TOPIC_PREFIX}/active_filament_type", filament_type, qos=1, retain=True)
                logger.info(f"Active filament: tray {tray_id} = {filament_type}")


def publish_to_ha() -> None:
    """Publish current printer state to Home Assistant MQTT sensors."""
    global _last_print_state, _last_filament_type, _canvas_info
    if not ha_client or not ha_client.is_connected():
        logger.warning("HA MQTT client not connected, skipping publish")
        return

    try:
        with printer_state_lock:
            extruder = printer_state.get("extruder") or {}
            nozzle_temp = extruder.get("temperature")
            if nozzle_temp is None:
                nozzle_temp = 0
            nozzle_target = extruder.get("target")
            if nozzle_target is None:
                nozzle_target = 0

            heater_bed = printer_state.get("heater_bed") or {}
            bed_temp = heater_bed.get("temperature")
            if bed_temp is None:
                bed_temp = 0
            bed_target = heater_bed.get("target")
            if bed_target is None:
                bed_target = 0

            ztemp = printer_state.get("ztemperature_sensor") or {}
            chamber_temp = ztemp.get("temperature")
            if chamber_temp is None:
                chamber_temp = 0

            # CC2 uses "print_status" (dict) not "print_stats"
            print_status_obj = printer_state.get("print_status") or {}
            state_str = (print_status_obj.get("state") or "").lower().strip()
            machine_status_obj = printer_state.get("machine_status") or {}
            machine_code = int(machine_status_obj.get("status") or 0)
            machine_code_sub = int(machine_status_obj.get("sub_status") or 0)

            # Map machine_code + sub_status to a human-readable state string.
            # state_str from print_status.state takes priority when present.
            if state_str:
                print_status = state_str
            elif machine_code == 2:
                # Use sub_status for detailed printing state
                print_status = _SUB_STATUS_NAMES.get(machine_code_sub, "printing")
            else:
                print_status = _MACHINE_STATUS_NAMES.get(machine_code, "idle")

            # Progress lives in machine_status on the CC2
            print_progress = machine_status_obj.get("progress")
            if print_progress is None:
                print_progress = print_status_obj.get("progress") or 0
        with _cc2_state_lock:
            active_tray_id = (_canvas_info.get("active_tray_id", -1) if _canvas_info else -1)
            _file_list_snap = list(_file_list)
            _last_print_state_snap = _last_print_state

        # Extended fields
        filament_detected = "ON" if extruder.get("filament_detected") else "OFF"
        remaining_time = int(print_status_obj.get("remaining_time_sec") or 0)
        current_layer = int(print_status_obj.get("current_layer") or 0)
        filename = str(print_status_obj.get("filename") or "")

        # Prefer gcode_move_inf (CC2 firmware) over gcode_move
        gcode_move = printer_state.get("gcode_move_inf") or printer_state.get("gcode_move") or {}
        z_height = round(float(gcode_move.get("z") or 0), 2)

        fans_obj = printer_state.get("fans") or {}
        # CC2 fan speeds are 0-255; normalize to 0-100%
        raw_fan = float((fans_obj.get("fan") or {}).get("speed") or 0)
        fan_speed = int(round(raw_fan / 255 * 100))
        raw_box = float((fans_obj.get("box_fan") or {}).get("speed") or 0)
        box_fan_speed = int(round(raw_box / 255 * 100))

        led_obj = printer_state.get("led") or {}
        led_status = "ON" if led_obj.get("status") else "OFF"

        exception_status = machine_status_obj.get("exception_status") or []
        has_error = "ON" if exception_status else "OFF"

        speed_mode_code = int(gcode_move.get("speed_mode") or 1)
        speed_mode = _SPEED_MODE_NAMES.get(speed_mode_code, "Balanced")

        file_count = len(_file_list_snap)
        latest_filename = _file_list_snap[-1] if _file_list_snap else ""

        # Convert progress to percentage (0-100)
        if isinstance(print_progress, (int, float)) and 0 <= float(print_progress) <= 1:
            print_progress = int(float(print_progress) * 100)

        payload_map = {
            f"{CC2_TOPIC_PREFIX}/nozzle_temp": str(nozzle_temp),
            f"{CC2_TOPIC_PREFIX}/nozzle_target": str(nozzle_target),
            f"{CC2_TOPIC_PREFIX}/bed_temp": str(bed_temp),
            f"{CC2_TOPIC_PREFIX}/bed_target": str(bed_target),
            f"{CC2_TOPIC_PREFIX}/chamber_temp": str(chamber_temp),
            f"{CC2_TOPIC_PREFIX}/print_status": str(print_status),
            f"{CC2_TOPIC_PREFIX}/print_progress": str(print_progress),
            f"{CC2_TOPIC_PREFIX}/filament_detected": filament_detected,
            f"{CC2_TOPIC_PREFIX}/remaining_time": str(remaining_time),
            f"{CC2_TOPIC_PREFIX}/current_layer": str(current_layer),
            f"{CC2_TOPIC_PREFIX}/filename": filename,
            f"{CC2_TOPIC_PREFIX}/z_height": str(z_height),
            f"{CC2_TOPIC_PREFIX}/fan_speed": str(fan_speed),
            f"{CC2_TOPIC_PREFIX}/box_fan_speed": str(box_fan_speed),
            f"{CC2_TOPIC_PREFIX}/led": led_status,
            f"{CC2_TOPIC_PREFIX}/has_error": has_error,
            f"{CC2_TOPIC_PREFIX}/speed_mode": speed_mode,
            f"{CC2_TOPIC_PREFIX}/file_count": str(file_count),
            f"{CC2_TOPIC_PREFIX}/latest_filename": latest_filename,
        }

        for topic, payload in payload_map.items():
            if _last_published.get(topic) != payload:
                ha_client.publish(topic, payload, qos=1, retain=True)
                _last_published[topic] = payload
                logger.debug(f"Published {topic} = {payload}")

        # Detect print start → request fresh canvas info to get active tray
        # Include preheating — slicer priority should fire as soon as print job starts
        printing_states = {"printing", "preheating", "paused", "pausing", "resuming", "stopping"}
        current_state = str(print_status).lower()
        was_printing = _last_print_state_snap in printing_states
        is_printing = current_state in printing_states
        if current_state != _last_print_state_snap:
            logger.info(f"Print state: {_last_print_state_snap!r} → {current_state!r} (machine={machine_code}, sub={machine_code_sub})")
        if machine_code == 11 and _last_print_state_snap != "file_transferring":
            # File transfer just started — CC2 is loading the job; good time to check active tray
            logger.info("File transfer started, pre-fetching canvas info for tray assignment")
            request_canvas_info()
        if is_printing and not was_printing:
            logger.info(f"Print started (state={print_status}), requesting canvas info")
            request_canvas_info()
        elif not is_printing and was_printing:
            # Print ended — reset so next print re-triggers filament detection
            with _cc2_state_lock:
                _last_filament_type = ""
                _canvas_info = {}  # Clear stale tray data so next print gets a fresh canvas request
        elif is_printing and active_tray_id >= 0:
            publish_active_filament(active_tray_id)
        with _cc2_state_lock:
            _last_print_state = current_state

    except Exception as e:
        logger.error(f"Error publishing to HA: {e}")


def publish_ha_autodiscovery() -> None:
    """Publish Home Assistant MQTT autodiscovery configs (retained)."""
    if not ha_client or not ha_client.is_connected():
        logger.warning("HA MQTT client not connected, skipping autodiscovery")
        return

    device = {
        "identifiers": [CC2_SN],
        "name": "Centauri Carbon 2",
        "model": "CC2",
        "manufacturer": "Elegoo"
    }

    sensors = {
        "nozzle_temp": {
            "name": "Nozzle Temperature",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "icon": None
        },
        "nozzle_target": {
            "name": "Nozzle Target Temperature",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "icon": None
        },
        "bed_temp": {
            "name": "Bed Temperature",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "icon": None
        },
        "bed_target": {
            "name": "Bed Target Temperature",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "icon": None
        },
        "chamber_temp": {
            "name": "Chamber Temperature",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "icon": None
        },
        "print_status": {
            "name": "Print Status",
            "unit_of_measurement": None,
            "device_class": None,
            "state_class": None,
            "icon": "mdi:printer-3d"
        },
        "print_progress": {
            "name": "Print Progress",
            "unit_of_measurement": "%",
            "device_class": None,
            "state_class": "measurement",
            "icon": "mdi:progress-clock"
        },
        "remaining_time": {
            "name": "Remaining Time",
            "unit_of_measurement": "s",
            "device_class": "duration",
            "state_class": "measurement",
            "icon": "mdi:timer-outline"
        },
        "current_layer": {
            "name": "Current Layer",
            "unit_of_measurement": None,
            "device_class": None,
            "state_class": "measurement",
            "icon": "mdi:layers"
        },
        "filename": {
            "name": "Print Filename",
            "unit_of_measurement": None,
            "device_class": None,
            "state_class": None,
            "icon": "mdi:file-document-outline"
        },
        "z_height": {
            "name": "Z Height",
            "unit_of_measurement": "mm",
            "device_class": None,
            "state_class": "measurement",
            "icon": "mdi:axis-z-arrow"
        },
        "box_fan_speed": {
            "name": "Enclosure Fan",
            "unit_of_measurement": "%",
            "device_class": None,
            "state_class": "measurement",
            "icon": "mdi:fan"
        },
        "file_count": {
            "name": "Files on Printer",
            "unit_of_measurement": None,
            "device_class": None,
            "state_class": "measurement",
            "icon": "mdi:folder-multiple-outline"
        },
        "latest_filename": {
            "name": "Latest File",
            "unit_of_measurement": None,
            "device_class": None,
            "state_class": None,
            "icon": "mdi:file-document-outline"
        },
    }

    for sensor_id, config in sensors.items():
        uid = f"cc2_{sensor_id}"
        discovery_topic = f"homeassistant/sensor/{uid}/config"

        payload = {
            "unique_id": uid,
            "object_id": uid,
            "name": config["name"],
            "state_topic": f"{CC2_TOPIC_PREFIX}/{sensor_id}",
            "availability_topic": f"{CC2_TOPIC_PREFIX}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }

        if config["unit_of_measurement"]:
            payload["unit_of_measurement"] = config["unit_of_measurement"]
        if config["device_class"]:
            payload["device_class"] = config["device_class"]
        if config["state_class"]:
            payload["state_class"] = config["state_class"]
        if config["icon"]:
            payload["icon"] = config["icon"]

        ha_client.publish(discovery_topic, json.dumps(payload), qos=1, retain=True)
        logger.info(f"Published autodiscovery for {sensor_id}")

    # Remove old fan_speed sensor entity (replaced by number) and led binary_sensor (replaced by switch)
    ha_client.publish(f"homeassistant/sensor/cc2_fan_speed/config", "", qos=1, retain=True)
    ha_client.publish(f"homeassistant/binary_sensor/cc2_led/config", "", qos=1, retain=True)

    # Binary sensors (read-only)
    binary_sensors = [
        ("filament_detected", "Filament Detected", "mdi:printer-3d-nozzle", None),
        ("has_error",         "Printer Error",     "mdi:alert-circle",      "problem"),
    ]
    for sensor_id, name, icon, device_class in binary_sensors:
        uid = f"cc2_{sensor_id}"
        p: dict = {
            "unique_id": uid,
            "object_id": uid,
            "name": name,
            "state_topic": f"{CC2_TOPIC_PREFIX}/{sensor_id}",
            "availability_topic": f"{CC2_TOPIC_PREFIX}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": device,
            "icon": icon,
        }
        if device_class:
            p["device_class"] = device_class
        ha_client.publish(f"homeassistant/binary_sensor/{uid}/config",
                          json.dumps(p), qos=1, retain=True)
        logger.info(f"Published autodiscovery for binary {sensor_id}")

    # Switch: LED control
    ha_client.publish("homeassistant/switch/cc2_led/config", json.dumps({
        "unique_id": "cc2_led",
        "object_id": "cc2_led",
        "name": "LED",
        "state_topic": f"{CC2_TOPIC_PREFIX}/led",
        "command_topic": f"{CC2_TOPIC_PREFIX}/led_control/set",
        "payload_on": "ON",
        "payload_off": "OFF",
        "availability_topic": f"{CC2_TOPIC_PREFIX}/status",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
        "icon": "mdi:led-on",
    }), qos=1, retain=True)
    logger.info("Published autodiscovery for switch led")

    # Number: Part Cooling Fan speed (0-100%)
    ha_client.publish("homeassistant/number/cc2_fan_speed/config", json.dumps({
        "unique_id": "cc2_fan_speed",
        "object_id": "cc2_fan_speed",
        "name": "Part Cooling Fan",
        "state_topic": f"{CC2_TOPIC_PREFIX}/fan_speed",
        "command_topic": f"{CC2_TOPIC_PREFIX}/fan_speed_control/set",
        "min": 0, "max": 100, "step": 1,
        "unit_of_measurement": "%",
        "availability_topic": f"{CC2_TOPIC_PREFIX}/status",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
        "icon": "mdi:fan",
    }), qos=1, retain=True)
    logger.info("Published autodiscovery for number fan_speed")

    # Select: Print Speed Mode
    ha_client.publish("homeassistant/select/cc2_speed_mode/config", json.dumps({
        "unique_id": "cc2_speed_mode",
        "object_id": "cc2_speed_mode",
        "name": "Print Speed Mode",
        "state_topic": f"{CC2_TOPIC_PREFIX}/speed_mode",
        "command_topic": f"{CC2_TOPIC_PREFIX}/speed_mode/set",
        "options": ["Silent", "Balanced", "Sport", "Ludicrous"],
        "availability_topic": f"{CC2_TOPIC_PREFIX}/status",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
        "icon": "mdi:speedometer",
    }), qos=1, retain=True)
    logger.info("Published autodiscovery for select speed_mode")

    # Buttons: print control + file list refresh
    for btn_id, btn_name, btn_icon in [
        ("pause_print",       "Pause Print",        "mdi:pause"),
        ("resume_print",      "Resume Print",       "mdi:play"),
        ("stop_print",        "Stop Print",         "mdi:stop"),
        ("file_list_refresh", "Refresh File List",  "mdi:refresh"),
    ]:
        uid = f"cc2_{btn_id}"
        ha_client.publish(f"homeassistant/button/{uid}/config", json.dumps({
            "unique_id": uid,
            "object_id": uid,
            "name": btn_name,
            "command_topic": f"{CC2_TOPIC_PREFIX}/{btn_id}/press",
            "availability_topic": f"{CC2_TOPIC_PREFIX}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
            "icon": btn_icon,
        }), qos=1, retain=True)
        logger.info(f"Published autodiscovery for button {btn_id}")


def cc2_on_connect(client: Client, userdata: Any, connect_flags: Any, rc: int, properties: Any = None) -> None:
    """CC2 MQTT connection callback."""
    if rc == 0:
        logger.info("Connected to CC2 MQTT broker")
        # Subscribe to status updates
        client.subscribe(f"elegoo/{CC2_SN}/api_status", qos=1)
        client.subscribe(f"elegoo/{CC2_SN}/{request_id}/register_response", qos=1)
        client.subscribe(f"elegoo/{CC2_SN}/{client_id}/api_response", qos=1)

        # Send registration
        reg_payload = json.dumps({
            "client_id": client_id,
            "request_id": request_id
        })
        client.publish(f"elegoo/{CC2_SN}/api_register", reg_payload, qos=1)
        logger.info("Sent registration to CC2")
        if ha_client and ha_client.is_connected():
            ha_client.publish(f"{CC2_TOPIC_PREFIX}/status", "online", qos=1, retain=True)
            logger.info("Re-published CC2 online status to HA")
    else:
        logger.error(f"CC2 connection failed with code {rc}")


def cc2_on_message(client: Client, userdata: Any, msg: Any) -> None:
    """CC2 MQTT message callback."""
    try:
        payload = json.loads(msg.payload.decode())
        logger.debug(f"CC2 message on {msg.topic}: {payload}")
        if not isinstance(payload, dict):
            logger.warning(f"Unexpected non-dictionary payload received on {msg.topic}")
            return

        # Handle method-specific responses
        if "api_response" in msg.topic:
            method = payload.get("method")
            result = payload.get("result", {})

            if method == 2005:
                # Canvas info (AMS tray data)
                if isinstance(result, dict) and result.get("error_code") == 0:
                    canvas = result.get("canvas_info", {})
                    with _cc2_state_lock:
                        _canvas_info.update(canvas)
                    active_tray = canvas.get("active_tray_id", -1)
                    publish_active_filament(active_tray)
                    logger.info(f"Canvas info updated, active_tray_id={active_tray}")
                return

            elif method == 1044:
                # File list response
                global _file_list
                files = []
                if isinstance(result, dict):
                    raw = result.get("files") or result.get("file_list") or result.get("data") or []
                    if isinstance(raw, list):
                        files = [
                            f.get("filename") or f.get("name") or str(f)
                            for f in raw if isinstance(f, dict)
                        ]
                        if not files:
                            files = [str(f) for f in raw if isinstance(f, str)]
                with _cc2_state_lock:
                    _file_list = files
                logger.info(f"File list updated: {len(_file_list)} files")
                if _file_list:
                    logger.info(f"Files: {', '.join(_file_list[:10])}")
                publish_to_ha()
                return

            elif method in (1021, 1022, 1023, 1029, 1030, 1031):
                # Control command acknowledgement
                err = result.get("error_code") if isinstance(result, dict) else None
                if err == 0:
                    logger.info(f"CC2 command method={method} acknowledged OK")
                else:
                    logger.warning(f"CC2 command method={method} result: {result}")
                return

        # Handle registration response
        if "register_response" in msg.topic:
            if payload.get("error") == "ok":
                logger.info("Registration successful, requesting full status, canvas info, and file list")
                send_cc2_command(1002)
                send_cc2_command(2005)
                send_cc2_command(1044)
            else:
                logger.error(f"Registration failed: {payload} — retrying in 5s")
                threading.Timer(5.0, lambda: cc2_client.publish(
                    f"elegoo/{CC2_SN}/api_register",
                    json.dumps({"client_id": client_id, "request_id": request_id}),
                    qos=1
                )).start()

        # Handle full status response (method 1002 reply) and delta updates
        elif "api_response" in msg.topic or "api_status" in msg.topic:
            status_data = None
            if "result" in payload:
                # JSON-RPC response (method 1002): printer objects nested under "status"
                result = payload["result"]
                if isinstance(result, dict):
                    status_data = result.get("status") or result
            elif "status" in payload:
                # Direct status notification from api_status topic
                status_data = payload["status"]
            elif "params" in payload:
                # Unsolicited Klipper-style notification: params[0] is the status dict
                params = payload["params"]
                if isinstance(params, list) and params and isinstance(params[0], dict):
                    status_data = params[0].get("status", params[0])
            if status_data and isinstance(status_data, dict):
                with printer_state_lock:
                    deep_merge(printer_state, status_data)
                logger.debug(f"Updated printer state keys: {list(printer_state.keys())}")
                publish_to_ha()

    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode CC2 message: {e}")
    except Exception as e:
        logger.error(f"Error processing CC2 message: {e}")


def cc2_on_disconnect(client: Client, userdata: Any, disconnect_flags: Any, rc: int, properties: Any = None) -> None:
    """CC2 MQTT disconnection callback."""
    global _last_filament_type
    logger.warning(f"CC2 MQTT disconnected — rc={rc} (0=clean, non-zero=unexpected)")
    with _cc2_state_lock:
        _canvas_info.clear()
        _last_filament_type = ""
    if ha_client and ha_client.is_connected():
        ha_client.publish(f"{CC2_TOPIC_PREFIX}/status", "offline", qos=1, retain=True)
        logger.info("Marked CC2 unavailable in HA (printer MQTT disconnected)")


def ha_on_message(client: Client, userdata: Any, msg: Any) -> None:
    """Handle commands from Home Assistant."""
    global _file_list
    topic = msg.topic
    try:
        payload = msg.payload.decode().strip()
    except Exception:
        return

    if topic == f"{CC2_TOPIC_PREFIX}/pause_print/press":
        logger.info("HA: pause print")
        send_cc2_command(1021)

    elif topic == f"{CC2_TOPIC_PREFIX}/resume_print/press":
        logger.info("HA: resume print")
        send_cc2_command(1023)

    elif topic == f"{CC2_TOPIC_PREFIX}/stop_print/press":
        logger.info("HA: stop print")
        send_cc2_command(1022)

    elif topic == f"{CC2_TOPIC_PREFIX}/led_control/set":
        status = 1 if payload.upper() == "ON" else 0
        logger.info(f"HA: LED → {'ON' if status else 'OFF'}")
        send_cc2_command(1029, {"status": status})
        # Optimistically update state so HA switch reflects immediately and survives reconnect
        val = "ON" if status else "OFF"
        if ha_client and ha_client.is_connected():
            ha_client.publish(f"{CC2_TOPIC_PREFIX}/led", val, retain=True)

    elif topic == f"{CC2_TOPIC_PREFIX}/fan_speed_control/set":
        try:
            pct = max(0, min(100, int(float(payload))))
            raw = int(round(pct / 100 * 255))
            logger.info(f"HA: part cooling fan → {pct}% (raw={raw})")
            send_cc2_command(1030, {"fans": {"fan": {"speed": raw}}})
        except ValueError:
            logger.warning(f"Invalid fan speed value: {payload!r}")

    elif topic == f"{CC2_TOPIC_PREFIX}/speed_mode/set":
        mode_map = {"Silent": 0, "Balanced": 1, "Sport": 2, "Ludicrous": 3}
        mode_code = mode_map.get(payload)
        if mode_code is not None:
            logger.info(f"HA: speed mode → {payload} ({mode_code})")
            send_cc2_command(1031, {"speed_mode": mode_code})
        else:
            logger.warning(f"Unknown speed mode: {payload!r}")

    elif topic == f"{CC2_TOPIC_PREFIX}/file_list_refresh/press":
        logger.info("HA: refresh file list")
        request_file_list()


def _set_tcp_keepalive(client: Client) -> None:
    """Enable TCP SO_KEEPALIVE on the client socket to prevent NAT/firewall idle-timeout drops."""
    try:
        sock = client.socket()
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            logger.info("TCP keepalive set on HA MQTT socket (idle=10s interval=5s count=3)")
    except Exception as e:
        logger.warning(f"Could not set TCP keepalive: {e}")


def ha_on_connect(client: Client, userdata: Any, connect_flags: Any, rc: int, properties: Any = None) -> None:
    """Home Assistant MQTT connection callback."""
    if rc == 0:
        _set_tcp_keepalive(client)
        logger.info("Connected to HA MQTT broker")
        client.publish(f"{CC2_TOPIC_PREFIX}/status", "online", qos=1, retain=True)
        publish_ha_autodiscovery()
        # Subscribe to command topics from HA
        client.subscribe(f"{CC2_TOPIC_PREFIX}/+/set", qos=1)
        client.subscribe(f"{CC2_TOPIC_PREFIX}/+/press", qos=1)
        logger.info("Subscribed to HA command topics")
        # Force republish all state so HA gets current values after any reconnect
        _last_published.clear()
        publish_to_ha()
    else:
        logger.error(f"HA connection failed with code {rc}")


def ha_on_disconnect(client: Client, userdata: Any, disconnect_flags: Any, rc: int, properties: Any = None) -> None:
    """Home Assistant MQTT disconnection callback."""
    logger.warning(f"HA MQTT disconnected — rc={rc} (0=clean, non-zero=unexpected)")


def heartbeat_thread() -> None:
    """Send PING to CC2 every 10s."""
    while True:
        try:
            time.sleep(10)
            if cc2_client and cc2_client.is_connected():
                payload = json.dumps({"type": "PING"})
                cc2_client.publish(f"elegoo/{CC2_SN}/{client_id}/api_request", payload, qos=1)
                logger.debug("Sent CC2 PING heartbeat")
        except Exception as e:
            logger.error(f"Heartbeat thread error: {e}")


def publish_poll_thread() -> None:
    """Force-publish all current printer state to HA every 30 seconds.

    HA expects integrations to push a full state refresh on a regular interval,
    not only on change. When the printer is idle all CC2 sensor values are static
    so cc2_on_message stops calling publish_to_ha; clearing _last_published and
    re-publishing ensures HA entities always reflect current state and never go stale.
    """
    while True:
        try:
            time.sleep(30)
            if ha_client and ha_client.is_connected():
                _last_published.clear()
                publish_to_ha()
                logger.debug("Forced full state publish to HA (30s poll)")
        except Exception as e:
            logger.error(f"Publish poll thread error: {e}")


def main() -> None:
    """Main entry point."""
    global client_id, request_id, cc2_client, ha_client

    # Validate required config
    missing = [v for v in ("CC2_IP", "CC2_SN") if not os.environ.get(v)]
    if missing:
        logger.error(f"Required environment variables not set: {', '.join(missing)}")
        sys.exit(1)

    # Generate IDs
    client_id = generate_client_id()
    request_id = generate_request_id()
    logger.info(f"Generated client_id={client_id}, request_id={request_id}")

    # CC2 MQTT client
    cc2_client = Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True
    )
    cc2_client.username_pw_set(CC2_USER, CC2_PASS)
    cc2_client.on_connect = cc2_on_connect
    cc2_client.on_message = cc2_on_message
    cc2_client.on_disconnect = cc2_on_disconnect

    # Home Assistant MQTT client
    if HA_MQTT_BROKER:
        ha_client = Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=f"cc2_ha_{client_id}",
            clean_session=True
        )
        if HA_MQTT_USER:
            ha_client.username_pw_set(HA_MQTT_USER, HA_MQTT_PASS)
        ha_client.on_connect = ha_on_connect
        ha_client.on_disconnect = ha_on_disconnect
        ha_client.on_message = ha_on_message
        ha_client.will_set(f"{CC2_TOPIC_PREFIX}/status", "offline", qos=1, retain=True)
        ha_client.reconnect_delay_set(min_delay=2, max_delay=30)

        try:
            logger.info(f"Connecting to HA MQTT broker at {HA_MQTT_BROKER}:{HA_MQTT_PORT}")
            ha_client.connect_async(HA_MQTT_BROKER, HA_MQTT_PORT, keepalive=60)
            ha_thread = threading.Thread(target=ha_client.loop_forever, daemon=True, name="ha-mqtt-loop")
            ha_thread.start()
        except Exception as e:
            logger.error(f"Failed to initialize HA MQTT client: {e}")
            ha_client = None
    else:
        logger.warning("HA_MQTT_BROKER not set, skipping HA integration")

    # Connect to CC2
    try:
        cc2_client.reconnect_delay_set(min_delay=5, max_delay=60)
        logger.info(f"Connecting to CC2 MQTT broker at {CC2_IP}:1883")
        cc2_client.connect_async(CC2_IP, 1883, keepalive=60)
    except Exception as e:
        logger.error(f"Failed to initialize CC2 MQTT connection: {e}")
        sys.exit(1)

    # Start background threads
    hb_thread = threading.Thread(target=heartbeat_thread, daemon=True)
    hb_thread.start()
    poll_thread = threading.Thread(target=publish_poll_thread, daemon=True, name="ha-poll-loop")
    poll_thread.start()
    logger.info("Heartbeat and HA poll threads started")

    # Main loop
    try:
        logger.info("Starting CC2 MQTT loop")
        cc2_client.loop_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        if ha_client:
            try:
                result = ha_client.publish(f"{CC2_TOPIC_PREFIX}/status", "offline", qos=1, retain=True)
                result.wait_for_publish(timeout=2.0)
                ha_client.disconnect()
            except Exception:
                pass
            ha_client.loop_stop()
        cc2_client.loop_stop()
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
