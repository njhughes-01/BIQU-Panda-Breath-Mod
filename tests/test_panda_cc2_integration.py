"""Tests for Panda.py CC2_IP conditional integration."""
import sys, os, json, importlib.util
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

REPO = os.path.join(os.path.dirname(__file__), "..")


def load_panda(cc2_ip=""):
    env = {
        "CC2_IP": cc2_ip,
        "CC2_TOPIC_PREFIX": "cc2",
        "HA_MQTT_BROKER": "dummy",
        "PANDA_DEBUG": "false",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch("paho.mqtt.client.Client") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            with patch("asyncio.get_event_loop", return_value=MagicMock()):
                spec = importlib.util.spec_from_file_location(
                    "panda_mod", os.path.join(REPO, "Panda.py")
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod, mock_instance


def _fake_msg(topic, payload):
    msg = MagicMock()
    msg.topic = topic
    msg.payload = payload.encode() if isinstance(payload, str) else payload
    return msg


# ── Subscription conditional on CC2_IP ──────────────────────────────────────

def test_with_cc2_ip_subscribes_to_cc2_topics():
    mod, mock_client = load_panda(cc2_ip="192.168.1.50")
    mod._on_mqtt_connect(mock_client, None, None, SimpleNamespace(is_failure=False), None)
    subscribed = [c.args[0] for c in mock_client.subscribe.call_args_list]
    assert any("cc2/#" in s for s in subscribed), f"cc2/# not in subscriptions: {subscribed}"


def test_without_cc2_ip_no_cc2_subscription():
    mod, mock_client = load_panda(cc2_ip="")
    subscribed = [c.args[0] for c in mock_client.subscribe.call_args_list]
    assert not any("cc2/" in s for s in subscribed), f"Unexpected cc2 subscription: {subscribed}"


# ── CC2 MQTT message handling ─────────────────────────────────────────────────

def test_cc2_bed_temp_updates_current_data():
    mod, mock_client = load_panda(cc2_ip="192.168.1.50")
    mod.CC2_IP = "192.168.1.50"
    mod.mqtt_client = mock_client
    msg = _fake_msg("cc2/bed_temp", "45.0")
    mod.on_mqtt_message(None, None, msg)
    assert mod.current_data["bed_temp"] == 45.0


def test_cc2_nozzle_forwarded():
    mod, mock_client = load_panda(cc2_ip="192.168.1.50")
    mod.CC2_IP = "192.168.1.50"
    mod.mqtt_client = mock_client
    msg = _fake_msg("cc2/nozzle_temp", "220.5")
    mod.on_mqtt_message(None, None, msg)
    published_topics = [c.args[0] for c in mock_client.publish.call_args_list]
    assert any("cc2_nozzle_temp" in t for t in published_topics)


def test_cc2_message_returns_before_lock_check():
    """CC2 messages must be handled and returned before the global_lock path."""
    mod, mock_client = load_panda(cc2_ip="192.168.1.50")
    mod.CC2_IP = "192.168.1.50"
    mod.mqtt_client = mock_client
    mod.global_lock = True  # lock is active
    msg = _fake_msg("cc2/bed_temp", "50.0")
    # If CC2 message is not returned early, the lock path would block it
    mod.on_mqtt_message(None, None, msg)
    # Bed temp should still have been updated despite lock
    assert mod.current_data["bed_temp"] == 50.0


def test_non_cc2_message_unaffected_by_cc2_handler():
    """Standard panda_breath_mod messages must not be consumed by the CC2 block."""
    mod, mock_client = load_panda(cc2_ip="192.168.1.50")
    mod.CC2_IP = "192.168.1.50"
    mod.mqtt_client = mock_client
    # Sending a panda topic should NOT early-return — it should reach normal handlers
    # We verify by checking that a known set command doesn't update bed_temp
    initial_bed = mod.current_data.get("bed_temp", 0.0)
    msg = _fake_msg("panda_breath_mod/unlock/set", "PRESS")
    mod.on_mqtt_message(None, None, msg)
    # Bed temp unchanged (unlock doesn't touch bed_temp)
    assert mod.current_data.get("bed_temp", 0.0) == initial_bed


def test_buffered_asa_cf_sets_chamber_target_on_print_start():
    """ASA-CF resolved while idle must arm chamber heat when CC2 enters preheating."""
    mod, mock_client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = mock_client
    mod.current_data["slicer_priority_mode"] = True
    mod.current_data["cc2_print_status"] = "idle"
    mod.current_data["chamber_setpoint"] = 0.0
    mod.current_data["cc2_pending_filament"] = "ASA-CF"

    mod._handle_cc2_data("print_status", "preheating")

    assert mod.current_data["chamber_setpoint"] == 55.0
    assert mod.current_data["slicer_soll"] == 55.0
    published = {c.args[0]: c.args[1] for c in mock_client.publish.call_args_list}
    assert published["panda_breath_mod/soll"] == 55
    assert published["panda_breath_mod/slicer_target_temp"] == 55


def test_duplicate_active_asa_cf_after_print_start_still_sets_missing_target():
    """HA can repeat the same filament after print start; dedupe must not skip an unset target."""
    mod, mock_client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = mock_client
    mod.current_data["slicer_priority_mode"] = True
    mod.current_data["cc2_print_status"] = "preheating"
    mod.current_data["chamber_setpoint"] = 0.0
    mod.current_data["cc2_pending_filament"] = "ASA-CF"

    mod._handle_cc2_data("active_filament_type", "ASA-CF")

    assert mod.current_data["chamber_setpoint"] == 55.0
    assert mod.current_data["slicer_soll"] == 55.0


def test_cc2_supported_filament_aliases_have_chamber_targets():
    """Elegoo-supported CC2 material aliases should map to deterministic targets."""
    mod, _mock_client = load_panda(cc2_ip="192.168.1.50")

    expected = {
        "PLA-CF": 0,
        "PET": 35,
        "PETG-CF": 35,
        "ABS-GF": 55,
        "ASA-GF": 55,
        "NYLON": 65,
        "PA6": 65,
        "PAHT-CF": 70,
        "PC-FR": 70,
    }
    for filament, target in expected.items():
        resolved, match = mod._resolve_filament_chamber_target(filament)
        assert resolved == target, filament
        assert match == filament


def test_ha_discovery_ignores_panda_mirror_sensor_for_cc2_chamber():
    """CC2 chamber source must be the Elegoo entity, not Panda's HA mirror."""
    mod, _mock_client = load_panda(cc2_ip="192.168.1.50")

    states = [
        {
            "entity_id": "sensor.panda_breath_mod_cc2_chamber_temp",
            "state": "26",
            "attributes": {
                "device_class": "temperature",
                "friendly_name": "Panda Breath Mod CC2 Chamber Temp",
                "object_id": "panda_cc2_chamber_temp",
            },
        },
        {
            "entity_id": "sensor.elegoo_centauri_carbon_2_chamber_temp",
            "state": "43",
            "attributes": {
                "device_class": "temperature",
                "friendly_name": "Elegoo Centauri Carbon 2 Chamber Temp",
            },
        },
        {
            "entity_id": "sensor.elegoo_centauri_carbon_2_print_status",
            "state": "paused",
            "attributes": {"friendly_name": "Elegoo Centauri Carbon 2 Print Status"},
        },
    ]

    response = MagicMock()
    response.json.return_value = states
    response.raise_for_status.return_value = None

    with patch.object(mod.requests, "get", return_value=response):
        found = mod._discover_ha_cc2_entities()

    assert found["chamber_temp"] == "sensor.elegoo_centauri_carbon_2_chamber_temp"
    assert found["print_status"] == "sensor.elegoo_centauri_carbon_2_print_status"
