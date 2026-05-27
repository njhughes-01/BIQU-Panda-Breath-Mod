#!/bin/sh
# Generate panda_config.json from environment variables before starting Panda.py
if [ "$1" = "python3" ] && [ "$2" = "Panda.py" ]; then
    python3 - <<'PYEOF'
import json, os

config = {
    "DEBUG": os.environ.get("PANDA_DEBUG", "false").lower() == "true",
    "DEBUG_TO_FILE": False,
    "HYSTERESE": float(os.environ.get("PANDA_HYSTERESE", "1.5")),
    "MIN_SWITCH_TIME": int(os.environ.get("PANDA_MIN_SWITCH_TIME", "10")),
    "MQTT_BROKER": os.environ.get("HA_MQTT_BROKER", ""),
    "MQTT_USER": os.environ.get("HA_MQTT_USER", ""),
    "MQTT_PASS": os.environ.get("HA_MQTT_PASS", ""),
    "MQTT_TOPIC_PREFIX": os.environ.get("PANDA_MQTT_TOPIC_PREFIX", "panda_breath_mod"),
    "HOST_IP": os.environ.get("PANDA_HOST_IP", ""),
    "PANDA_IP": os.environ.get("PANDA_IP", ""),
    "PRINTER_SN": os.environ.get("PANDA_SN", ""),
    "ACCESS_CODE": os.environ.get("PANDA_ACCESS_CODE", ""),
    "HA_BED_TEMPERATURE_ENTITY": os.environ.get("PANDA_HA_BED_ENTITY", "sensor.ks1c_bed_temperature"),
    "HA_BASE_URL": os.environ.get("HA_BASE_URL", ""),
    "HA_TOKEN": os.environ.get("HA_TOKEN", ""),
    "PRINTER_IP": os.environ.get("PANDA_IP", ""),
}

with open("/app/panda_config.json", "w") as f:
    json.dump(config, f, indent=2)

print("Generated panda_config.json from environment variables")
PYEOF
fi

exec "$@"
