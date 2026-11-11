"""Tests for Panda.py CC2_IP conditional integration."""
import sys, os, json, time, asyncio, importlib.util, shutil
from importlib.machinery import SourceFileLoader
from pathlib import Path
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import panda_native_mqtt

from tests.conftest import current_state_dir

REPO = os.path.join(os.path.dirname(__file__), "..")


def load_panda(cc2_ip="", env_extra=None):
    # Run the real Panda.py source, but with __file__ inside the per-test temp dir
    # so BASE_DIR / PREHEAT_STATE_FILE (and the import-time _load_preheat_state())
    # never touch the repo's runtime preheat_state.json. A copy of the tracked
    # panda_config.json is placed there so import-time config loading is unchanged.
    state_dir = current_state_dir()
    real_src = Path(REPO, "Panda.py").resolve()
    real_cfg = real_src.parent / "panda_config.json"
    if real_cfg.is_file():
        shutil.copyfile(real_cfg, state_dir / "panda_config.json")
    env = {
        "CC2_IP": cc2_ip,
        "CC2_TOPIC_PREFIX": "cc2",
        "HA_MQTT_BROKER": "dummy",
        "PANDA_DEBUG": "false",
    }
    if env_extra:
        env.update({k: ("" if v is None else str(v)) for k, v in env_extra.items()})
    with patch.dict(os.environ, env, clear=False):
        with patch("paho.mqtt.client.Client") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            with patch("asyncio.get_event_loop", return_value=MagicMock()):
                spec = importlib.util.spec_from_file_location(
                    "panda_mod",
                    str(state_dir / "Panda.py"),
                    loader=SourceFileLoader("panda_mod", str(real_src)),
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                assert mod.PREHEAT_STATE_FILE == state_dir.resolve() / "preheat_state.json", \
                    f"Panda.py state file not redirected: {mod.PREHEAT_STATE_FILE}"
                return mod, mock_instance


def _fake_msg(topic, payload, retain=False):
    msg = MagicMock()
    msg.topic = topic
    msg.payload = payload.encode() if isinstance(payload, str) else payload
    msg.retain = retain
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
        "PETG": 0,
        "PETG-CF": 0,
        "PETG-GF": 0,
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


def test_petg_does_not_use_unknown_material_fallback():
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.CC2_HA_FALLBACK_TEMP = 45
    mod.current_data.update(cc2_print_status="idle", chamber_setpoint=0.0,
                            cc2_pending_filament="PETG")
    mod._handle_cc2_data("print_status", "preheating")
    assert mod.current_data["chamber_setpoint"] == 0


def test_late_petg_leaves_owned_pause_paused(tmp_path):
    """State-file-restored preheat claim + printer paused: ownership is not provable
    via the protocol after a restart, so cancel must clear state, log the reason,
    and leave the pause for the user — never press resume."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.main_loop = None
    mod.current_data.update(slicer_priority_mode=True, cc2_print_status="paused",
                            chamber_setpoint=45, cc2_pending_filament="")
    mod.cc2_paused_for_preheat = True
    with patch.object(mod, "_cc2_press_button") as press, patch.object(mod, "log_event") as log:
        mod._handle_cc2_data("active_filament_type", "PETG")
        press.assert_not_called()
    assert mod.cc2_paused_for_preheat is False
    saved = json.loads((tmp_path / "preheat_state.json").read_text())
    assert saved["cc2_paused_for_preheat"] is False
    msgs = " ".join(str(c.args[0]) for c in log.call_args_list if c.args)
    assert "not auto-resuming" in msgs
    assert mod.current_data["chamber_setpoint"] == 0


def test_repeated_petg_update_does_not_republish():
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.main_loop = None
    mod.current_data.update(slicer_priority_mode=True, cc2_print_status="printing",
                            chamber_setpoint=0, cc2_pending_filament="")
    mod._handle_cc2_data("active_filament_type", "PETG")
    client.reset_mock()
    mod._handle_cc2_data("active_filament_type", "PETG")
    client.publish.assert_not_called()


def test_cancel_with_pause_in_flight_never_resumes_landing_pause(tmp_path):
    """Dispatched-but-unacknowledged pause: cancel arrives while our press may still
    be landing ('pausing'). The later 'paused' is ambiguous (could equally be a user
    pause) — it must never be auto-released."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.main_loop = None
    mod.current_data.update(slicer_priority_mode=True, cc2_print_status="pausing",
                            chamber_setpoint=45, cc2_pending_filament="")
    mod.cc2_paused_for_preheat = True
    mod._cc2_pause_in_flight = True
    mod._cc2_pause_in_flight_at = time.time()
    with patch.object(mod, "_cc2_press_button") as press, patch.object(mod, "log_event") as log:
        mod._handle_cc2_data("active_filament_type", "PETG")
        press.assert_not_called()
        assert mod.cc2_paused_for_preheat is False
        assert mod._cc2_pause_in_flight is False
        mod._handle_cc2_data("print_status", "paused")   # our press (or the user) lands
        press.assert_not_called()
        mod._handle_cc2_data("print_status", "paused")   # duplicate delivery
        press.assert_not_called()
    msgs = " ".join(str(c.args[0]) for c in log.call_args_list if c.args)
    assert "not auto-resuming" in msgs


# ── Late no-heat material: cancel pending preheat safely ─────────────────

def _instant_sleep(_delay):
    async def _s():
        return None
    return _s()


def _capture_scheduler(captured):
    """Fake asyncio.run_coroutine_threadsafe that records coroutines instead of running them."""
    def _schedule(coro, _loop):
        captured.append(coro)
        return MagicMock()
    return _schedule


def _cold_printing(mod):
    mod.panda_writer = MagicMock()
    mod.main_loop = MagicMock()
    mod.current_data.update(slicer_priority_mode=True, cc2_print_status="printing",
                            chamber_setpoint=0, cc2_pending_filament="",
                            cc2_resolved_filament="", slicer_soll=0.0,
                            chamber_temp=0, cc2_chamber_temp=0)


def test_zero_target_cancels_scheduled_heat_before_any_command(tmp_path):
    """PETG delivered after ASA heat was scheduled but before it ran:
    the coroutine must abort before touching the heater or the pause button."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    _cold_printing(mod)
    captured = []
    sends = MagicMock()

    async def _send(p):
        sends(p)

    with patch("asyncio.run_coroutine_threadsafe", side_effect=_capture_scheduler(captured)):
        mod._handle_cc2_data("active_filament_type", "ASA")    # schedules heat (gen 0)
        mod._handle_cc2_data("active_filament_type", "PETG")   # zero target → gen bump
    assert len(captured) >= 1
    heat = captured[0]
    for c in captured[1:]:
        c.close()
    with patch.object(mod, "panda_send", _send), patch("asyncio.sleep", _instant_sleep), \
            patch.object(mod, "_cc2_press_button") as press:
        asyncio.run(heat)
    sends.assert_not_called()
    press.assert_not_called()
    assert mod.cc2_paused_for_preheat is False
    assert mod._cc2_pause_in_flight is False
    assert mod.current_data["chamber_setpoint"] == 0


def test_cancel_during_pause_sleep_unclaims_flag_and_skips_pause(tmp_path):
    """PETG lands while the heat coroutine is inside its 1.5 s pre-pause sleep:
    the coroutine must un-set the flag it claimed and never press pause."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    _cold_printing(mod)
    captured = []
    fired = {"done": False}

    def gated_sleep(delay):
        async def _s():
            if delay == 1.5 and not fired["done"]:
                fired["done"] = True
                # Cancel arrives while the coroutine is suspended after claiming the flag.
                mod._handle_cc2_data("active_filament_type", "PETG")
        return _s()

    with patch("asyncio.run_coroutine_threadsafe", side_effect=_capture_scheduler(captured)):
        mod._handle_cc2_data("active_filament_type", "ASA")
        heat = captured[0]
        async def _noop_send(_p):
            return None
        with patch.object(mod, "panda_send", _noop_send), patch("asyncio.sleep", gated_sleep), \
                patch.object(mod, "_cc2_press_button") as press:
            asyncio.run(heat)
        # PETG delivery scheduled a heater-off coroutine — close it un-run.
        for c in captured[1:]:
            c.close()
    press.assert_not_called()
    assert mod.cc2_paused_for_preheat is False, "cancelled coroutine must un-claim the preheat flag"
    assert mod._cc2_pause_in_flight is False
    assert mod.current_data["chamber_setpoint"] == 0
    saved = json.loads((tmp_path / "preheat_state.json").read_text())
    assert saved["cc2_paused_for_preheat"] is False


def test_pause_in_flight_late_petg_never_resumes_landed_pause(tmp_path):
    """Pause already dispatched (status still 'printing'), then PETG resolves:
    the landing 'paused' is ambiguous — it must never be auto-released."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    _cold_printing(mod)
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_capture_scheduler(captured)):
        mod._handle_cc2_data("active_filament_type", "ASA")
        heat = captured[0]
        async def _noop_send(_p):
            return None
        with patch.object(mod, "panda_send", _noop_send), patch("asyncio.sleep", _instant_sleep), \
                patch.object(mod, "_cc2_press_button") as press, patch.object(mod, "log_event") as log:
            asyncio.run(heat)                      # pause pressed, flag + in_flight set
            press.assert_called_once_with("pause")
            assert mod.cc2_paused_for_preheat is True
            mod._handle_cc2_data("active_filament_type", "PETG")   # cancel while in flight
            press.assert_called_once_with("pause")                 # no compensating resume
            assert mod.cc2_paused_for_preheat is False
            assert mod._cc2_pause_in_flight is False
            mod._handle_cc2_data("print_status", "paused")         # pause lands
            press.assert_called_once_with("pause")
            mod._handle_cc2_data("print_status", "paused")         # duplicate delivery
            press.assert_called_once_with("pause")
        for c in captured[1:]:
            c.close()
    msgs = " ".join(str(c.args[0]) for c in log.call_args_list if c.args)
    assert "not auto-resuming" in msgs


def test_cancel_between_marker_and_dispatch_never_arms_resume(tmp_path):
    """Cancel landing at the exact dispatch moment (formerly between the in-flight
    marker and the press): the press is atomic with the generation check under the
    lock, so the cancel sees it in flight, presses nothing, and arms no resume —
    the landing 'paused' is left alone."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    _cold_printing(mod)
    captured = []
    dispatched = []

    def _press_at_dispatch(action):
        dispatched.append(action)
        if action == "pause":
            # Reentrant cancel at the dispatch instant — the old marker/dispatch race.
            mod._cancel_cc2_preheat_zero_target()

    with patch("asyncio.run_coroutine_threadsafe", side_effect=_capture_scheduler(captured)):
        mod._handle_cc2_data("active_filament_type", "ASA")
        heat = captured[0]
        async def _noop_send(_p):
            return None
        with patch.object(mod, "panda_send", _noop_send), patch("asyncio.sleep", _instant_sleep), \
                patch.object(mod, "_cc2_press_button", side_effect=_press_at_dispatch):
            asyncio.run(heat)
        for c in captured[1:]:
            c.close()
    assert dispatched == ["pause"]                 # only the pause; never a resume
    assert mod.cc2_paused_for_preheat is False
    assert mod._cc2_pause_in_flight is False
    # The dispatched pause may still land — it must never be auto-released.
    with patch.object(mod, "_cc2_press_button") as press:
        mod._handle_cc2_data("print_status", "paused")
        mod._handle_cc2_data("print_status", "paused")
        press.assert_not_called()


def test_fallback_preheat_paused_then_late_petg_leaves_paused(tmp_path):
    """Unknown material + CC2_HA_FALLBACK_TEMP pauses the print; PETG discovered
    afterwards must clear preheat state and zero the target, but never resume —
    the observed pause is not provably ours."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.CC2_HA_FALLBACK_TEMP = 45
    mod.current_data.update(slicer_priority_mode=True, cc2_print_status="idle",
                            chamber_setpoint=0, cc2_pending_filament="",
                            chamber_temp=0, cc2_chamber_temp=0)
    mod.panda_writer = MagicMock()
    mod.main_loop = MagicMock()
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_capture_scheduler(captured)):
        mod._handle_cc2_data("print_status", "preheating")   # fallback arms 45°C + pause
        assert mod.current_data["chamber_setpoint"] == 45
        heat = captured[0]
        async def _noop_send(_p):
            return None
        with patch.object(mod, "panda_send", _noop_send), patch("asyncio.sleep", _instant_sleep), \
                patch.object(mod, "_cc2_press_button") as press:
            asyncio.run(heat)
            press.assert_called_once_with("pause")
            mod._handle_cc2_data("active_filament_type", "PETG")
            assert mod.cc2_paused_for_preheat is False
            press.assert_called_once_with("pause")             # no compensating resume
            mod._handle_cc2_data("print_status", "paused")     # pause lands
            press.assert_called_once_with("pause")             # never released
        for c in captured[1:]:
            c.close()
    assert mod.current_data["chamber_setpoint"] == 0


def test_late_petg_does_not_resume_user_pause(tmp_path):
    """Paused by the user (no owned preheat, nothing in flight): PETG must not resume."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.main_loop = None
    mod.cc2_paused_for_preheat = False
    mod.current_data.update(slicer_priority_mode=True, cc2_print_status="paused",
                            chamber_setpoint=45, cc2_pending_filament="")
    with patch.object(mod, "_cc2_press_button") as press:
        mod._handle_cc2_data("active_filament_type", "PETG")
        press.assert_not_called()
        mod._handle_cc2_data("print_status", "paused")   # still the user's pause
        press.assert_not_called()
    assert not hasattr(mod, "_cc2_cancelled_preheat")   # no compensating-resume marker
    assert mod.current_data["chamber_setpoint"] == 0


def test_simultaneous_user_pause_is_never_resumed(tmp_path):
    """User pauses at the same moment our preheat pause press is in flight: the
    resulting 'paused' is provably ambiguous — cancel must clear state, log the
    reason, and never press resume."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.main_loop = None
    mod.cc2_paused_for_preheat = True
    mod._cc2_pause_in_flight = True
    mod._cc2_pause_in_flight_at = time.time()
    mod.current_data.update(slicer_priority_mode=True, cc2_print_status="paused",
                            chamber_setpoint=45, cc2_pending_filament="")
    with patch.object(mod, "_cc2_press_button") as press, patch.object(mod, "log_event") as log:
        mod._handle_cc2_data("active_filament_type", "PETG")
        press.assert_not_called()
        mod._handle_cc2_data("print_status", "paused")   # repeated paused status
        press.assert_not_called()
    msgs = " ".join(str(c.args[0]) for c in log.call_args_list if c.args)
    assert "not auto-resuming" in msgs
    assert mod.cc2_paused_for_preheat is False
    assert mod.current_data["chamber_setpoint"] == 0


def test_asa_still_preheats_and_pauses(tmp_path):
    """Positive-target materials keep the existing preheat+pause behavior."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    _cold_printing(mod)
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_capture_scheduler(captured)):
        mod._handle_cc2_data("active_filament_type", "ASA")
        heat = captured[0]
        async def _noop_send(_p):
            return None
        with patch.object(mod, "panda_send", _noop_send), patch("asyncio.sleep", _instant_sleep), \
                patch.object(mod, "_cc2_press_button") as press:
            asyncio.run(heat)
        for c in captured[1:]:
            c.close()
    press.assert_called_once_with("pause")
    assert mod.cc2_paused_for_preheat is True
    assert mod.current_data["chamber_setpoint"] == 55
    saved = json.loads((tmp_path / "preheat_state.json").read_text())
    assert saved["cc2_paused_for_preheat"] is True


def test_stale_ha_material_cleared_so_fallback_applies(tmp_path):
    """Unresolved HA poll must drop a stale buffered material so the next print's
    unknown-material fallback is not suppressed (Codex P2)."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.CC2_HA_FALLBACK_TEMP = 45
    mod.current_data.update(cc2_print_status="idle", chamber_setpoint=0.0,
                            cc2_pending_filament="PETG", cc2_resolved_filament="PETG")
    mod._mark_cc2_filament_unresolved()
    assert mod.current_data["cc2_pending_filament"] == ""
    assert mod.current_data["cc2_resolved_filament"] == ""
    mod._handle_cc2_data("print_status", "printing")
    assert mod.current_data["chamber_setpoint"] == 45


def test_fresh_petg_still_suppresses_fallback(tmp_path):
    """A resolved PETG (not stale) must still suppress the unknown fallback."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.CC2_HA_FALLBACK_TEMP = 45
    mod.current_data.update(cc2_print_status="idle", chamber_setpoint=0.0,
                            cc2_pending_filament="PETG")
    mod._handle_cc2_data("print_status", "preheating")
    assert mod.current_data["chamber_setpoint"] == 0


def test_filename_pending_survives_unresolved_cycle(tmp_path):
    """Filename-encoded chamber settings stay authoritative across unresolved polls."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.current_data.update(cc2_pending_filament="FILENAME-55C", chamber_setpoint=55.0)
    mod._mark_cc2_filament_unresolved()
    assert mod.current_data["cc2_pending_filament"] == "FILENAME-55C"
    assert mod.current_data["chamber_setpoint"] == 55.0


def test_user_pause_after_cancel_is_untouched(tmp_path):
    """A user pause after a completed cancel must never be touched: no marker, no
    grace-window heuristic, no resume — the compensating-resume mechanism is gone."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.main_loop = None
    mod.current_data.update(slicer_priority_mode=True, cc2_print_status="printing",
                            chamber_setpoint=45, cc2_pending_filament="")
    mod._handle_cc2_data("active_filament_type", "PETG")   # cancel, nothing owned
    assert not hasattr(mod, "_cc2_cancelled_preheat")      # no resume marker exists at all
    assert not hasattr(mod, "_CC2_CANCEL_GRACE")
    with patch.object(mod, "_cc2_press_button") as press:
        mod._handle_cc2_data("print_status", "paused")     # user pauses later
        press.assert_not_called()
    assert mod.cc2_paused_for_preheat is False


def test_print_end_clears_transient_pause_state(tmp_path):
    """Terminal status must drop the in-flight marker so the next job starts clean."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.main_loop = MagicMock()
    mod.current_data.update(cc2_print_status="printing", cc2_pending_filament="ASA",
                            cc2_resolved_filament="ASA")
    mod._cc2_pause_in_flight = True
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_capture_scheduler(captured)):
        mod._handle_cc2_data("print_status", "idle")
    for c in captured:
        c.close()
    assert mod._cc2_pause_in_flight is False
    assert mod.current_data["cc2_pending_filament"] == ""
    assert mod.current_data["cc2_resolved_filament"] == ""


def test_cc2_target_delta_ignores_panda_overshoot_temp():
    """Status delta should show CC2 remaining gap, not Panda-to-CC2 spread."""
    mod, _mock_client = load_panda(cc2_ip="192.168.1.50")

    assert mod._format_cc2_target_delta(55, 43) == "+12"
    assert mod._format_cc2_target_delta(55, 56) == "-1"


def test_cc2_real_target_ignores_temporary_panda_overshoot_setpoint():
    """Resume threshold should use filament target, not temporary Panda set_temp."""
    mod, _mock_client = load_panda(cc2_ip="192.168.1.50")

    mod.current_data["slicer_priority_mode"] = True
    mod.current_data["slicer_soll"] = 55.0
    mod.current_data["chamber_setpoint"] = 60.0

    assert mod._cc2_real_chamber_target() == 55.0
    assert 48.0 >= (mod._cc2_real_chamber_target() - mod.CC2_RESUME_OFFSET)


def test_resume_ready_printing_transition_does_not_repause():
    """At the resume threshold, a preheating→printing transition must not re-pause CC2."""
    mod, mock_client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = mock_client
    mod.cc2_paused_for_preheat = True
    mod.current_data["slicer_priority_mode"] = True
    mod.current_data["slicer_soll"] = 55.0
    mod.current_data["chamber_setpoint"] = 60.0
    mod.current_data["chamber_temp"] = 60.0
    mod.current_data["cc2_chamber_temp"] = 48.0
    mod.current_data["cc2_print_status"] = "preheating"

    with patch.object(mod, "_cc2_press_button") as press_button:
        mod._handle_cc2_data("print_status", "printing")

    press_button.assert_not_called()
    assert mod.cc2_paused_for_preheat is False


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


# ── Heater command ordering: stale heat-on must never land after the PETG off ──

def _is_heat_on(d):
    s = d.get("settings", {})
    return s.get("work_on") is True and s.get("isrunning") == 1


def _is_off(d):
    s = d.get("settings", {})
    return s.get("work_on") is False and s.get("set_temp") == 0


class _Wire:
    """Fake Panda link recording what reaches the 'wire'.

    mode="ws":     send() suspends on `release` BEFORE recording a gated payload
                   (a transport that yields before writing).
    mode="writer": write() records synchronously; drain() suspends on `release`
                   after a gated payload was written (the real writer path).
    """
    def __init__(self, gate, mode="ws"):
        self.gate, self.mode = gate, mode
        self.log = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._last = None

    async def send(self, payload):
        d = json.loads(payload)
        if self.gate(d) and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        self.log.append(d)

    def is_closing(self):
        return False

    def write(self, frame):
        body = frame[2:] if frame[1] < 126 else frame[4:]
        self._last = json.loads(body)
        self.log.append(self._last)

    async def drain(self):
        if self._last is not None and self.gate(self._last) and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()


async def _ordering_scenario(mod, tmp_path, gate, mode="ws", release=True, send_timeout=None,
                             entry="filament", after="petg"):
    """ASA heat is suspended inside one send; then `after` happens ("petg" = PETG
    arrives, "print_end" = printing -> idle, "petg_late"/"print_end_late" = the same but
    only after the heat coroutine finished, None = nothing); then the send completes
    (release=True) or never does (release=False, bounded by send_timeout).
    Every wait is bounded so a deadlock fails the test instead of hanging it."""
    loop = asyncio.get_running_loop()
    real_sleep = asyncio.sleep

    async def fast_sleep(_d, *a, **k):
        await real_sleep(0)

    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    _cold_printing(mod)
    mod.main_loop = loop
    if send_timeout is not None:
        mod._PANDA_SEND_TIMEOUT = send_timeout
    wire = _Wire(gate, mode)
    if mode == "ws":
        mod.panda_ws, mod.panda_writer = wire, None
    else:
        mod.panda_ws, mod.panda_writer = None, wire
    futures = []
    real_sched = asyncio.run_coroutine_threadsafe

    def sched(coro, lp):
        f = real_sched(coro, lp)
        futures.append(asyncio.wrap_future(f))
        return f

    with patch("asyncio.run_coroutine_threadsafe", sched), patch("asyncio.sleep", fast_sleep), \
            patch.object(mod, "_cc2_press_button") as press:
        if entry == "filament":
            mod._handle_cc2_data("active_filament_type", "ASA")    # schedules _cc2_heat
        else:
            # Print start with ASA buffered while idle schedules _cc2_heat_on_start.
            mod.current_data.update(cc2_print_status="idle", cc2_pending_filament="ASA")
            mod._handle_cc2_data("print_status", "preheating")
        await asyncio.wait_for(wire.entered.wait(), 1)             # heat is suspended in its send
        assert len(futures) == 1
        if after == "petg":
            mod._handle_cc2_data("active_filament_type", "PETG")   # cancel + schedules off
        elif after == "print_end":
            mod._handle_cc2_data("print_status", "idle")           # print stops + schedules off
        if after in ("petg", "print_end"):
            assert len(futures) == 2
            # Give the off a bounded chance to run while the heat send is still suspended.
            await asyncio.wait([futures[1]], timeout=0.1)
        if release:
            wire.release.set()
        if after in ("petg_late", "print_end_late"):
            # The heat coroutine has fully finished (valid ASA: claimed + pressed pause);
            # only now does the cancel / print stop arrive.
            await asyncio.wait_for(futures[0], 2)
            if after == "petg_late":
                mod._handle_cc2_data("active_filament_type", "PETG")
            else:
                mod._handle_cc2_data("print_status", "idle")
        await asyncio.wait_for(asyncio.gather(*futures), 2)
    return wire, press


def _assert_final_off(wire, press, mod):
    assert wire.log, "nothing reached the wire"
    assert _is_off(wire.log[-1]), f"final command is not off: {wire.log}"
    off_idx = max(i for i, d in enumerate(wire.log) if _is_off(d))
    assert not any(_is_heat_on(d) for d in wire.log[off_idx + 1:]), f"stale heat-on after off: {wire.log}"
    press.assert_not_called()
    assert mod.cc2_paused_for_preheat is False
    assert mod._cc2_pause_in_flight is False
    assert mod.current_data["chamber_setpoint"] == 0


@pytest.mark.parametrize("entry", ["filament", "print_start"])
def test_stale_heat_on_cannot_land_after_off_when_send_yields_before_write(tmp_path, entry):
    """Heat-on is suspended in a transport that yields BEFORE writing; PETG's off must
    not overtake it and then be followed by the stale heat-on."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    wire, press = asyncio.run(_ordering_scenario(mod, tmp_path, _is_heat_on, "ws", entry=entry))
    _assert_final_off(wire, press, mod)


def test_stale_heat_on_cannot_land_after_off_with_writer_drain_suspended(tmp_path):
    """Control for the real writer path: write() is synchronous, so a heat-on stuck in
    drain() is already ahead of the off on the wire."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    wire, press = asyncio.run(_ordering_scenario(mod, tmp_path, _is_heat_on, "writer"))
    _assert_final_off(wire, press, mod)
    assert [bool(_is_heat_on(d)) for d in wire.log].index(True) < [bool(_is_off(d)) for d in wire.log].index(True)


@pytest.mark.parametrize("entry", ["filament", "print_start"])
def test_cancel_while_final_get_settings_send_in_flight_leaves_no_stale_state(tmp_path, entry):
    """PETG lands while the last heat command (get_settings) is awaiting: the coroutine
    must not mark the heater ON, record an overshoot, or claim the preheat pause."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.global_heating_state = mod.RELAY_OFF
    wire, press = asyncio.run(_ordering_scenario(mod, tmp_path, lambda d: "get_settings" in d, "ws", entry=entry))
    _assert_final_off(wire, press, mod)
    assert mod.global_heating_state != mod.RELAY_ON, "stale heat coroutine marked heater ON after cancel"
    assert mod._preheat_overshoot_temp == 0


def test_hung_heat_send_does_not_deadlock_the_off(tmp_path):
    """A heat-on send that never completes must not block the off forever."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    wire, press = asyncio.run(_ordering_scenario(mod, tmp_path, _is_heat_on, "ws",
                                                 release=False, send_timeout=0.2))
    _assert_final_off(wire, press, mod)


class _CancelBeforeClaimLock:
    """Stands in for _cc2_heat_lock. Just before the heat coroutine takes the lock to
    CLAIM the preheat pause (heater already marked ON, no claim yet) it first lets a
    zero-target cancel run — as if the MQTT thread bumped the generation right then."""
    def __init__(self, mod, cancel):
        self._real, self._mod, self._cancel, self._fired = mod._cc2_heat_lock, mod, cancel, False

    def __enter__(self):
        if (not self._fired and self._mod.global_heating_state == self._mod.RELAY_ON
                and not self._mod.cc2_paused_for_preheat):
            self._fired = True
            self._cancel()
        return self._real.__enter__()

    def __exit__(self, *a):
        return self._real.__exit__(*a)


@pytest.mark.parametrize("entry", ["filament", "print_start"])
def test_cancel_just_before_preheat_claim_never_leaves_a_stale_claim(tmp_path, entry):
    """A cancel landing after the last send but before the pause claim must stop the
    claim: no cc2_paused_for_preheat=True may be set or persisted after the cancel."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.global_heating_state = mod.RELAY_OFF
    saved_flags, cancel_done = [], []
    real_save = mod._save_preheat_state

    def spy_save():
        saved_flags.append(mod.cc2_paused_for_preheat)
        real_save()

    def cancel():
        mod._handle_cc2_data("active_filament_type", "PETG")
        cancel_done.append(len(saved_flags))

    async def run():
        loop = asyncio.get_running_loop()
        real_sleep, real_sched = asyncio.sleep, asyncio.run_coroutine_threadsafe

        async def fast_sleep(_d, *a, **k):
            await real_sleep(0)

        mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
        _cold_printing(mod)
        mod.main_loop = loop
        wire = _Wire(lambda d: False, "ws")          # never gates: only the injected cancel races
        mod.panda_ws, mod.panda_writer = wire, None
        mod._cc2_heat_lock = _CancelBeforeClaimLock(mod, cancel)
        futures = []

        def sched(coro, lp):
            f = real_sched(coro, lp)
            futures.append(asyncio.wrap_future(f))
            return f

        with patch("asyncio.run_coroutine_threadsafe", sched), patch("asyncio.sleep", fast_sleep), \
                patch.object(mod, "_save_preheat_state", spy_save), \
                patch.object(mod, "_cc2_press_button") as press:
            if entry == "filament":
                mod._handle_cc2_data("active_filament_type", "ASA")
            else:
                mod.current_data.update(cc2_print_status="idle", cc2_pending_filament="ASA")
                mod._handle_cc2_data("print_status", "preheating")
            # The first future is the heat coroutine; the injected cancel adds the off.
            await asyncio.wait_for(futures[0], 2)
            await asyncio.wait_for(asyncio.gather(*futures), 2)
        return wire, press

    wire, press = asyncio.run(run())
    assert cancel_done, "cancel never injected before the claim"
    assert True not in saved_flags[cancel_done[0]:], f"stale claim persisted after cancel: {saved_flags}"
    _assert_final_off(wire, press, mod)


# ── Timed-out / delayed heater sends: ambiguous delivery must not lose the pause ──
# A timed-out send is UNCONFIRMED, not "not sent": with websockets 16 a text frame is
# written to the transport synchronously and only the flow-control drain can still be
# pending, so the heat-on may well be on the wire. A valid ASA job must therefore carry
# on to its preheat pause (otherwise the heater is on while the print runs cold), and
# a PETG cancel / print stop must still leave no stale heat, claim or pause behind.

_TIMEOUT = 0.05


def _run_scenario(mod, tmp_path, *args, **kwargs):
    kwargs.setdefault("send_timeout", _TIMEOUT)
    with patch.object(mod, "log_event") as log:
        wire, press = asyncio.run(_ordering_scenario(mod, tmp_path, *args, **kwargs))
    msgs = " ".join(str(c.args[0]) for c in log.call_args_list if c.args)
    return wire, press, msgs


@pytest.mark.parametrize("mode", ["writer", "ws"])
@pytest.mark.parametrize("entry", ["filament", "print_start"])
def test_valid_asa_heat_on_timeout_still_claims_and_presses_preheat_pause(tmp_path, entry, mode):
    """The heat-on send times out (writer: frame written, drain pending; ws: frame maybe
    never written). Delivery is unknown, so the heater is assumed possibly ON and the
    preheat pause MUST still happen — never 'abort and let the print run cold with a
    live heater'. Conservative by design: with no device ack, a paused print (visible,
    released automatically once the chamber is warm or the reconnect re-sends the
    overshoot heat) is safer than a silent unpaused one."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.global_heating_state = mod.RELAY_OFF
    wire, press, msgs = _run_scenario(mod, tmp_path, _is_heat_on, mode, release=False,
                                      entry=entry, after=None)
    assert "UNCONFIRMED" in msgs
    assert press.call_args_list == [call("pause")]
    assert mod.cc2_paused_for_preheat is True
    assert mod._preheat_overshoot_temp > 0
    assert mod.global_heating_state == mod.RELAY_ON
    assert not any(_is_off(d) for d in wire.log), "valid ASA must not be turned off"
    if mode == "writer":
        assert any(_is_heat_on(d) for d in wire.log), "writer path: the frame is on the wire"
    assert json.loads(mod.PREHEAT_STATE_FILE.read_text())["cc2_paused_for_preheat"] is True


@pytest.mark.parametrize("mode", ["writer", "ws"])
@pytest.mark.parametrize("release", [True, False])
@pytest.mark.parametrize("entry", ["filament", "print_start"])
@pytest.mark.parametrize("after", ["petg", "print_end"])
def test_cancel_or_print_stop_during_delayed_heat_send_leaves_off_last_and_no_stale_state(
        tmp_path, after, entry, release, mode):
    """PETG arrives / the print stops while the ASA heat-on is suspended in its send —
    whether that send later completes (release) or times out. Either way: the off is the
    last command, no further heat-on follows it, no claim, no pause, no ON marker."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.global_heating_state = mod.RELAY_OFF
    wire, press, _ = _run_scenario(mod, tmp_path, _is_heat_on, mode, release=release,
                                   entry=entry, after=after)
    _assert_final_off(wire, press, mod)
    assert mod.global_heating_state != mod.RELAY_ON
    assert mod._preheat_overshoot_temp == 0
    assert mod._panda_off_pending is False


@pytest.mark.parametrize("after", ["petg_late", "print_end_late"])
@pytest.mark.parametrize("entry", ["filament", "print_start"])
def test_cancel_or_stop_after_unconfirmed_heat_pause_never_resumes(tmp_path, after, entry):
    """Valid ASA, heat-on unconfirmed, pause pressed — THEN PETG arrives / the print stops.
    The pause could be ours or a simultaneous user pause (indistinguishable), so it is
    never auto-resumed; our claim and in-flight marker are cleared; the off goes last."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.global_heating_state = mod.RELAY_OFF
    wire, press, msgs = _run_scenario(mod, tmp_path, _is_heat_on, "writer", release=False,
                                      entry=entry, after=after)
    assert press.call_args_list == [call("pause")], "only the original pause, never a resume"
    assert mod.cc2_paused_for_preheat is False
    assert mod._cc2_pause_in_flight is False
    assert mod.current_data["chamber_setpoint"] == 0
    assert _is_off(wire.log[-1])
    off_idx = max(i for i, d in enumerate(wire.log) if _is_off(d))
    assert not any(_is_heat_on(d) for d in wire.log[off_idx + 1:])
    if after == "petg_late":
        assert "not auto-resuming" in msgs


def _petg_off_coroutine(mod, tmp_path):
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    _cold_printing(mod)
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", side_effect=_capture_scheduler(captured)), \
            patch.object(mod, "log_event"):
        mod._handle_cc2_data("active_filament_type", "PETG")
    assert len(captured) == 1
    return captured[0]


@pytest.mark.parametrize("mode", ["writer", "ws"])
def test_off_that_times_out_is_not_reported_delivered_and_stays_pending(tmp_path, mode):
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod._PANDA_SEND_TIMEOUT = _TIMEOUT
    off = _petg_off_coroutine(mod, tmp_path)
    wire = _Wire(_is_off, mode)               # the off hangs in send()/drain() forever
    mod.panda_ws, mod.panda_writer = (wire, None) if mode == "ws" else (None, wire)
    with patch.object(mod, "log_event") as log:
        asyncio.run(off)
    msgs = " ".join(str(c.args[0]) for c in log.call_args_list if c.args)
    assert mod._panda_off_pending is True
    assert "NOT confirmed delivered" in msgs and "UNKNOWN" in msgs
    assert "heater off" not in msgs.lower().replace("heater-off", "")  # never claims it is off


def test_off_dropped_while_disconnected_is_not_reported_delivered(tmp_path):
    """panda_send only logs 'command dropped' when there is no link; that must not
    count as a delivered off."""
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    off = _petg_off_coroutine(mod, tmp_path)
    mod.panda_ws, mod.panda_writer = None, None
    with patch.object(mod, "log_event") as log:
        asyncio.run(off)
    msgs = " ".join(str(c.args[0]) for c in log.call_args_list if c.args)
    assert mod._panda_off_pending is True
    assert "NOT confirmed delivered" in msgs


def test_pending_off_is_replayed_once_and_cleared_when_accepted(tmp_path):
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    wire = _Wire(lambda d: False, "ws")
    mod.panda_ws, mod.panda_writer = wire, None
    mod._panda_off_pending, mod._panda_off_pending_at = True, time.time()
    with patch.object(mod, "log_event"):
        asyncio.run(mod._resend_pending_off())
        asyncio.run(mod._resend_pending_off())      # nothing pending any more
    assert [bool(_is_off(d)) for d in wire.log] == [True]
    assert mod._panda_off_pending is False


def test_stale_pending_off_is_dropped_not_replayed(tmp_path):
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    wire = _Wire(lambda d: False, "ws")
    mod.panda_ws, mod.panda_writer = wire, None
    mod._panda_off_pending = True
    mod._panda_off_pending_at = time.time() - mod._PANDA_OFF_PENDING_MAX_AGE - 1
    with patch.object(mod, "log_event") as log:
        asyncio.run(mod._resend_pending_off())
    assert wire.log == [] and mod._panda_off_pending is False
    assert "too old" in " ".join(str(c.args[0]) for c in log.call_args_list if c.args)


def test_live_heat_demand_supersedes_pending_off_but_a_stale_one_does_not(tmp_path):
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    wire = _Wire(lambda d: False, "ws")
    mod.panda_ws, mod.panda_writer = wire, None
    mod._panda_off_pending, mod._panda_off_pending_at = True, time.time()

    async def go(gen):
        with patch("asyncio.sleep", _instant_sleep):
            return await mod._cc2_send_heat_sequence(gen, 50)

    mod._cc2_heat_generation = 5
    with patch.object(mod, "log_event"):
        assert asyncio.run(go(4)) is mod.SendResult.SKIPPED      # superseded: sends nothing
        assert wire.log == [] and mod._panda_off_pending is True
        assert asyncio.run(go(5)) is mod.SendResult.SENT         # live demand
    assert mod._panda_off_pending is False
    assert any(_is_heat_on(d) for d in wire.log)


# ── WS loop heat sends must share the generation-ordered path ────────────────
# update_limits_from_ws sends heat from its own (stale) view of the target: the
# reconnect heat-on, the drop-to-target, the dynamic-overshoot set_temp and the
# auto-on. A PETG cancel (bump generation + queue the off) can land while any of
# them is suspended; none may put a positive target on the wire after the off.

def _is_positive_target(d):
    s = d.get("settings", {})
    return _is_heat_on(d) or (isinstance(s.get("set_temp"), (int, float)) and s["set_temp"] > 0)


class _LoopWs(_Wire):
    """Websocket for update_limits_from_ws: scripted recv(), then blocks forever."""
    def __init__(self, gate, messages):
        super().__init__(gate, "ws")
        self.messages = [json.dumps(m) for m in messages]
        self.drained = asyncio.Event()

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        self.drained.set()
        await asyncio.Event().wait()


class _ConnectOnce:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *a):
        return False


def _arm_asa_printing(mod, tmp_path, **extra):
    """ASA is the active 55C target on a printing CC2 (set directly: no heat scheduled)."""
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.current_data.update(slicer_priority_mode=True, cc2_print_status="printing",
                            chamber_setpoint=55.0, slicer_soll=55.0, cc2_pending_filament="ASA",
                            cc2_resolved_filament="ASA", chamber_temp=0, cc2_chamber_temp=0)
    mod.current_data.update(extra)
    mod.panda_writer = None
    mod._cc2_dynamic_overshoot_active = False
    mod._ws_bind_time = 0.0


async def _ws_loop_scenario(mod, messages, gate, cancel="petg", release=True):
    """Run update_limits_from_ws against a scripted websocket. The first send matching
    `gate` suspends BEFORE hitting the wire; `cancel` ("petg"/None) then arrives while it
    is suspended; then the send is released. Every wait is bounded."""
    loop = asyncio.get_running_loop()
    real_sleep = asyncio.sleep

    async def fast_sleep(_d, *a, **k):
        await real_sleep(0)

    mod.main_loop = loop
    ws = _LoopWs(gate, messages)
    mod.panda_ws = None
    futures = []
    real_sched = asyncio.run_coroutine_threadsafe

    def sched(coro, lp):
        f = real_sched(coro, lp)
        futures.append(asyncio.wrap_future(f))
        return f

    with patch("asyncio.run_coroutine_threadsafe", sched), patch("asyncio.sleep", fast_sleep), \
            patch.object(mod.websockets, "connect", lambda *a, **k: _ConnectOnce(ws)), \
            patch.object(mod, "_cc2_press_button") as press, patch.object(mod, "log_event"):
        task = asyncio.ensure_future(mod.update_limits_from_ws())
        try:
            await asyncio.wait_for(ws.entered.wait(), 2)     # a heat send is suspended
            if cancel == "petg":
                mod._handle_cc2_data("active_filament_type", "PETG")
                assert len(futures) == 1                     # the queued off
                await asyncio.wait([futures[0]], timeout=0.1)  # bounded chance to overtake
            if release:
                ws.release.set()
            await asyncio.wait_for(ws.drained.wait(), 2)
            if futures:
                await asyncio.wait_for(asyncio.gather(*futures), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return ws, press


def _assert_no_positive_target_after_off(ws, press, mod):
    assert any(_is_off(d) for d in ws.log), f"PETG off never reached the wire: {ws.log}"
    off_idx = max(i for i, d in enumerate(ws.log) if _is_off(d))
    stale = [d for d in ws.log[off_idx + 1:] if _is_positive_target(d)]
    assert not stale, f"stale positive target after the PETG off: {ws.log}"
    assert not any(c.args == ("resume",) for c in press.call_args_list), press.call_args_list
    assert mod.cc2_paused_for_preheat is False


_BIND_MSG = {"settings": {"warehouse_temper": 20, "work_mode": 2, "work_on": 0, "set_temp": 55, "isrunning": 0}}


def _fresh_loop_mod(tmp_path):
    mod, client = load_panda(cc2_ip="192.168.1.50")
    mod.mqtt_client = client
    mod.bind_confirmed = False
    mod.global_lock = False
    mod.power_forced_off = False
    mod._cc2_heat_generation = 0
    return mod


def test_reconnect_heat_on_cannot_follow_petg_cancel(tmp_path):
    """update_limits_from_ws reconnect re-sync: heat-on sent from the stale target while
    the PETG cancel's off is queued must not land after that off."""
    mod = _fresh_loop_mod(tmp_path)
    _arm_asa_printing(mod, tmp_path)
    ws, press = asyncio.run(_ws_loop_scenario(mod, [_BIND_MSG], _is_heat_on))
    _assert_no_positive_target_after_off(ws, press, mod)


def test_reconnect_heat_on_skipped_when_cancel_lands_before_the_send(tmp_path):
    """The target was read (positive) but the send is still queued behind another heater
    command when PETG cancels: the stale heat-on must never be sent at all."""
    mod = _fresh_loop_mod(tmp_path)
    _arm_asa_printing(mod, tmp_path)

    # Driver without a gated send: the heat-on is queued behind the held lock.
    async def driver():
        loop = asyncio.get_running_loop()
        real_sleep = asyncio.sleep

        async def fast_sleep(_d, *a, **k):
            await real_sleep(0)

        mod.main_loop = loop
        ws = _LoopWs(lambda d: False, [_BIND_MSG])
        mod.panda_ws = None
        with patch("asyncio.sleep", fast_sleep), \
                patch.object(mod.websockets, "connect", lambda *a, **k: _ConnectOnce(ws)), \
                patch.object(mod, "_cc2_press_button"), patch.object(mod, "log_event"):
            async with mod._get_panda_cmd_lock():
                task = asyncio.ensure_future(mod.update_limits_from_ws())
                for _ in range(30):
                    await real_sleep(0)                  # reach the queued heat-on
                mod._cc2_heat_generation += 1            # what the PETG cancel does first
                mod.current_data.update(chamber_setpoint=0.0, slicer_soll=0.0)
            try:
                await asyncio.wait_for(ws.drained.wait(), 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return ws

    ws = asyncio.run(driver())
    assert not any(_is_positive_target(d) for d in ws.log), ws.log


def test_drop_to_target_cannot_follow_petg_cancel_or_resume(tmp_path):
    """Preheat claim, chamber ready: the drop-to-target heat send is suspended when PETG
    cancels. Its positive set_temp must not follow the off, and the cancel's
    never-resume rule must hold (the loop must not press resume afterwards)."""
    mod = _fresh_loop_mod(tmp_path)
    _arm_asa_printing(mod, tmp_path, cc2_print_status="paused", cc2_chamber_temp=54.0)
    mod.bind_confirmed = True
    mod.cc2_paused_for_preheat = True
    mod._preheat_overshoot_temp = 60
    msg = {"settings": {"warehouse_temper": 54, "work_mode": 2, "work_on": 1, "set_temp": 60, "isrunning": 1}}
    gate = lambda d: d.get("settings", {}).get("set_temp") == 55
    ws, press = asyncio.run(_ws_loop_scenario(mod, [msg], gate))
    _assert_no_positive_target_after_off(ws, press, mod)


def test_dynamic_overshoot_set_temp_cannot_follow_petg_cancel(tmp_path):
    """CC2 far below target: the overshoot set_temp (a positive target write) is suspended
    when PETG cancels and must not land after the off."""
    mod = _fresh_loop_mod(tmp_path)
    _arm_asa_printing(mod, tmp_path, cc2_chamber_temp=30.0)
    mod.bind_confirmed = True
    mod.global_heating_state = mod.RELAY_ON
    msg = {"settings": {"warehouse_temper": 55, "work_mode": 2, "work_on": 1, "set_temp": 55, "isrunning": 1}}
    gate = lambda d: "work_on" not in d.get("settings", {}) and d.get("settings", {}).get("set_temp") == 60
    ws, press = asyncio.run(_ws_loop_scenario(mod, [msg], gate))
    _assert_no_positive_target_after_off(ws, press, mod)


def test_auto_on_heat_cannot_follow_petg_cancel(tmp_path):
    """Loop auto-on (chamber below target, heater idle): its heat-on, sent from the stale
    target, must not land after the PETG off."""
    mod = _fresh_loop_mod(tmp_path)
    _arm_asa_printing(mod, tmp_path, cc2_chamber_temp=50.0)
    mod.bind_confirmed = True
    mod.global_heating_state = mod.RELAY_OFF
    msg = {"settings": {"warehouse_temper": 30, "work_mode": 2, "work_on": 0, "set_temp": 55, "isrunning": 0}}
    ws, press = asyncio.run(_ws_loop_scenario(mod, [msg], _is_heat_on))
    _assert_no_positive_target_after_off(ws, press, mod)


# Controls: without a cancel the same sends still go out and the resume still fires.

def _drop_msg():
    return {"settings": {"warehouse_temper": 54, "work_mode": 2, "work_on": 1, "set_temp": 60, "isrunning": 1}}


def test_controls_ws_loop_heat_sends_still_work_without_a_cancel(tmp_path):
    # reconnect re-sync
    mod = _fresh_loop_mod(tmp_path)
    _arm_asa_printing(mod, tmp_path)
    ws, _ = asyncio.run(_ws_loop_scenario(mod, [_BIND_MSG], _is_heat_on, cancel=None))
    assert any(_is_heat_on(d) and d["settings"]["set_temp"] == 55 for d in ws.log), ws.log
    assert mod.global_heating_state == mod.RELAY_ON
    assert not any('get_settings' in d for d in ws.log), ws.log

    # drop-to-target still sends the real target and presses resume once
    mod = _fresh_loop_mod(tmp_path)
    _arm_asa_printing(mod, tmp_path, cc2_print_status="paused", cc2_chamber_temp=54.0)
    mod.bind_confirmed = True
    mod.cc2_paused_for_preheat = True
    mod._preheat_overshoot_temp = 60
    ws, press = asyncio.run(_ws_loop_scenario(
        mod, [_drop_msg()], lambda d: d.get("settings", {}).get("set_temp") == 55, cancel=None))
    assert any(_is_heat_on(d) and d["settings"]["set_temp"] == 55 for d in ws.log), ws.log
    assert [c.args for c in press.call_args_list] == [("resume",)]

    # dynamic overshoot set_temp
    mod = _fresh_loop_mod(tmp_path)
    _arm_asa_printing(mod, tmp_path, cc2_chamber_temp=30.0)
    mod.bind_confirmed = True
    mod.global_heating_state = mod.RELAY_ON
    msg = {"settings": {"warehouse_temper": 55, "work_mode": 2, "work_on": 1, "set_temp": 55, "isrunning": 1}}
    ws, _ = asyncio.run(_ws_loop_scenario(
        mod, [msg], lambda d: "work_on" not in d.get("settings", {}) and d.get("settings", {}).get("set_temp") == 60,
        cancel=None))
    assert {"settings": {"set_temp": 60}} in ws.log

    # auto-on
    mod = _fresh_loop_mod(tmp_path)
    _arm_asa_printing(mod, tmp_path, cc2_chamber_temp=50.0)
    mod.bind_confirmed = True
    mod.global_heating_state = mod.RELAY_OFF
    msg = {"settings": {"warehouse_temper": 30, "work_mode": 2, "work_on": 0, "set_temp": 55, "isrunning": 0}}
    ws, _ = asyncio.run(_ws_loop_scenario(mod, [msg], _is_heat_on, cancel=None))
    assert any(_is_heat_on(d) and d["settings"]["set_temp"] == 55 for d in ws.log), ws.log
    assert mod.global_heating_state == mod.RELAY_ON


# ── HA poller: stale material across jobs (driven through ha_cc2_poller) ─────

class _PollerStop(BaseException):
    """Ends the poller's endless loop once the scripted cycles are used up."""


_HA_DOWN = "__ha_down__"   # scripted cycle value: every HA request raises (disconnected)
_HA_FAIL = "__ha_fail__"   # scripted entity state: only that entity's request raises


def _ha_entities(with_color=True, with_filename=True):
    ents = {"chamber_temp": "sensor.cc2_chamber_temp", "print_status": "sensor.cc2_print_status",
            "_a1_color": "sensor.cc2_a1_color", "_a1_name": "sensor.cc2_a1_name",
            "_a2_color": "sensor.cc2_a2_color", "_a2_name": "sensor.cc2_a2_name"}
    if with_color:
        ents["_active_filament_color"] = "sensor.cc2_active_filament_color"
    if with_filename:
        ents["filename"] = "sensor.cc2_filename"
    return ents


_SLOTS = {"sensor.cc2_a1_color": "#FF0000", "sensor.cc2_a1_name": "PETG",
          "sensor.cc2_a2_color": "#00FF00", "sensor.cc2_a2_name": "ASA"}


def _poller_mod(tmp_path, entities, cc2_ip="", **current):
    """cc2_ip="" = HA-only (the poller is the sole material source); a value also
    enables the MQTT/CC2_IP source next to the poller."""
    mod, client = load_panda(cc2_ip=cc2_ip)
    mod.CC2_HA_MODE = True
    mod.CC2_ACTIVE = True
    mod.mqtt_client = client
    mod.PREHEAT_STATE_FILE = tmp_path / "preheat_state.json"
    mod.CC2_HA_FALLBACK_TEMP = 45
    mod.current_data.update(slicer_priority_mode=True, chamber_setpoint=0.0, chamber_temp=0,
                            cc2_chamber_temp=0, **current)
    mod._discover_ha_cc2_entities = lambda: entities
    return mod


def _run_poller(mod, cycles):
    """Run the real ha_cc2_poller for len(cycles) polls. Each cycle is a dict
    {entity_id: state} (missing id → 'unavailable') or _HA_DOWN. Returns a copy of
    current_data taken after each cycle."""
    snapshots, idx = [], {"i": -1}
    captured = []

    def _fake_get(url, headers=None, timeout=None):
        cyc = cycles[idx["i"]]
        if cyc is _HA_DOWN:
            raise ConnectionError("HA unreachable")
        eid = url.rsplit("/", 1)[1]
        if cyc.get(eid) == _HA_FAIL:
            raise ConnectionError(f"HA request for {eid} failed")
        return SimpleNamespace(json=lambda: {"state": cyc.get(eid, "unavailable"), "attributes": {}})

    async def _fake_sleep(_d):
        snapshots.append(dict(mod.current_data))
        if idx["i"] + 1 >= len(cycles):
            raise _PollerStop()
        idx["i"] += 1

    idx["i"] = 0
    with patch.object(mod.requests, "get", _fake_get), patch("asyncio.sleep", _fake_sleep), \
            patch("asyncio.run_coroutine_threadsafe", side_effect=_capture_scheduler(captured)):
        with pytest.raises(_PollerStop):
            asyncio.run(mod.ha_cc2_poller())
    for c in captured:
        c.close()
    return snapshots


def _idle(**extra):
    return {"sensor.cc2_print_status": "idle", **extra}


def _printing(**extra):
    return {"sensor.cc2_print_status": "printing", **extra}


@pytest.mark.parametrize("stale", ["PETG", "ASA"])
@pytest.mark.parametrize("sensor", ["missing", "unavailable", "unknown", "disconnected"])
def test_poller_new_job_unknown_material_drops_previous_job_material(tmp_path, stale, sensor):
    """A material buffered for the previous job must not suppress the unknown
    fallback when the new job starts and the poller cannot determine the material."""
    mod = _poller_mod(tmp_path, _ha_entities(with_color=sensor != "missing"),
                      cc2_print_status="idle", cc2_pending_filament=stale, cc2_resolved_filament=stale)
    color = {"sensor.cc2_active_filament_color": sensor} if sensor in ("unavailable", "unknown") else {}
    first = _HA_DOWN if sensor == "disconnected" else _idle(**color)
    snaps = _run_poller(mod, [first, _printing(**color)])
    assert snaps[0]["cc2_pending_filament"] == ""        # dropped while idle, before the job
    assert snaps[-1]["cc2_print_status"] == "printing"
    assert snaps[-1]["chamber_setpoint"] == 45           # unknown-material fallback applied


@pytest.mark.parametrize("sensor", ["unavailable", "disconnected"])
def test_poller_transient_loss_during_current_job_keeps_material(tmp_path, sensor):
    """Losing the colour sensor / HA mid-job must not erase the material that was
    resolved for the running job (clearing would re-arm heat and could re-pause)."""
    mod = _poller_mod(tmp_path, _ha_entities(), cc2_print_status="idle")
    good = _printing(**{"sensor.cc2_active_filament_color": "#00ff00", **_SLOTS})
    bad = _printing(**{"sensor.cc2_active_filament_color": "unavailable"}) if sensor == "unavailable" else _HA_DOWN
    snaps = _run_poller(mod, [good, bad, bad])
    assert snaps[0]["cc2_pending_filament"] == "ASA" and snaps[0]["chamber_setpoint"] == 55
    for s in snaps[1:]:
        assert s["cc2_pending_filament"] == "ASA"
        assert s["chamber_setpoint"] == 55


def test_poller_missing_entity_mid_job_keeps_material(tmp_path):
    """No colour entity at all, job already running with a buffered material (e.g. from
    MQTT or a restart): polls must not clear it."""
    mod = _poller_mod(tmp_path, _ha_entities(with_color=False), cc2_print_status="printing",
                      cc2_pending_filament="ASA", cc2_resolved_filament="ASA")
    mod.current_data["chamber_setpoint"] = 55.0
    snaps = _run_poller(mod, [_printing(), _printing(), _HA_DOWN])
    for s in snaps:
        assert s["cc2_pending_filament"] == "ASA" and s["cc2_resolved_filament"] == "ASA"
        assert s["chamber_setpoint"] == 55.0


@pytest.mark.parametrize("stale", ["PETG", "ASA"])
def test_poller_explicit_filename_temp_beats_stale_material(tmp_path, stale):
    """Filename-encoded temperature keeps priority over stale material and over the
    unknown fallback, whether or not the colour sensor exists."""
    for with_color in (True, False):
        mod = _poller_mod(tmp_path, _ha_entities(with_color=with_color),
                          cc2_print_status="idle", cc2_pending_filament=stale, cc2_resolved_filament=stale)
        fname = {"sensor.cc2_filename": "petg_benchy_60C.gcode"}
        snaps = _run_poller(mod, [_idle(**fname), _printing(**fname), _printing(**fname)])
        for s in snaps:
            assert s["cc2_pending_filament"] == "FILENAME-60C"
            assert s["chamber_setpoint"] == 60.0


def test_poller_valid_fresh_petg_suppresses_fallback(tmp_path):
    """A fresh PETG reading (colour sensor matched to a slot) for the new job still
    suppresses the unknown fallback; and stale ASA from before is replaced by it."""
    mod = _poller_mod(tmp_path, _ha_entities(), cc2_print_status="idle",
                      cc2_pending_filament="ASA", cc2_resolved_filament="ASA")
    petg = {"sensor.cc2_active_filament_color": "#ff0000", **_SLOTS}
    snaps = _run_poller(mod, [_idle(**petg), _printing(**petg), _printing(**petg)])
    assert snaps[0]["cc2_pending_filament"] == "PETG"
    assert snaps[-1]["cc2_print_status"] == "printing"
    assert snaps[-1]["chamber_setpoint"] == 0            # PETG: no heat, no fallback 45


def test_poller_fresh_asa_reading_after_stale_petg(tmp_path):
    """Fresh ASA for the new job wins over stale PETG and arms the ASA target."""
    mod = _poller_mod(tmp_path, _ha_entities(), cc2_print_status="idle",
                      cc2_pending_filament="PETG", cc2_resolved_filament="PETG")
    asa = {"sensor.cc2_active_filament_color": "#00ff00", **_SLOTS}
    snaps = _run_poller(mod, [_idle(**asa), _printing(**asa)])
    assert snaps[-1]["chamber_setpoint"] == 55
    assert snaps[-1]["cc2_pending_filament"] == "ASA"


@pytest.mark.parametrize("color_state", ["unavailable", _HA_FAIL])
def test_poller_start_cycle_color_glitch_keeps_fresh_petg(tmp_path, color_state):
    """Fresh PETG resolved on an idle poll; on the very poll where the job starts the
    colour read glitches. PETG must survive: no 45 C fallback heating for PETG."""
    mod = _poller_mod(tmp_path, _ha_entities(), cc2_print_status="idle")
    petg = {"sensor.cc2_active_filament_color": "#ff0000", **_SLOTS}
    glitch = {"sensor.cc2_active_filament_color": color_state}
    snaps = _run_poller(mod, [_idle(**petg), _printing(**glitch), _printing(**glitch)])
    assert snaps[0]["cc2_pending_filament"] == "PETG"
    assert snaps[1]["cc2_print_status"] == "printing"
    assert snaps[1]["cc2_pending_filament"] == "PETG"
    assert snaps[-1]["chamber_setpoint"] == 0


def test_poller_missing_entity_keeps_mqtt_material_when_both_sources_run(tmp_path):
    """CC2_IP (MQTT supplies active_filament_type) plus HA mode, but no colour entity:
    the poller is not the sole source, so it must not wipe the MQTT-buffered PETG."""
    mod = _poller_mod(tmp_path, _ha_entities(with_color=False), cc2_ip="192.168.1.50",
                      cc2_print_status="idle")
    mod._handle_cc2_data("active_filament_type", "PETG")   # as the MQTT handler would
    assert mod.current_data["cc2_pending_filament"] == "PETG"
    snaps = _run_poller(mod, [_idle(), _idle(), _printing()])
    assert all(s["cc2_pending_filament"] == "PETG" for s in snaps)
    assert snaps[-1]["cc2_print_status"] == "printing"
    assert snaps[-1]["chamber_setpoint"] == 0


@pytest.mark.parametrize("running,chamber_target", [(True, 0), (True, 55), (False, 55)])
def test_dry_cycle_is_not_stopped_by_chamber_auto_control(tmp_path, running, chamber_target):
    mod = _fresh_loop_mod(tmp_path)
    mod.bind_confirmed = True
    mod.current_data.update(cc2_print_status="idle", chamber_setpoint=chamber_target, slicer_soll=chamber_target, chamber_temp=25)

    async def run():
        ws = _LoopWs(lambda d: False, [{"settings": {"warehouse_temper": 25, "work_mode": 3,
                    "work_on": running, "isrunning": int(running), "filament_temp": 50}}])
        with patch.object(mod.websockets, "connect", lambda *a, **k: _ConnectOnce(ws)), \
                patch.object(mod, "log_event"):
            task = asyncio.create_task(mod.update_limits_from_ws())
            try:
                await asyncio.wait_for(ws.drained.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return ws.log

    messages = asyncio.run(run())
    assert not any("settings" in d for d in messages), messages

@pytest.mark.parametrize('cc2_active', [True, False])
@pytest.mark.parametrize('pending,target', [(True, 0), (False, 55), (True, 55)])
def test_drying_reconnect_preserves_device_control(tmp_path, pending, target, cc2_active):
    mod = _fresh_loop_mod(tmp_path)
    mod.CC2_ACTIVE = cc2_active
    mod.current_data.update(cc2_print_status='printing', chamber_setpoint=target, slicer_soll=target)
    mod._panda_off_pending = pending
    mod._panda_off_pending_at = time.time()
    async def run():
        ws = _LoopWs(lambda d: False, [{'settings': {'work_mode': 3, 'work_on': True, 'isrunning': 1, 'warehouse_temper': 25, 'remaining_seconds': 3600}}])
        with patch.object(mod.websockets, 'connect', lambda *a, **k: _ConnectOnce(ws)), patch.object(mod, 'log_event'):
            task = asyncio.create_task(mod.update_limits_from_ws())
            try:
                await asyncio.wait_for(ws.drained.wait(), 4)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return ws.log
    messages = asyncio.run(run())
    assert not any('settings' in d for d in messages), messages
    assert not mod._panda_off_pending
    if cc2_active:
        assert messages == [], messages
    else:
        assert 'printer' in messages[0], messages
        assert messages[1] == {'get_settings': 1}, messages


def test_print_end_preserves_confirmed_dryer(tmp_path):
    mod = _fresh_loop_mod(tmp_path)
    mod.bind_confirmed = True
    mod.last_ws_settings.update(work_mode=3, work_on=True, isrunning=1, remaining_seconds=3600)
    mod.current_data.update(cc2_print_status='printing', chamber_setpoint=55, slicer_soll=55)
    async def run():
        ws = _LoopWs(lambda d: False, [])
        mod.panda_ws = ws
        mod.main_loop = asyncio.get_running_loop()
        mod._handle_cc2_data('print_status', 'idle')
        await asyncio.sleep(.05)
        return ws.log
    messages = asyncio.run(run())
    assert mod.current_data['chamber_setpoint'] == 0
    assert not any('settings' in d for d in messages), messages
    assert not mod._panda_off_pending


def test_slicer_priority_does_not_restart_finished_dryer(tmp_path):
    mod = _fresh_loop_mod(tmp_path)
    mod.bind_confirmed = True
    mod.last_ws_settings.update(work_mode=3, work_on=False, isrunning=0, remaining_seconds=0)
    mod.current_data.update(chamber_setpoint=55, slicer_soll=55)
    async def run():
        ws = _LoopWs(lambda d: False, [])
        mod.panda_ws = ws
        mod.main_loop = asyncio.get_running_loop()
        mod.on_mqtt_message(None, None, _fake_msg('panda_breath_mod/slicer_priority_mode/set', 'ON'))
        await asyncio.sleep(.05)
        return ws.log
    assert not asyncio.run(run())


def test_full_dry_command_sends_custom_hours_before_start():
    mod, _ = load_panda(cc2_ip='192.168.1.50')
    async def run():
        from unittest.mock import AsyncMock
        mod.panda_ws = SimpleNamespace(send=AsyncMock())
        mod.bind_confirmed = True
        mod.main_loop = asyncio.get_running_loop()
        mod.on_mqtt_message(None,None,_fake_msg('panda_breath_mod/drying/set',json.dumps({'temperature_c':50,'duration_hours':8})))
        await asyncio.sleep(.05)
        return [json.loads(c.args[0]) for c in mod.panda_ws.send.call_args_list]
    packets=asyncio.run(run())
    assert packets == [{'settings':s} for s in ({'isrunning':0},{'filament_temp':50},{'filament_timer':8},{'filament_drying_mode':3},{'work_mode':3},{'work_on':True},{'isrunning':1})] + [{'get_settings':1}]


@pytest.mark.parametrize('hours',[0,100,480,True,'8'])
def test_invalid_full_dry_duration_sends_nothing(hours):
    mod,_=load_panda(cc2_ip='192.168.1.50')
    with patch('asyncio.run_coroutine_threadsafe') as schedule:
        mod.on_mqtt_message(None,None,_fake_msg('panda_breath_mod/drying/set',json.dumps({'temperature_c':50,'duration_hours':hours})))
        schedule.assert_not_called()

@pytest.mark.parametrize('topic,value',[('heat_stop','PRESS'),('panda_power','OFF'),('work_on','OFF')])
def test_stop_cancels_pending_full_dry_before_start(topic,value):
    mod,_=load_panda(cc2_ip='192.168.1.50')
    async def run():
        packets=[]
        async def send(payload):
            d=json.loads(payload);packets.append(d)
            if d.get('settings',{}).get('filament_temp')==50:
                mod.on_mqtt_message(None,None,_fake_msg('panda_breath_mod/'+topic+'/set',value))
        mod.panda_ws=SimpleNamespace(send=send);mod.bind_confirmed=True
        mod.main_loop=asyncio.get_running_loop()
        mod.on_mqtt_message(None,None,_fake_msg('panda_breath_mod/drying/set',json.dumps({'temperature_c':50,'duration_hours':8})))
        await asyncio.sleep(.8)
        return packets
    packets=asyncio.run(run())
    assert not any(p.get('settings',{}).get('isrunning')==1 for p in packets),packets
    assert any(p.get('settings',{}).get('work_on') is False for p in packets),packets


def test_cc2_timeout_listens_without_unsupported_poll(tmp_path):
    mod = _fresh_loop_mod(tmp_path)
    class QuietOnce(_LoopWs):
        quiet = True
        async def recv(self):
            if self.quiet:
                self.quiet = False
                raise asyncio.TimeoutError()
            return await super().recv()
    async def run():
        ws = QuietOnce(lambda d: False, [{'settings': {'work_mode': 3, 'work_on': 1, 'isrunning': 1}}])
        with patch.object(mod.websockets, 'connect', lambda *a, **k: _ConnectOnce(ws)):
            task = asyncio.create_task(mod.update_limits_from_ws())
            try:
                await asyncio.wait_for(ws.drained.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return ws.log
    assert asyncio.run(run()) == []


@pytest.mark.parametrize('cc2_active',[True,False])
def test_locked_connection_stops_without_cc2_printer_rebind(tmp_path,cc2_active):
    mod = _fresh_loop_mod(tmp_path)
    mod.CC2_ACTIVE=cc2_active
    mod.global_lock=True
    async def run():
        packets=[]; stopped=asyncio.Event()
        async def send(payload):
            d=json.loads(payload);packets.append(d)
            if d.get('settings',{}).get('work_on') is False:stopped.set()
        ws=SimpleNamespace(send=send)
        with patch.object(mod.websockets,'connect',lambda *a,**k:_ConnectOnce(ws)):
            task=asyncio.create_task(mod.update_limits_from_ws())
            try:await asyncio.wait_for(stopped.wait(),3)
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
        return packets
    packets=asyncio.run(run())
    assert any(p.get('settings',{}).get('work_on') is False for p in packets)
    assert any('printer' in p for p in packets) is (not cc2_active)


# ═════════════════════════════════════════════════════════════════════════
# Native MQTT device transport integration (openspec native-panda-mqtt, task 2)
#
# With PANDA_DEVICE_TRANSPORT=mqtt the mod commands and observes the Panda
# over the dedicated broker's native protocol instead of the device WebSocket.
# The tests drive the real update_limits_from_ws / on_mqtt_message / press and
# stop handlers with a fake NativePandaTransport (Panda.py seam:
# mod._native_transport_factory) and prove: no persistent device WebSocket is
# ever opened in mqtt mode, reconnect never re-sends a policy command, dry
# start goes through transport.start_drying (never the legacy 7-frame burst),
# set topics forward only custom field updates, retained control inputs are
# ignored, the required status topics are published, and an emergency stop is
# native-first with at most ONE bounded WS fallback frame.
# ═════════════════════════════════════════════════════════════════════════

MQTT_ENV = {
    "PANDA_MQTT_TOPIC_PREFIX": "dummy/panda",
    "PANDA_DEVICE_TRANSPORT": "mqtt",
    "PANDA_DEVICE_MQTT_HOST": "broker.local",
    "PANDA_DEVICE_MQTT_PORT": "1884",
    "PANDA_DEVICE_MQTT_USER": "panda_mod",
    "PANDA_DEVICE_MQTT_ID": "panda_mod_test",
}

SNAPSHOT = {"settings": {
    "warehouse_temper": 25, "set_temp": 50, "work_mode": 2, "work_on": 0,
    "isrunning": 0, "filtertemp": 70, "work_time": 0, "poweroff_time": 0,
    "filament_temp": 50, "filament_timer": 4, "custom_temp": 50,
    "custom_timer": 4, "firmware_version": "1.2.0",
}}


class _FakeNativeTransport:
    """Stands in for panda_native_mqtt.NativePandaTransport in Panda.py tests."""

    def __init__(self, snapshots=None, raise_on_recv=None, dry_running=False):
        self.sent, self.starts = [], []          # send() payloads / start_drying calls
        self.emergency_calls = 0
        self.emergency_result = True
        self.entered = self.exited = 0
        self.recv_calls = 0
        self.available = True
        self.status_age = 0.0
        self.start_status = None
        self.heating_status = None
        self._q = [json.dumps(s) for s in (snapshots if snapshots is not None else [SNAPSHOT])]
        self._last = self._q[0] if self._q else json.dumps(SNAPSHOT)
        self._raise = raise_on_recv
        # supervised-drying seam (drying_supervisor's only transport surface)
        self.dry_running = dry_running
        self._dry_seq = 0
        self.recovers = []                        # recover_drying (temp_c, deadline)
        self.recover_result = "confirmed"
        self.stops = 0                            # stop_supervised calls
        self.stop_result = "confirmed"

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *exc):
        self.exited += 1
        return False

    async def send(self, payload):
        self.sent.append(json.loads(payload))
        return True

    async def recv(self):
        self.recv_calls += 1
        if self._q:
            self._last = self._q.pop(0)
            return self._last
        if self._raise is not None:
            raise self._raise
        # Paced repeat (20 Hz): telemetry keeps flowing without flooding the
        # recording mock publishers into unbounded memory in a tight loop.
        await asyncio.sleep(0.05)
        return self._last

    async def start_drying(self, temperature_c, duration_hours, cancelled=None):
        self.starts.append((temperature_c, duration_hours, cancelled))
        return True

    def drying_snapshot(self):
        """Fresh live native state with a distinct sequence id per call."""
        self._dry_seq += 1
        return self._dry_seq, {
            "mode": "filament drying", "work_on": "ON", "chamber_temp": 25.0,
            "drying_running": "ON" if self.dry_running else "OFF",
            "custom_temp": 50, "custom_timer": 8,
            "drying_remaining_min": 60 if self.dry_running else 0,
        }

    def heating_evidence_status(self):
        if not self.available or self.status_age is None or self.status_age > 10:
            return "unknown"
        snap = self.drying_snapshot()
        if snap is None:
            return "unknown"
        native = snap[1]
        if (native["mode"] != "filament drying" or native["work_on"] != "ON"
                or native["drying_running"] != "ON"):
            return "none"
        return self.heating_status or "none"

    async def recover_drying(self, temp_c, deadline, cancelled=None):
        self.recovers.append((temp_c, deadline))
        return self.recover_result

    async def stop_supervised(self):
        self.stops += 1
        return self.stop_result

    async def emergency_stop(self):
        self.emergency_calls += 1
        return self.emergency_result


class _OneShotWs:
    """Bounded WS fallback fake: records frames, scripted recv, blocks after."""

    def __init__(self, recv_msgs=None, send_error=None):
        self.log = []
        self._q = [json.dumps(m) for m in (recv_msgs or [])]
        self._send_error = send_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, payload):
        if self._send_error:
            raise self._send_error
        self.log.append(json.loads(payload))

    async def recv(self):
        if self._q:
            return self._q.pop(0)
        await asyncio.Event().wait()


def _mqtt_mod(tmp_path, transports, **load_kw):
    """Module in mqtt mode with a scripted list of fake transports.

    The password file only has to exist — the fake factory never reads it, and
    the real factory path is exercised by the fail-closed test instead.
    """
    (tmp_path / "pwpw").write_text("s3cret\n")
    env = dict(MQTT_ENV, PANDA_DEVICE_MQTT_PASSWORD_FILE=str(tmp_path / "pwpw"))
    mod, client = load_panda(cc2_ip="192.168.1.50", env_extra=env, **load_kw)
    mod.mqtt_client = client
    created = []

    def _factory(**kw):
        ft = transports[min(len(created), len(transports) - 1)]
        created.append((ft, kw))
        return ft

    mod._native_transport_factory = _factory
    return mod, client, created


def _run_loop_briefly(mod, until, timeout=5.0):
    """Run update_limits_from_ws until `until()` holds (polling) or timeout."""
    async def run():
        task = asyncio.create_task(mod.update_limits_from_ws())
        try:
            deadline = time.monotonic() + timeout
            while not until() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert until(), "loop never reached the requested state"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def _no_ws_connect():
    def _boom(*a, **k):
        raise AssertionError("mqtt mode must never open the device WebSocket")
    return _boom


def _ws_boom(hits):
    def _boom(*a, **k):
        hits.append(1)
        raise AssertionError("mqtt mode must never open the device WebSocket")
    return _boom


def _pubs(client):
    out = {}
    for c in client.publish.call_args_list:
        out.setdefault(c.args[0], []).append(c.args[1])
    return out


def _dry_session(mod):
    """The persisted supervised session file, or None when nothing is armed."""
    p = Path(mod.DRY_SESSION_FILE)
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def test_mqtt_transport_mode_never_opens_the_device_websocket(tmp_path):
    ft = _FakeNativeTransport()
    mod, client, created = _mqtt_mod(tmp_path, [ft])
    with patch.object(mod.websockets, "connect", _no_ws_connect()), \
         patch.object(mod, "log_event"):
        _run_loop_briefly(mod, lambda: ft.recv_calls >= 1)
    assert created and created[0][0] is ft
    assert created[0][1]["host"] == "broker.local" and created[0][1]["port"] == 1884
    assert created[0][1]["password"] == "s3cret"           # read from the file
    assert mod.panda_ws is ft                              # commands route to it
    assert ft.entered == 1 and ft.sent == []               # no bind/get_settings burst


def test_websocket_mode_default_never_touches_the_native_factory(tmp_path):
    mod, client = load_panda(cc2_ip="192.168.1.50")   # no transport env at all
    assert mod.DEVICE_TRANSPORT == "websocket"
    calls = []
    mod._native_transport_factory = lambda **k: calls.append(k)
    ws = _LoopWs(lambda d: False, [{"printer": "get_settings"}, SNAPSHOT])
    with patch.object(mod.websockets, "connect", lambda *a, **k: _ConnectOnce(ws)), \
         patch.object(mod, "log_event"):
        _run_loop_briefly(mod, lambda: ws.drained.is_set(), timeout=8.0)
    assert calls == []
    assert mod.panda_ws is not None


def test_mqtt_config_without_cc2_active_is_rejected_clearly(tmp_path):
    (tmp_path / "pwpw").write_text("s3cret\n")
    mod, client = load_panda(cc2_ip="", env_extra=dict(
        MQTT_ENV, PANDA_DEVICE_MQTT_PASSWORD_FILE=str(tmp_path / "pwpw")))
    # Explicit MQTT selection is fail-closed even for unsupported legacy mode.
    assert mod.DEVICE_TRANSPORT == "mqtt"
    assert mod.DEVICE_MQTT_CONFIG_OK is False
    assert "requires CC2" in mod.DEVICE_TRANSPORT_REJECT_REASON


def test_mqtt_missing_device_config_fails_closed(tmp_path):
    # No password file, no device id: the mod must NOT fall back to a silent
    # WebSocket; it stays connected to nothing and reports the device OFF.
    mod, client = load_panda(cc2_ip="192.168.1.50", env_extra={
        "PANDA_MQTT_TOPIC_PREFIX": "dummy/panda",
        "PANDA_DEVICE_TRANSPORT": "mqtt",
        "PANDA_DEVICE_MQTT_HOST": "broker.local",
    })
    assert mod.DEVICE_TRANSPORT == "mqtt"
    assert mod.DEVICE_MQTT_CONFIG_OK is False
    hits = []
    with patch.object(mod.websockets, "connect", _ws_boom(hits)), \
         patch.object(mod, "log_event"):
        _run_loop_briefly(mod, lambda: "dummy/panda/device_available" in _pubs(client))
        assert mod.panda_ws is None
    assert hits == []


def test_mqtt_first_snapshot_sends_no_policy_commands(tmp_path):
    # An ASA print with a pending heater-off and a warm target: the legacy
    # loop replays OFF and resumes heating on (re)bind. Native must send
    # NOTHING — a fresh snapshot alone carries the device truth.
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod._panda_off_pending = True
    mod._panda_off_pending_at = time.time()
    _arm_asa_printing(mod, tmp_path)
    mod.cc2_paused_for_preheat = True
    mod.current_data["cc2_chamber_temp"] = 60
    def _replay():
        raise AssertionError("native reconnect must not replay _resend_pending_off")
    mod._resend_pending_off = _replay
    with patch.object(mod.websockets, "connect", _no_ws_connect()), \
         patch.object(mod, "log_event"):
        _run_loop_briefly(mod, lambda: ft.recv_calls >= 2)
    assert ft.sent == [] and ft.starts == []


def test_mqtt_reconnect_invalidates_without_replaying_anything(tmp_path):
    # Transport dies mid-stream: reuse the transport so uncertain starts survive,
    # do not replay pending-off or any heat command, and the
    # except path must not zero native active-cycle data.
    ft1 = _FakeNativeTransport(raise_on_recv=panda_native_mqtt.DeviceUnavailable("gone"))
    ft2 = _FakeNativeTransport()
    mod, client, created = _mqtt_mod(tmp_path, [ft1])
    ft1.start_status = "uncertain"
    mod._panda_off_pending = True
    mod._panda_off_pending_at = time.time()
    _arm_asa_printing(mod, tmp_path)
    def _replay():
        raise AssertionError("native reconnect must not replay _resend_pending_off")
    mod._resend_pending_off = _replay
    with patch.object(mod.websockets, "connect", _no_ws_connect()), \
         patch.object(mod, "log_event"):
        _run_loop_briefly(mod, lambda: ft1.entered >= 2, timeout=15.0)
    assert ft1.exited >= 1 and ft1.entered >= 2
    assert len(created) == 1 and ft1.start_status == "uncertain"
    assert ft1.sent == [] and ft1.starts == []


def test_native_snapshot_publishes_dry_time_as_minutes(tmp_path):
    # custom_timer is HOURS on the native protocol; the mod's existing dry_time
    # number keeps its minutes display.
    ft = _FakeNativeTransport([{"settings": dict(SNAPSHOT["settings"],
                                                 work_mode=3, custom_timer=6)}])
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    with patch.object(mod.websockets, "connect", _no_ws_connect()), \
         patch.object(mod, "log_event"):
        _run_loop_briefly(mod, lambda: ft.recv_calls >= 1)
    pubs = _pubs(client)
    assert pubs["dummy/panda/dry_time"][-1] == 360
    assert mod.current_data["filament_timer"] == 360


@pytest.mark.parametrize("cancel_topic,cancel_value", [("manual", "PRESS"), ("auto", "PRESS"), ("mode_select", "Manual")])
def test_mqtt_dry_press_json_arms_supervisor_never_a_frame_burst(tmp_path, cancel_topic, cancel_value):
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.mqtt_client = client
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    mod._cc2_press_button = MagicMock()
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg(
            "dummy/panda/drying/set",
            json.dumps({"enabled": True, "duration_hours": 8, "temperature_c": 52})))
    for coro in captured:
        with patch.object(mod, "log_event"):
            asyncio.run(coro)
    # The supervisor is the sole owner of the supervised start: nothing starts
    # directly and no legacy frame burst — the session is persisted before any
    # device write, and the supervisor performs the first ON itself.
    assert ft.starts == [] and ft.recovers == [] and ft.sent == []
    session = _dry_session(mod)
    assert session and session["status"] == "running" and session["target_c"] == 52
    assert session["deadline"] == pytest.approx(time.time() + 8 * 3600, abs=15)
    assert session["pending_stop"] is False and session["session_id"]
    pending = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(pending)):
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/" + cancel_topic + "/set", cancel_value))
    for coro in pending:
        coro.close()
    # mode changes cancel the supervised session first, persisting the stop
    # obligation before any device command runs
    session = _dry_session(mod)
    assert session["status"] == "cancelled" and session["pending_stop"] is True
    assert ft.sent == [] and ft.starts == []     # no legacy settings frames either


def test_mqtt_press_without_json_uses_current_settings_minutes_as_hours(tmp_path):
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    mod.current_data.update(filament_temp=55, filament_timer=90)   # not whole hours
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/drying/set", ""))
    for coro in captured:
        with patch.object(mod, "log_event"):
            asyncio.run(coro)
    # A duration that cannot be represented in whole hours is refused outright:
    # no session at all, nothing written.
    assert _dry_session(mod) is None
    assert ft.starts == [] and ft.recovers == [] and ft.sent == []
    mod.current_data["filament_timer"] = 180                       # 3 whole hours
    captured.clear()
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/drying/set", ""))
    for coro in captured:
        with patch.object(mod, "log_event"):
            asyncio.run(coro)
    session = _dry_session(mod)
    assert session and session["target_c"] == 55
    assert session["deadline"] == pytest.approx(time.time() + 3 * 3600, abs=15)
    assert ft.starts == [] and ft.recovers == [] and ft.sent == []


def test_mqtt_explicit_json_duration_hours_is_canonical(tmp_path):
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    mod.current_data.update(filament_temp=55, filament_timer=120)
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg(
            "dummy/panda/drying/set",
            json.dumps({"enabled": True, "duration_hours": 8, "temperature_c": 50})))
    for coro in captured:
        with patch.object(mod, "log_event"):
            asyncio.run(coro)
    session = _dry_session(mod)
    assert session["target_c"] == 50                                # JSON wins over current settings
    assert session["deadline"] == pytest.approx(time.time() + 8 * 3600, abs=15)
    assert ft.starts == [] and ft.sent == []


def test_mqtt_supervisor_owns_first_start_and_second_press_moves_nothing(tmp_path):
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod._cc2_press_button = MagicMock()

    async def run():
        mod.main_loop = asyncio.get_running_loop()
        mod.DRY_SUPERVISE_TICK_S = 0.02
        mod.on_mqtt_message(client, None, _fake_msg(
            "dummy/panda/drying/set",
            json.dumps({"enabled": True, "duration_hours": 8, "temperature_c": 52})))
        await asyncio.sleep(0.05)
        deadline0 = _dry_session(mod)["deadline"]
        task = asyncio.create_task(mod.supervised_drying_loop())
        try:
            stop_t = time.monotonic() + 5
            while not ft.recovers and time.monotonic() < stop_t:
                await asyncio.sleep(0.01)
            assert ft.recovers == [(52, deadline0)], ft.recovers   # supervisor's own start
            assert ft.starts == []                                  # never the direct flow
            # second explicit press: rejected, the fixed deadline is not moved
            mod.on_mqtt_message(client, None, _fake_msg(
                "dummy/panda/drying/set",
                json.dumps({"enabled": True, "duration_hours": 8, "temperature_c": 52})))
            await asyncio.sleep(0.1)
            assert _dry_session(mod)["deadline"] == deadline0
            assert len(ft.recovers) == 1
            # deadline/status/pending_stop mirror through existing MQTT telemetry
            mirror = [json.loads(s) for s in _pubs(client)["dummy/panda/dry_session"]]
            assert mirror and mirror[-1]["status"] == "running"
            assert mirror[-1]["deadline"] == deadline0
            # work_on ON (chamber power) does NOT cancel a supervised session
            mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/work_on/set", "ON"))
            await asyncio.sleep(0.1)
            assert _dry_session(mod)["status"] == "running"
            # mod Stop: cancels first (persisted), then the owed stop is delivered
            ft.stop_result = "uncertain"
            mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/heat_stop/set", "PRESS"))
            await asyncio.sleep(0.1)
            session = _dry_session(mod)
            assert session["status"] == "cancelled" and session["pending_stop"] is True
            stop_t = time.monotonic() + 2
            while ft.stops == 0 and time.monotonic() < stop_t:
                await asyncio.sleep(0.01)
            assert ft.stops >= 1
            assert _dry_session(mod)["pending_stop"] is True        # uncertain → kept
            ft.stop_result = "confirmed"
            stop_t = time.monotonic() + 2
            while _dry_session(mod)["pending_stop"] and time.monotonic() < stop_t:
                await asyncio.sleep(0.01)
            assert _dry_session(mod)["pending_stop"] is False
            assert len(ft.recovers) == 1                            # never rearms
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_mqtt_supervised_start_requires_fresh_idle_state(tmp_path):
    # fresh telemetry showing drying already running: the request is refused
    # before any write and NO session is armed (no adoption of a foreign cycle)
    ft = _FakeNativeTransport(dry_running=True)
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg(
            "dummy/panda/drying/set",
            json.dumps({"enabled": True, "duration_hours": 8, "temperature_c": 50})))
    for coro in captured:
        with patch.object(mod, "log_event"):
            asyncio.run(coro)
    assert _dry_session(mod) is None
    assert ft.starts == [] and ft.recovers == [] and ft.sent == []


def test_queued_dry_flow_never_arms_after_an_intervening_stop(tmp_path):
    """The captured generation is re-checked IMMEDIATELY before arming.

    A Stop processed while the drying flow was still queued must leave no
    session persisted and no device write — the older queued flow may not arm
    over a cancellation it was queued before."""
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    mod._cc2_press_button = MagicMock()
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg(
            "dummy/panda/drying/set",
            json.dumps({"enabled": True, "duration_hours": 8, "temperature_c": 52})))
    assert len(captured) == 1                       # the start is queued, not run
    later = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(later)):
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/work_on/set", "OFF"))
    for coro in later:
        coro.close()                                # this Stop's own write is not the check
    # now execute the OLDER queued drying flow: it must refuse
    with patch.object(mod, "log_event"):
        asyncio.run(captured[0])
    assert _dry_session(mod) is None                # nothing armed, nothing to stop
    assert ft.starts == [] and ft.recovers == [] and ft.sent == []
    # ... and a plain start afterwards still works (the guard is not stuck)
    fresh = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(fresh)):
        mod.on_mqtt_message(client, None, _fake_msg(
            "dummy/panda/drying/set",
            json.dumps({"enabled": True, "duration_hours": 8, "temperature_c": 52})))
    with patch.object(mod, "log_event"):
        asyncio.run(fresh[0])
    session = _dry_session(mod)
    assert session and session["status"] == "running" and session["target_c"] == 52


def test_malformed_dry_session_file_imports_and_logs_its_rejection(tmp_path, capsys):
    """The supervisor loads its file during import and LOGS while rejecting a
    corrupt record, so it must only be constructed once log_event exists: the
    rejection has to be reported (not swallowed) and the session must simply
    stay inactive — never a crash of the module import."""
    (tmp_path / "pwpw").write_text("s3cret\n")
    bad = tmp_path / "drying_session.json"
    bad.write_text("{not json at all")
    env = dict(MQTT_ENV, PANDA_DEBUG="true",
               PANDA_DEVICE_MQTT_PASSWORD_FILE=str(tmp_path / "pwpw"),
               PANDA_DRY_SESSION_FILE=str(bad))
    mod, client = load_panda(cc2_ip="192.168.1.50", env_extra=env)   # must not raise
    assert "persisted session unreadable" in capsys.readouterr().out   # logger existed
    assert mod._drying_supervisor.status()["status"] == "inactive"
    ft = _FakeNativeTransport()
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg(
            "dummy/panda/drying/set",
            json.dumps({"enabled": True, "duration_hours": 8, "temperature_c": 50})))
    with patch.object(mod, "log_event"):
        asyncio.run(captured[0])
    # an unusable persisted record does not block a fresh explicit request
    session = _dry_session(mod)
    assert session and session["status"] == "running"


def test_dry_session_state_file_honours_env_and_defaults_to_base_dir(tmp_path):
    custom = tmp_path / "custom-dry-session.json"
    env = dict(MQTT_ENV, PANDA_DEVICE_MQTT_PASSWORD_FILE=str(tmp_path / "pwpw"),
               PANDA_DRY_SESSION_FILE=str(custom))
    (tmp_path / "pwpw").write_text("s3cret\n")
    mod, _ = load_panda(cc2_ip="192.168.1.50", env_extra=env)
    assert Path(mod.DRY_SESSION_FILE) == custom
    mod2, _, _ = _mqtt_mod(tmp_path, [_FakeNativeTransport()])
    assert Path(mod2.DRY_SESSION_FILE) == mod2.PREHEAT_STATE_FILE.parent / "drying_session.json"


def test_mqtt_dry_number_sets_forward_only_custom_field_updates(tmp_path):
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/dry_temp/set", "48"))
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/dry_time/set", "180"))
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/dry_time/set", "90"))
    for coro in captured:
        with patch.object(mod, "log_event"):
            asyncio.run(coro)
    # Minutes must land as whole hours; 90 min is not representable → not sent.
    assert {"settings": {"filament_temp": 48}} in ft.sent
    assert {"settings": {"filament_timer": 3}} in ft.sent
    assert len(ft.sent) == 2
    assert all("work_on" not in s.get("settings", {}) and
               "isrunning" not in s.get("settings", {}) for s in ft.sent)


def test_mqtt_retained_control_inputs_are_ignored(tmp_path):
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    captured = []
    msg = _fake_msg("dummy/panda/drying/set",
                    json.dumps({"enabled": True, "duration_hours": 8, "temperature_c": 52}))
    msg.retain = True
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, msg)
    assert captured == []                          # no flow scheduled at all
    assert ft.starts == [] and ft.sent == []


def test_mqtt_status_topics_are_published(tmp_path):
    ft = _FakeNativeTransport(dry_running=True)
    ft.available = True
    ft.status_age = 1.5
    ft.start_status = "uncertain"
    ft.heating_status = "pending"
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    with patch.object(mod.websockets, "connect", _no_ws_connect()), \
         patch.object(mod, "log_event"):
        _run_loop_briefly(mod, lambda: ft.recv_calls >= 1)
    pubs = _pubs(client)
    assert pubs["dummy/panda/device_transport"][-1] == "mqtt"
    assert pubs["dummy/panda/device_available"][-1] == "ON"
    assert pubs["dummy/panda/device_status_age"][-1] == "1.5"
    assert pubs["dummy/panda/dry_start_status"][-1] == "uncertain"
    assert pubs["dummy/panda/dry_heating_confirmed"][-1] == "pending"


def _run_mqtt_heat_stop(mod, client, ft, topic="dummy/panda/heat_stop/set"):
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg(topic, "ON"))
    with patch.object(mod, "log_event") as log:
        for coro in captured:
            asyncio.run(coro)
    return log


def test_mqtt_emergency_stop_is_native_first_with_no_websocket(tmp_path):
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    with patch.object(mod.websockets, "connect", _no_ws_connect()):
        _run_mqtt_heat_stop(mod, client, ft)
    assert ft.emergency_calls == 1
    assert ft.sent == []


def test_mqtt_emergency_falls_back_to_one_bounded_ws_frame(tmp_path):
    # Native unconfirmed → exactly one WS connection sending ONLY the emergency
    # off frame, and confirming OFF from a device echo when it can.
    ft = _FakeNativeTransport()
    ft.emergency_result = False
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    ws = _OneShotWs(recv_msgs=[{"settings": {"work_on": False}}])
    with patch.object(mod.websockets, "connect", lambda *a, **k: _ConnectOnce(ws)), \
         patch("asyncio.sleep", _instant_sleep):
        log = _run_mqtt_heat_stop(mod, client, ft)
    assert ft.emergency_calls == 1
    assert ws.log == [{"settings": {"isrunning": 0, "work_on": False, "set_temp": 0}}]
    msgs = " ".join(str(c.args[0]) for c in log.call_args_list if c.args)
    assert "Device reports power disabled via WebSocket" in msgs
    assert "physical relay state unavailable" in msgs
    assert "UNCONFIRMED" not in msgs


def test_mqtt_emergency_reports_unconfirmed_when_both_transports_fail(tmp_path):
    # The mod must never claim the heater is off in that state.
    ft = _FakeNativeTransport()
    ft.emergency_result = False
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    ws = _OneShotWs()
    with patch.object(mod.websockets, "connect", lambda *a, **k: _ConnectOnce(ws)), \
         patch("asyncio.sleep", _instant_sleep):
        log = _run_mqtt_heat_stop(mod, client, ft, topic="dummy/panda/heat_stop/set")
    msgs = " ".join(str(c.args[0]) for c in log.call_args_list if c.args)
    assert "UNCONFIRMED" in msgs
    pubs = _pubs(client)
    assert any("UNCONFIRMED" in str(p) for p in pubs.get("dummy/panda/status", []))
    assert ws.log == [{"settings": {"isrunning": 0, "work_on": False, "set_temp": 0}}]


def test_mqtt_emergency_off_falls_back_when_transport_is_down(tmp_path):
    # Locked/transport-down at stop time: panda_ws None must not skip the stop;
    # the bounded fallback frame is still the only thing that touches a socket.
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = None
    mod.main_loop = MagicMock()
    ws = _OneShotWs(recv_msgs=[{"settings": {"work_on": False}}])
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)), \
         patch.object(mod.websockets, "connect", lambda *a, **k: _ConnectOnce(ws)), \
         patch("asyncio.sleep", _instant_sleep), \
         patch.object(mod, "log_event"):
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/heat_stop/set", "ON"))
        for coro in captured:
            asyncio.run(coro)
    assert ws.log == [{"settings": {"isrunning": 0, "work_on": False, "set_temp": 0}}]
    assert ft.emergency_calls == 0


def test_mqtt_global_lock_never_enters_the_legacy_ws_off_loop(tmp_path):
    # While locked with no transport up, the loop must not spin WS-off writes.
    ft = _FakeNativeTransport()
    mod, client, created = _mqtt_mod(tmp_path, [ft])
    mod.global_lock = True
    hits = []
    with patch.object(mod.websockets, "connect", _ws_boom(hits)), \
         patch.object(mod, "log_event"):
        async def run():
            task = asyncio.create_task(mod.update_limits_from_ws())
            await asyncio.sleep(0.2)          # a real yield so the loop spins
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())
    assert hits == []
    assert created == []
    assert mod.panda_ws is None


def test_mqtt_global_lock_still_consumes_device_telemetry(tmp_path):
    # OFF confirmation needs ingestion even while locked: with a live
    # transport the recv loop keeps being drained, and no settings frame is
    # ever written from the loop.
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    with patch.object(mod.websockets, "connect", _no_ws_connect()), \
         patch.object(mod, "log_event"):
        async def run():
            task = asyncio.create_task(mod.update_limits_from_ws())
            try:
                deadline = time.monotonic() + 10
                while ft.recv_calls < 1 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert ft.recv_calls >= 1, "transport never streamed"
                mod.global_lock = True          # lock while connected
                while ft.recv_calls < 3 and time.monotonic() < deadline + 10:
                    await asyncio.sleep(0.01)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())
    assert ft.recv_calls >= 3
    assert ft.sent == []


@pytest.mark.parametrize("saved", ["not json", json.dumps({"written_at": 0})])
def test_startup_handles_invalid_or_stale_preheat_state(saved):
    state_file = current_state_dir() / "preheat_state.json"
    state_file.write_text(saved)
    mod, _ = load_panda()
    assert mod.cc2_paused_for_preheat is False


@pytest.mark.parametrize("proof,expected_heat", [
    ("none", "OFF"), ("unknown", "OFF"), ("confirmed", "ON"),
    ("pending", "OFF"), ("at_target", "OFF"), ("unconfirmed", "OFF"),
    ("missing_interface", "OFF"),
])
def test_native_heat_report_rejects_obsolete_proof(tmp_path, proof, expected_heat):
    ft = _FakeNativeTransport([
        {"settings": dict(SNAPSHOT["settings"], work_mode=3,
                          warehouse_temper=15, isrunning=proof not in ("none", "unknown"))}
    ], dry_running=proof != "none")
    ft.heating_status = proof if proof in ("pending", "at_target", "unconfirmed") else "confirmed"
    ft.available = proof != "unknown"
    ft.status_age = 0.0
    if proof == "missing_interface":
        ft.heating_evidence_status = None
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    # A real terminal session, with its owed Stop confirmed before reporting.
    wall, mono = [time.time()], [1000.0]
    sup = mod.drying_supervisor.DryingSupervisor(
        mod.DRY_SESSION_FILE, clock=lambda: wall[0], monotonic=lambda: mono[0])
    mod._drying_supervisor = sup
    assert sup.arm(50, 1)
    wall[0] += 3600
    mono[0] += 3600
    asyncio.run(sup.tick(ft, lambda: False))
    before = sup.status()
    assert before["status"] == "completed" and before["pending_stop"] is False
    saved = Path(mod.DRY_SESSION_FILE).read_bytes()
    writes = (list(ft.sent), list(ft.starts), list(ft.recovers), ft.stops)
    with patch.object(mod.websockets, "connect", _no_ws_connect()), patch.object(mod, "log_event"):
        _run_loop_briefly(mod, lambda: "dummy/panda/heating" in _pubs(client))
    pubs = _pubs(client)
    assert pubs["dummy/panda/heating"][-1] == expected_heat
    assert pubs["dummy/panda/dry_heating_confirmed"][-1] == (
        "unknown" if proof == "missing_interface" else proof)
    assert sup.status() == before
    assert Path(mod.DRY_SESSION_FILE).read_bytes() == saved
    assert (ft.sent, ft.starts, ft.recovers, ft.stops) == writes


def test_native_heat_report_clears_after_mod_stop_while_locked(tmp_path):
    """A locked receive loop must retract retained Heat after real mod Stop."""
    running = {"settings": dict(SNAPSHOT["settings"], work_mode=3,
                                work_on=1, isrunning=1)}

    class PacedTransport(_FakeNativeTransport):
        async def recv(self):
            self.recv_calls += 1
            snapshot = await self.telemetry.get()
            self.dry_running = bool(snapshot["settings"]["isrunning"])
            return json.dumps(snapshot)

        async def emergency_stop(self):
            # Observe real durable cancellation at the device-command boundary.
            cancelled = _dry_session(mod)
            assert cancelled["status"] == "cancelled"
            assert cancelled["pending_stop"] is True
            assert cancelled["session_id"] == armed["session_id"]
            assert cancelled["deadline"] == armed["deadline"]
            return await super().emergency_stop()

    ft = PacedTransport(dry_running=True)
    ft.heating_status = "confirmed"
    mod, client, created = _mqtt_mod(tmp_path, [ft])
    sup = mod.drying_supervisor.DryingSupervisor(
        mod.DRY_SESSION_FILE, clock=lambda: 1800000000.0, monotonic=lambda: 1000.0)
    mod._drying_supervisor = sup
    assert sup.arm(50, 1)
    armed = _dry_session(mod)

    async def wait_for(predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert predicate(), "receive loop never published the requested proof"

    async def run():
        mod.main_loop = asyncio.get_running_loop()
        ft.telemetry = asyncio.Queue()
        ft.telemetry.put_nowait(running)
        task = asyncio.create_task(mod.update_limits_from_ws())
        try:
            await wait_for(lambda: _pubs(client).get("dummy/panda/heating") == ["ON"]
                           and _pubs(client).get("dummy/panda/dry_heating_confirmed") == ["confirmed"])
            assert not mod.global_lock and not mod.heating_locked
            captured = []
            with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
                mod.on_mqtt_message(client, None, _fake_msg(
                    "dummy/panda/heat_stop/set", "PRESS", retain=False))
            assert len(captured) == 1
            await captured[0]
            assert mod.global_lock and mod.heating_locked
            assert ft.emergency_calls == 1
            stopped = sup.status()
            assert stopped["status"] == "cancelled" and stopped["pending_stop"] is True
            assert stopped["session_id"] == armed["session_id"]
            assert stopped["deadline"] == armed["deadline"]
            saved = Path(mod.DRY_SESSION_FILE).read_bytes()
            commands = (list(ft.sent), list(ft.starts), list(ft.recovers),
                        ft.stops, ft.emergency_calls)
            assert commands == ([], [], [], 0, 1)
            proof_count = len(_pubs(client)["dummy/panda/dry_heating_confirmed"])
            ft.telemetry.put_nowait({"settings": dict(
                running["settings"], work_on=0, isrunning=0, work_time=0)})
            # Synchronize on OFF telemetry's proof, never on the desired Heat.
            await wait_for(lambda: len(_pubs(client)["dummy/panda/dry_heating_confirmed"]) > proof_count
                           and _pubs(client)["dummy/panda/dry_heating_confirmed"][-1] == "none")
            assert ft.dry_running is False
            assert mod.global_lock and mod.heating_locked
            assert mod.panda_ws is ft and ft.entered == 1 and ft.exited == 0
            assert len(created) == 1
            assert sup.status() == stopped
            assert Path(mod.DRY_SESSION_FILE).read_bytes() == saved
            assert (ft.sent, ft.starts, ft.recovers, ft.stops, ft.emergency_calls) == commands
            mod._cc2_press_button.assert_not_called()
            for publication in client.publish.call_args_list:
                if publication.args[0] in ("dummy/panda/heating", "dummy/panda/dry_heating_confirmed"):
                    assert publication.kwargs["retain"] is True
            assert _pubs(client)["dummy/panda/dry_heating_confirmed"][-1] == "none"
            assert _pubs(client)["dummy/panda/heating"][-1] == "OFF"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    with patch.object(mod.websockets, "connect", _no_ws_connect()), \
         patch.object(mod, "_cc2_press_button"), patch.object(mod, "log_event"):
        asyncio.run(run())


def test_native_dry_temperature_rejection_is_atomic(tmp_path):
    ft = _FakeNativeTransport()
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/dry_temp/set", "48"))
    for coro in captured:
        asyncio.run(coro)
    captured.clear()
    ft.sent.clear()
    prior_pubs = list(_pubs(client)["dummy/panda/dry_temp"])
    for value in ("80", "-5", "0", "39.9", "60.9", "nan", "inf", "-inf"):
        try:
            with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
                mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/dry_temp/set", value))
            assert mod.current_data["filament_temp"] == 48
            assert _pubs(client)["dummy/panda/dry_temp"] == prior_pubs
            assert captured == [] and ft.sent == []
            assert _dry_session(mod) is None
        finally:
            for coro in captured:
                coro.close()
            captured.clear()
    for value, expected in (("40", 40), ("48", 48), ("60", 60), ("48.9", 48)):
        with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
            mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/dry_temp/set", value))
        for coro in captured:
            asyncio.run(coro)
        captured.clear()
        assert ft.sent[-1] == {"settings": {"filament_temp": expected}}
        assert mod.current_data["filament_temp"] == expected
        assert _pubs(client)["dummy/panda/dry_temp"][-1] == expected
    assert len(ft.sent) == 4 and ft.starts == [] and ft.recovers == []
    assert _dry_session(mod) is None
    mod.setup_mqtt_discovery(client)
    configs = [json.loads(c.args[1]) for c in client.publish.call_args_list
               if c.args[0].startswith("homeassistant/number/")]
    dry = next(c for c in configs if c["object_id"] == "panda_dry_temp")
    assert (dry["min"], dry["max"]) == (40, 60)
    legacy, legacy_client = load_panda()
    legacy.setup_mqtt_discovery(legacy_client)
    configs = [json.loads(c.args[1]) for c in legacy_client.publish.call_args_list
               if c.args[0].startswith("homeassistant/number/")]
    dry = next(c for c in configs if c["object_id"] == "panda_dry_temp")
    assert (dry["min"], dry["max"]) == (1, 80)


@pytest.mark.parametrize("condition", ["dry", "running", "pending_stop", "stale", "uncertain_dry"])
def test_native_work_on_cannot_bypass_dry_owner(tmp_path, condition):
    ft = _FakeNativeTransport()
    native = {"mode": "filament drying" if condition in ("dry", "uncertain_dry") else "auto mode",
              "work_on": "OFF", "drying_running": "OFF", "chamber_temp": 25,
              "custom_temp": 50, "custom_timer": 8, "drying_remaining_min": 0}
    ft.drying_snapshot = lambda: (1, dict(native)) if ft.available else None
    if condition == "uncertain_dry":
        ft.start_status = "uncertain"
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    if condition in ("running", "pending_stop"):
        assert mod._drying_supervisor.arm(50, 1)
        if condition == "pending_stop":
            assert mod._drying_supervisor.cancel()
    before = mod._drying_supervisor.status()
    saved = Path(mod.DRY_SESSION_FILE).read_bytes() if Path(mod.DRY_SESSION_FILE).exists() else None
    captured = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(captured)):
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/work_on/set", "ON"))
    if condition == "stale":
        ft.available = False  # freshness must be checked at execution, not enqueue
    assert len(captured) == 1
    asyncio.run(captured[0])
    assert ft.sent == [] and ft.starts == [] and ft.recovers == [] and ft.stops == 0
    assert "1" not in _pubs(client).get("dummy/panda/work_on", [])
    assert mod._drying_supervisor.status() == before
    assert (Path(mod.DRY_SESSION_FILE).read_bytes() if Path(mod.DRY_SESSION_FILE).exists() else None) == saved


@pytest.mark.parametrize("cancel", ["off", "lock"])
def test_native_work_on_queued_before_cancellation_cannot_land(tmp_path, cancel):
    ft = _FakeNativeTransport()
    ft.drying_snapshot = lambda: (1, {"mode": "auto mode", "work_on": "OFF", "drying_running": "OFF"})
    mod, client, _ = _mqtt_mod(tmp_path, [ft])
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    queued, later = [], []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(queued)):
        mod.on_mqtt_message(client, None, _fake_msg("dummy/panda/work_on/set", "ON"))
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(later)):
        mod.on_mqtt_message(client, None, _fake_msg(
            "dummy/panda/work_on/set" if cancel == "off" else "dummy/panda/heat_stop/set",
            "OFF" if cancel == "off" else "PRESS"))
    for coro in later:
        coro.close()  # stop persistence/order has existing coverage; execute only old ON
    asyncio.run(queued[0])
    assert ft.sent == [] and ft.starts == [] and ft.recovers == []
    assert "1" not in _pubs(client).get("dummy/panda/work_on", [])
    assert _dry_session(mod) is None


@pytest.mark.parametrize("transport", ["mqtt", "websocket"])
def test_native_work_on_non_dry_preserves_legacy_envelope(tmp_path, transport):
    ft = _FakeNativeTransport()
    ft.drying_snapshot = lambda: (1, {"mode": "auto mode", "work_on": "OFF", "drying_running": "OFF"})
    if transport == "mqtt":
        mod, client, _ = _mqtt_mod(tmp_path, [ft])
    else:
        mod, client = load_panda()
    mod.panda_ws = ft
    mod.main_loop = MagicMock()
    queued = []
    with patch("asyncio.run_coroutine_threadsafe", _capture_scheduler(queued)):
        mod.on_mqtt_message(client, None, _fake_msg(f"{mod.MQTT_TOPIC_PREFIX}/work_on/set", "ON"))
    asyncio.run(queued[0])
    assert ft.sent == ([{"settings": {"work_on": 1}}] if transport == "mqtt" else
                       [{"settings": {"work_on": 1, "isrunning": 1}}])
    assert _pubs(client)[f"{mod.MQTT_TOPIC_PREFIX}/work_on"][-1] == "1"
    assert _dry_session(mod) is None and ft.starts == [] and ft.recovers == []
