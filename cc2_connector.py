#!/usr/bin/env python3
"""
CC2 MQTT Connector for Elegoo Centauri Carbon 2
Bridges printer sensor data to Home Assistant via MQTT.
"""

import os
import json
import time
import random
import threading
import logging
import sys
from typing import Optional, Dict, Any
from paho.mqtt.client import Client, MQTTMessageInfo
from paho.mqtt.enums import CallbackAPIVersion

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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


def generate_client_id() -> str:
    """Generate client ID: '0cli' + 5 hex timestamp digits + 3 hex random, truncated to 10 chars."""
    ts_hex = format(int(time.time() * 1000), "x")[-5:]
    rand_hex = format(random.randint(0, 4095), "x")
    cid = f"0cli{ts_hex}{rand_hex}"
    return cid[:10]


def generate_request_id() -> str:
    """Generate request ID matching the web interface format: 16 random hex chars + full hex timestamp."""
    rand_hex = ''.join(
        format(random.randint(0, 15) if c == 'x' else (random.randint(0, 3) + 8), 'x')
        for c in 'xxxxxxxxxxxxxxxx'
    )
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


def publish_to_ha() -> None:
    """Publish current printer state to Home Assistant MQTT sensors."""
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

            print_stats = printer_state.get("print_stats") or {}
            print_status = print_stats.get("state") or "idle"
            print_progress = print_stats.get("progress")
            if print_progress is None:
                print_progress = 0

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
        }

        for topic, payload in payload_map.items():
            ha_client.publish(topic, payload, qos=1, retain=False)
            logger.debug(f"Published {topic} = {payload}")

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
            "device_class": None,
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
            "device_class": None,
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

        # Handle registration response
        if "register_response" in msg.topic:
            if payload.get("error") == "ok":
                logger.info("Registration successful, requesting full status")
                # Request full status
                req_payload = json.dumps({"id": 1, "method": 1002})
                client.publish(f"elegoo/{CC2_SN}/{client_id}/api_request", req_payload, qos=1)
            else:
                logger.error(f"Registration failed: {payload}")

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
                if isinstance(params, list) and params:
                    status_data = params[0].get("status", params[0])
            if status_data and isinstance(status_data, dict):
                with printer_state_lock:
                    deep_merge(printer_state, status_data)
                logger.debug(f"Updated printer state: {printer_state}")
                publish_to_ha()

    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode CC2 message: {e}")
    except Exception as e:
        logger.error(f"Error processing CC2 message: {e}")


def cc2_on_disconnect(client: Client, userdata: Any, disconnect_flags: Any, rc: int, properties: Any = None) -> None:
    """CC2 MQTT disconnection callback."""
    if rc != 0:
        logger.warning(f"CC2 disconnected with code {rc}, will reconnect...")


def ha_on_connect(client: Client, userdata: Any, connect_flags: Any, rc: int, properties: Any = None) -> None:
    """Home Assistant MQTT connection callback."""
    if rc == 0:
        logger.info("Connected to HA MQTT broker")
        # Publish availability and autodiscovery
        client.publish(f"{CC2_TOPIC_PREFIX}/status", "online", qos=1, retain=True)
        publish_ha_autodiscovery()
    else:
        logger.error(f"HA connection failed with code {rc}")


def ha_on_disconnect(client: Client, userdata: Any, disconnect_flags: Any, rc: int, properties: Any = None) -> None:
    """Home Assistant MQTT disconnection callback."""
    if rc != 0:
        logger.warning(f"HA disconnected with code {rc}, will reconnect...")


def heartbeat_thread() -> None:
    """Send PING heartbeat to CC2 every 10 seconds."""
    while True:
        try:
            time.sleep(10)
            if cc2_client and cc2_client.is_connected():
                payload = json.dumps({"type": "PING"})
                cc2_client.publish(f"elegoo/{CC2_SN}/{client_id}/api_request", payload, qos=1)
                logger.debug("Sent PING heartbeat")
        except Exception as e:
            logger.error(f"Heartbeat thread error: {e}")


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
        ha_client.will_set(f"{CC2_TOPIC_PREFIX}/status", "offline", qos=1, retain=True)
        ha_client.reconnect_delay_set(min_delay=10, max_delay=120)

        try:
            logger.info(f"Connecting to HA MQTT broker at {HA_MQTT_BROKER}:{HA_MQTT_PORT}")
            ha_client.connect_async(HA_MQTT_BROKER, HA_MQTT_PORT, keepalive=60)
            ha_client.loop_start()
        except Exception as e:
            logger.error(f"Failed to initialize HA MQTT client: {e}")
            ha_client = None
    else:
        logger.warning("HA_MQTT_BROKER not set, skipping HA integration")

    # Connect to CC2
    try:
        cc2_client.reconnect_delay_set(min_delay=5, max_delay=60)
        logger.info(f"Connecting to CC2 MQTT broker at {CC2_IP}:1883")
        cc2_client.connect(CC2_IP, 1883, keepalive=60)
    except Exception as e:
        logger.error(f"Failed to connect to CC2 MQTT broker: {e}")
        sys.exit(1)

    # Start heartbeat thread
    hb_thread = threading.Thread(target=heartbeat_thread, daemon=True)
    hb_thread.start()
    logger.info("Heartbeat thread started")

    # Main loop
    try:
        logger.info("Starting CC2 MQTT loop")
        cc2_client.loop_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        if ha_client:
            ha_client.publish(f"{CC2_TOPIC_PREFIX}/status", "offline", qos=1, retain=True)
            ha_client.loop_stop()
        cc2_client.loop_stop()
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
