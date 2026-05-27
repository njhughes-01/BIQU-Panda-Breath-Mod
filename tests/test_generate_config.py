"""Tests for generate_config.py — config generation from environment variables."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from generate_config import generate_config


def test_defaults():
    cfg = generate_config({})
    assert cfg["DEBUG"] is False
    assert cfg["DEBUG_TO_FILE"] is False
    assert cfg["HYSTERESE"] == 1.5
    assert cfg["MIN_SWITCH_TIME"] == 10
    assert cfg["CC2_TOPIC_PREFIX"] == "cc2"
    assert cfg["MQTT_TOPIC_PREFIX"] == "panda_breath_mod"
    assert cfg["HA_BED_TEMPERATURE_ENTITY"] == "sensor.ks1c_bed_temperature"


def test_debug_true():
    assert generate_config({"PANDA_DEBUG": "true"})["DEBUG"] is True
    assert generate_config({"PANDA_DEBUG": "True"})["DEBUG"] is True
    assert generate_config({"PANDA_DEBUG": "TRUE"})["DEBUG"] is True


def test_debug_false():
    assert generate_config({"PANDA_DEBUG": "false"})["DEBUG"] is False
    assert generate_config({})["DEBUG"] is False
    assert generate_config({"PANDA_DEBUG": "FALSE"})["DEBUG"] is False


def test_type_coercion():
    cfg = generate_config({"PANDA_HYSTERESE": "2.5", "PANDA_MIN_SWITCH_TIME": "20"})
    assert cfg["HYSTERESE"] == 2.5
    assert isinstance(cfg["HYSTERESE"], float)
    assert cfg["MIN_SWITCH_TIME"] == 20
    assert isinstance(cfg["MIN_SWITCH_TIME"], int)


def test_printer_ip_mirrors_panda_ip():
    cfg = generate_config({"PANDA_IP": "10.0.0.5"})
    assert cfg["PANDA_IP"] == "10.0.0.5"
    assert cfg["PRINTER_IP"] == "10.0.0.5"


def test_printer_sn_from_panda_sn():
    cfg = generate_config({"PANDA_SN": "MYSERIAL123"})
    assert cfg["PRINTER_SN"] == "MYSERIAL123"


def test_ha_bed_entity_default():
    assert generate_config({})["HA_BED_TEMPERATURE_ENTITY"] == "sensor.ks1c_bed_temperature"


def test_ha_bed_entity_override():
    cfg = generate_config({"PANDA_HA_BED_ENTITY": "sensor.my_bed"})
    assert cfg["HA_BED_TEMPERATURE_ENTITY"] == "sensor.my_bed"


def test_debug_to_file_always_false():
    assert generate_config({"PANDA_DEBUG_TO_FILE": "true"})["DEBUG_TO_FILE"] is False
    assert generate_config({})["DEBUG_TO_FILE"] is False
