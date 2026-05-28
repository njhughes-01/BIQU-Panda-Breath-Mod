"""Generate panda_config.json from environment variables.

Called by docker-entrypoint.sh at container startup. Extracted here so it
can be tested directly without running the shell entrypoint.
"""
import json
import os


def generate_config(env=None):
    if env is None:
        env = os.environ
    return {
        "DEBUG": env.get("PANDA_DEBUG", "false").lower() == "true",
        "DEBUG_TO_FILE": False,
        "HYSTERESE": float(env.get("PANDA_HYSTERESE", "1.5")),
        "MIN_SWITCH_TIME": int(env.get("PANDA_MIN_SWITCH_TIME", "10")),
        "MQTT_BROKER": env.get("HA_MQTT_BROKER", ""),
        "MQTT_USER": env.get("HA_MQTT_USER", ""),
        "MQTT_PASS": env.get("HA_MQTT_PASS", ""),
        "MQTT_TOPIC_PREFIX": env.get("PANDA_MQTT_TOPIC_PREFIX", "panda_breath_mod"),
        "HOST_IP": env.get("PANDA_HOST_IP", ""),
        "PANDA_IP": env.get("PANDA_IP", ""),
        "PRINTER_SN": env.get("PANDA_SN", ""),
        "ACCESS_CODE": env.get("PANDA_ACCESS_CODE", ""),
        "HA_BED_TEMPERATURE_ENTITY": env.get("PANDA_HA_BED_ENTITY", "sensor.ks1c_bed_temperature"),
        "HA_BASE_URL": env.get("HA_BASE_URL", ""),
        "HA_TOKEN": env.get("HA_TOKEN", ""),
        "PRINTER_IP": env.get("PANDA_IP", ""),
        "CC2_IP": env.get("CC2_IP", ""),
        "CC2_TOPIC_PREFIX": env.get("CC2_TOPIC_PREFIX", "cc2"),
        "MQTT_PORT": int(env.get("HA_MQTT_PORT", 1883)),
    }


if __name__ == "__main__":
    config = generate_config()
    with open("/app/panda_config.json", "w") as f:
        json.dump(config, f, indent=2)
    print("Generated panda_config.json from environment variables")
