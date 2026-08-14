"""Tests for panda_web.py — HTTP handler endpoints."""
import sys, os, json, importlib.util, threading, http.client
from unittest.mock import MagicMock, patch

REPO = os.path.join(os.path.dirname(__file__), "..")


def load_web():
    with patch.dict(os.environ, {"HA_MQTT_BROKER": "dummy", "PANDA_MQTT_TOPIC_PREFIX": "panda_breath_mod"}):
        with patch("paho.mqtt.client.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            spec = importlib.util.spec_from_file_location(
                "panda_web", os.path.join(REPO, "panda_web.py")
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.mqtt_client = MagicMock()
            return mod


import pytest
from http.server import ThreadingHTTPServer


@pytest.fixture
def server():
    mod = load_web()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    t = threading.Thread(target=srv.serve_forever)
    t.daemon = True
    t.start()
    port = srv.server_address[1]
    yield mod, port
    srv.shutdown()


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, resp.getheader("content-type", ""), body


def post(port, path, payload):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(payload).encode()
    conn.request("POST", path, data, {"content-type": "application/json", "content-length": str(len(data))})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, json.loads(body)


# ── GET endpoints ─────────────────────────────────────────────────────────────

def test_index_200(server):
    mod, port = server
    status, ct, body = get(port, "/")
    assert status == 200
    assert "text/html" in ct
    assert b"Panda Breath Control" in body


def test_index_html_alias(server):
    mod, port = server
    status, _, body = get(port, "/index.html")
    assert status == 200
    assert b"Panda Breath Control" in body


def test_state_200_json(server):
    mod, port = server
    status, ct, body = get(port, "/api/state")
    assert status == 200
    assert "application/json" in ct
    data = json.loads(body)
    assert "mqtt_connected" in data
    assert "topics" in data
    assert "logs" in data
    assert "last_seen" in data


def test_state_mqtt_disconnected_default(server):
    mod, port = server
    _, _, body = get(port, "/api/state")
    data = json.loads(body)
    assert data["mqtt_connected"] is False


def test_unknown_path_404(server):
    mod, port = server
    status, _, _ = get(port, "/api/nonexistent")
    assert status == 404


# ── State update reflection ───────────────────────────────────────────────────

def test_state_reflects_topic_update(server):
    mod, port = server
    mod.update_topic("panda_breath_mod/ist", "42.5")
    _, _, body = get(port, "/api/state")
    data = json.loads(body)
    assert data["topics"].get("ist") == "42.5"


def test_log_appended(server):
    mod, port = server
    mod.update_topic("panda_breath_mod/log", "test message")
    _, _, body = get(port, "/api/state")
    data = json.loads(body)
    assert "test message" in data["logs"]


# ── POST /api/command ─────────────────────────────────────────────────────────

def test_known_command_publishes(server):
    mod, port = server
    status, body = post(port, "/api/command", {"command": "auto"})
    assert status == 200
    assert body["ok"] is True
    mod.mqtt_client.publish.assert_called()
    topic = mod.mqtt_client.publish.call_args[0][0]
    payload = mod.mqtt_client.publish.call_args[0][1]
    assert "auto/set" in topic
    assert payload == "PRESS"


def test_power_off_command(server):
    mod, port = server
    post(port, "/api/command", {"command": "power_off"})
    topic = mod.mqtt_client.publish.call_args[0][0]
    payload = mod.mqtt_client.publish.call_args[0][1]
    assert "panda_power/set" in topic
    assert payload == "OFF"


def test_number_command_with_value(server):
    mod, port = server
    post(port, "/api/command", {"command": "soll", "value": "35"})
    topic = mod.mqtt_client.publish.call_args[0][0]
    payload = mod.mqtt_client.publish.call_args[0][1]
    assert "soll/set" in topic
    assert payload == "35"


def test_unknown_command_returns_400(server):
    mod, port = server
    status, body = post(port, "/api/command", {"command": "bogus_command"})
    assert status == 400
    assert "error" in body
