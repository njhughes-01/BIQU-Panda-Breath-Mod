"""Tests for cc2_connector.py — deep_merge, publish_to_ha, message parsing."""
import sys, os, json, importlib.util
from unittest.mock import MagicMock, patch, call

REPO = os.path.join(os.path.dirname(__file__), "..")


def load_cc2(cc2_ip="1.2.3.4", cc2_sn="TESTSN"):
    env_patch = {"CC2_IP": cc2_ip, "CC2_SN": cc2_sn, "HA_MQTT_BROKER": ""}
    with patch.dict(os.environ, env_patch):
        with patch("paho.mqtt.client.Client") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.is_connected.return_value = True
            mock_cls.return_value = mock_instance
            spec = importlib.util.spec_from_file_location(
                "cc2_connector", os.path.join(REPO, "cc2_connector.py")
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.ha_client = MagicMock()
            mod.ha_client.is_connected.return_value = True
            mod._last_published.clear()
            mod.printer_state.clear()
            return mod


# ── deep_merge ──────────────────────────────────────────────────────────────

def test_basic_merge():
    mod = load_cc2()
    result = mod.deep_merge({"a": 1}, {"b": 2})
    assert result == {"a": 1, "b": 2}


def test_override():
    mod = load_cc2()
    result = mod.deep_merge({"a": 1}, {"a": 2})
    assert result == {"a": 2}


def test_nested_merge():
    mod = load_cc2()
    result = mod.deep_merge({"a": {"x": 1}}, {"a": {"y": 2}})
    assert result == {"a": {"x": 1, "y": 2}}


def test_nested_override():
    mod = load_cc2()
    result = mod.deep_merge({"a": {"x": 1}}, {"a": {"x": 99}})
    assert result == {"a": {"x": 99}}


def test_flat_replaces_dict():
    mod = load_cc2()
    result = mod.deep_merge({"a": {"x": 1}}, {"a": "flat"})
    assert result == {"a": "flat"}


def test_mutates_base():
    mod = load_cc2()
    base = {"a": 1}
    result = mod.deep_merge(base, {"b": 2})
    assert base is result  # same object, mutated in place


# ── publish_to_ha ────────────────────────────────────────────────────────────

def test_publish_full_state():
    mod = load_cc2()
    mod.printer_state.update({
        "extruder": {"temperature": 220.5, "target": 210.0},
        "heater_bed": {"temperature": 60.0, "target": 65.0},
        "ztemperature_sensor": {"temperature": 35.0},
        "print_stats": {"state": "printing", "progress": 0.42},
    })
    mod.publish_to_ha()
    published = {c.args[0]: c.args[1] for c in mod.ha_client.publish.call_args_list}
    assert published["cc2/nozzle_temp"] == "220.5"
    assert published["cc2/bed_temp"] == "60.0"
    assert published["cc2/chamber_temp"] == "35.0"
    assert published["cc2/print_progress"] == "42"
    assert published["cc2/print_status"] == "printing"


def test_progress_fraction_normalized():
    mod = load_cc2()
    mod.printer_state["print_stats"] = {"progress": 0.75}
    mod.publish_to_ha()
    published = {c.args[0]: c.args[1] for c in mod.ha_client.publish.call_args_list}
    assert published["cc2/print_progress"] == "75"


def test_progress_already_percent():
    mod = load_cc2()
    mod.printer_state["print_stats"] = {"progress": 50}
    mod.publish_to_ha()
    published = {c.args[0]: c.args[1] for c in mod.ha_client.publish.call_args_list}
    assert published["cc2/print_progress"] == "50"


def test_publish_skips_unchanged():
    mod = load_cc2()
    mod.printer_state["extruder"] = {"temperature": 200.0}
    mod.publish_to_ha()
    count_first = mod.ha_client.publish.call_count
    mod.publish_to_ha()
    # second call should not publish anything new
    assert mod.ha_client.publish.call_count == count_first


def test_publish_skips_when_disconnected():
    mod = load_cc2()
    mod.ha_client.is_connected.return_value = False
    mod.printer_state["extruder"] = {"temperature": 200.0}
    mod.publish_to_ha()
    mod.ha_client.publish.assert_not_called()


def test_publish_with_none_ha_client():
    mod = load_cc2()
    mod.ha_client = None
    mod.printer_state["extruder"] = {"temperature": 200.0}
    mod.publish_to_ha()  # must not raise


def test_filament_from_filename_supports_cc2_material_aliases():
    mod = load_cc2()

    cases = {
        "bracket_PETG-CF_0.2mm.gcode": "PETG-CF",
        "duct_PAHT_CF_0.2mm.gcode": "PAHT-CF",
        "clip_PC-FR_0.2mm.gcode": "PC-FR",
        "cover_PET_0.2mm.gcode": "PET",
        "hinge_NYLON_0.2mm.gcode": "NYLON",
    }
    for filename, expected in cases.items():
        assert mod._filament_from_filename(filename) == expected


# ── cc2_on_message parsing paths ─────────────────────────────────────────────

def _make_msg(topic, payload_dict):
    msg = MagicMock()
    msg.topic = topic
    msg.payload = json.dumps(payload_dict).encode()
    return msg


def test_api_status_path():
    mod = load_cc2()
    msg = _make_msg(
        f"elegoo/TESTSN/{mod.client_id}/api_status" if hasattr(mod, "client_id") else "elegoo/TESTSN/x/api_status",
        {"status": {"extruder": {"temperature": 200.0}}},
    )
    mod.cc2_on_message(None, None, msg)
    assert mod.printer_state.get("extruder", {}).get("temperature") == 200.0


def test_jsonrpc_result_path():
    mod = load_cc2()
    msg = _make_msg(
        "elegoo/TESTSN/x/api_response",
        {"result": {"status": {"heater_bed": {"temperature": 55.0}}}},
    )
    mod.cc2_on_message(None, None, msg)
    assert mod.printer_state.get("heater_bed", {}).get("temperature") == 55.0


def test_params_notification_path():
    mod = load_cc2()
    msg = _make_msg(
        "elegoo/TESTSN/x/api_response",
        {"params": [{"status": {"print_stats": {"state": "printing"}}}]},
    )
    mod.cc2_on_message(None, None, msg)
    assert mod.printer_state.get("print_stats", {}).get("state") == "printing"


def test_invalid_json_no_crash():
    mod = load_cc2()
    msg = MagicMock()
    msg.topic = "elegoo/TESTSN/x/api_status"
    msg.payload = b"not valid json"
    mod.cc2_on_message(None, None, msg)  # must not raise


def test_non_dict_payload_no_crash():
    mod = load_cc2()
    msg = MagicMock()
    msg.topic = "elegoo/TESTSN/x/api_status"
    msg.payload = json.dumps([1, 2, 3]).encode()
    mod.cc2_on_message(None, None, msg)  # must not raise
