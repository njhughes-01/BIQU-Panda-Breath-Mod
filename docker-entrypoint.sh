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
    # NOTE: In standard Docker bridge networking, ip route returns the *container's* bridge IP
    # (e.g. 172.17.0.x), which the physical Panda printer cannot reach from the LAN.
    # Set PANDA_HOST_IP explicitly in .env to your Docker host's LAN IP (e.g. 192.168.1.x).
    # Port 8883 is mapped on the host, so the host IP is always the correct value.
    if [ -z "$PANDA_HOST_IP" ] && [ -n "$PANDA_IP" ]; then
        _detected=$(ip route get "$PANDA_IP" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
        case "$_detected" in
            172.1[6-9].*|172.2[0-9].*|172.3[01].*)
                echo "WARNING: Auto-detected IP $_detected is a Docker bridge address — the Panda printer cannot reach it from the LAN."
                echo "Set PANDA_HOST_IP=<your-host-LAN-IP> in .env (e.g. 192.168.1.x). Port 8883 is already mapped on the host."
                ;;
            "")
                echo "WARNING: Could not auto-detect PANDA_HOST_IP. Set it explicitly in .env to your Docker host's LAN IP."
                ;;
            *)
                PANDA_HOST_IP="$_detected"
                echo "Auto-detected PANDA_HOST_IP=$PANDA_HOST_IP"
                ;;
        esac
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

    python3 -c "
import json, os, sys
sys.path.insert(0, '/app')
from generate_config import generate_config
with open('/app/panda_config.json', 'w') as f:
    json.dump(generate_config(os.environ), f, indent=2)
print('Generated panda_config.json from environment variables')
"
fi

exec "$@"
