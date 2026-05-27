#!/bin/sh
# Generate panda_config.json and TLS certs from environment variables before starting Panda.py
if [ "$1" = "python3" ] && [ "$2" = "Panda.py" ]; then

    # Accept both Docker-specific names and the upstream panda_config.json names.
    PANDA_HOST_IP="${PANDA_HOST_IP:-$HOST_IP}"
    PANDA_SN="${PANDA_SN:-$PRINTER_SN}"
    PANDA_ACCESS_CODE="${PANDA_ACCESS_CODE:-$ACCESS_CODE}"
    PANDA_MQTT_TOPIC_PREFIX="${PANDA_MQTT_TOPIC_PREFIX:-$MQTT_TOPIC_PREFIX}"
    export PANDA_HOST_IP PANDA_SN PANDA_ACCESS_CODE PANDA_MQTT_TOPIC_PREFIX

    # Auto-detect host LAN IP reachable by the Panda printer if not explicitly set.
    if [ -z "$PANDA_HOST_IP" ] && [ -n "$PANDA_IP" ]; then
        PANDA_HOST_IP=$(ip route get "$PANDA_IP" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
        echo "Auto-detected PANDA_HOST_IP=$PANDA_HOST_IP"
    fi

    missing=""
    for var in PANDA_IP PANDA_SN PANDA_ACCESS_CODE HA_TOKEN; do
        eval "value=\${$var}"
        if [ -z "$value" ]; then
            missing="$missing $var"
        fi
    done
    if [ -n "$missing" ]; then
        echo "Missing required Panda environment variables:$missing" >&2
        exit 1
    fi

    # Generate TLS certs on first run if not already present
    if [ ! -f /app/certs/cert.pem ] || [ ! -f /app/certs/key.pem ]; then
        echo "Generating self-signed TLS certificates..."
        mkdir -p /app/certs
        python3 - <<'CERTEOF'
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import datetime

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"panda-breath")])
now = datetime.datetime.now(datetime.timezone.utc)
cert = (x509.CertificateBuilder()
    .subject_name(name).issuer_name(name)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now)
    .not_valid_after(now + datetime.timedelta(days=3650))
    .sign(key, hashes.SHA256()))
with open("/app/certs/key.pem", "wb") as f:
    f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
with open("/app/certs/cert.pem", "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))
CERTEOF
        echo "TLS certificates generated"
    fi

    # Copy certs to app root where Panda.py expects them
    cp /app/certs/cert.pem /app/cert.pem
    cp /app/certs/key.pem /app/key.pem

    python3 - <<PYEOF
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
    "CC2_TOPIC_PREFIX": os.environ.get("CC2_TOPIC_PREFIX", "cc2"),
}

with open("/app/panda_config.json", "w") as f:
    json.dump(config, f, indent=2)

print("Generated panda_config.json from environment variables")
PYEOF
fi

exec "$@"
