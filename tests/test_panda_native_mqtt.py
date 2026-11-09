"""Tests for panda_native_mqtt — the dedicated authenticated device-broker transport.

The device path never touches Home Assistant: the transport talks to its own
broker with its own client, pinned to one device id. No real broker, device or
network is used: paho is injected via client_factory and time via clock.
"""
import asyncio
import json
import math
import time

import pytest

from panda_native_mqtt import (
    DeviceUnavailable,
    NativePandaTransport,
    TOPIC_AVAILABILITY_SUFFIX,
    TOPIC_COMMAND_SUFFIX,
    TOPIC_STATE_SUFFIX,
)

DEV = "btt_panda_aabbcc"
CMD = f"panda_breath/{DEV}/{TOPIC_COMMAND_SUFFIX}"
STA = f"panda_breath/{DEV}/{TOPIC_STATE_SUFFIX}"
AVL = f"panda_breath/{DEV}/{TOPIC_AVAILABILITY_SUFFIX}"


class FakePublish:
    def __init__(self, rc=0):
        self.rc = rc


class FakeClient:
    """Stands in for a paho client: records calls, lets tests fire callbacks."""

    instances = []

    def __init__(self, publish_rc=0):
        self.publish_rc = publish_rc
        self.published = []          # (topic, payload, kwargs)
        self.subscriptions = []      # (topic, kwargs)
        self.connected = False
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.credentials = None
        self.connect_args = None
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        FakeClient.instances.append(self)

    # -- paho surface used by the transport -------------------------------
    def username_pw_set(self, user, password):
        self.credentials = (user, password)

    def connect_async(self, host, port, keepalive=60):
        self.connect_args = (host, port, keepalive)
        return 0

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self, force=False):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True
        self.connected = False

    def subscribe(self, topic, *a, **k):
        self.subscriptions.append((topic, k))
        return (0, (1,))

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, {"qos": qos, "retain": retain}))
        return FakePublish(self.publish_rc)

    # -- test-side firing --------------------------------------------------
    def fire_connect(self, rc=0):
        self.connected = rc == 0
        self.on_connect(self, None, None, rc)

    def fire_disconnect(self, rc=0):
        self.connected = False
        self.on_disconnect(self, None, rc)

    def fire_state(self, payload, retain=False, topic=STA):
        msg = FakeMsg(topic, payload if isinstance(payload, (bytes, bytearray))
                      else json.dumps(payload).encode(), retain)
        self.on_message(self, None, msg)


class FakeMsg:
    def __init__(self, topic, payload, retain=False):
        self.topic = topic
        self.payload = payload
        self.retain = retain


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def full_state(**over):
    """A valid full native V1.0.4 snapshot."""
    s = {
        "chamber_temp": 24.5,
        "target_temp": 0,
        "mode": "auto mode",
        "work_on": "OFF",
        "drying_running": "OFF",
        "custom_temp": 50,
        "custom_timer": 4,
        "drying_remaining_min": 0,
        "filter_temp": 30,
        "heater_temp": 0,
    }
    s.update(over)
    return s


def make_transport(client=None, clock=None, device_id=DEV, **kw):
    client = client or FakeClient()
    clock = clock or FakeClock()
    tr = NativePandaTransport("127.0.0.1", 1884, "user", "secret", device_id,
                              client_factory=lambda: client, clock=clock, **kw)
    return tr, client, clock


async def connected_transport(**kw):
    """Enter the transport, broker connected, one fresh live snapshot ingested."""
    tr, client, clock = make_transport(**kw)
    await tr.__aenter__()
    client.fire_connect(0)
    await asyncio.sleep(0)
    client.fire_state(full_state())
    await asyncio.sleep(0)
    return tr, client, clock


async def stop_transport(tr):
    await tr.__aexit__(None, None, None)


# ═════════════════════════════ Constructor / connection ═════════════════════

def test_constructor_does_not_connect():
    client = FakeClient()
    tr, _, _ = make_transport(client=client)
    assert client.published == [] and client.subscriptions == []
    assert not client.loop_started and client.connect_args is None
    assert tr.device_id == DEV


def test_requires_host_and_device_id():
    with pytest.raises(ValueError):
        NativePandaTransport("", 1883, "u", "p", DEV)
    with pytest.raises(ValueError):
        NativePandaTransport("h", 1883, "u", "p", "")


def test_enter_connects_clean_session_and_subscribes_exact_topics():
    async def scenario():
        tr, client, _ = make_transport()
        await tr.__aenter__()
        assert client.connect_args is not None
        assert client.connect_args[0] == "127.0.0.1"
        assert client.connect_args[1] == 1884
        assert client.credentials == ("user", "secret")
        assert client.loop_started
        client.fire_connect(0)          # paho subscribes from on_connect
        await asyncio.sleep(0)
        topics = [t for t, _ in client.subscriptions]
        assert sorted(topics) == sorted([STA, AVL]), topics
        for t, k in client.subscriptions:
            assert "*" not in t and "+" not in t
        await tr.__aexit__(None, None, None)
    asyncio.run(scenario())


def test_exit_disconnects_and_stops_loop():
    async def scenario():
        tr, client, _ = make_transport()
        await tr.__aenter__()
        client.fire_connect(0)
        await tr.__aexit__(None, None, None)
        assert client.disconnected and client.loop_stopped
    asyncio.run(scenario())


# ═════════════════════════════ State ingestion ══════════════════════════════

def test_valid_full_snapshot_establishes_fresh_state():
    async def scenario():
        tr, client, clock = await connected_transport()
        assert tr.available is True
        assert tr.status_age == 0.0
        clock.advance(4.0)
        assert tr.status_age == pytest.approx(4.0)
        assert tr.available is True
        await stop_transport(tr)
    asyncio.run(scenario())


def test_incomplete_snapshot_does_not_establish_control():
    async def scenario():
        tr, client, _ = make_transport()
        await tr.__aenter__()
        client.fire_connect(0)
        partial = full_state()
        del partial["drying_remaining_min"]
        client.fire_state(partial)
        await asyncio.sleep(0)
        assert tr.available is False
        await stop_transport(tr)
    asyncio.run(scenario())


def test_retained_state_never_establishes_freshness():
    async def scenario():
        tr, client, _ = make_transport()
        await tr.__aenter__()
        client.fire_connect(0)
        client.fire_state(full_state(), retain=True)
        await asyncio.sleep(0)
        assert tr.available is False
        # a later live packet makes it fresh
        client.fire_state(full_state())
        await asyncio.sleep(0)
        assert tr.available is True
        await stop_transport(tr)
    asyncio.run(scenario())


def test_other_device_topics_ignored():
    async def scenario():
        tr, client, _ = await connected_transport()
        before = tr.status_age
        client.fire_state(full_state(chamber_temp=99.0), topic="panda_breath/OTHERDEV/state")
        await asyncio.sleep(0)
        assert tr.status_age == pytest.approx(before)
        env = json.loads(await asyncio.wait_for(tr.poll_latest(), 1))
        assert env["settings"]["warehouse_temper"] == 24.5
        await stop_transport(tr)
    asyncio.run(scenario())


def test_malformed_nonobject_invalid_payloads_ignored():
    async def scenario():
        tr, client, _ = await connected_transport()
        bad = [
            b"not json",
            b"[1,2,3]",
            b'{"mode": "sleep mode"}',                      # unknown mode enum
            full_state(work_on="TRUE"),                     # not strict ON/OFF
            full_state(drying_running="on"),                # not strict ON/OFF
            full_state(chamber_temp="warm"),                # non-numeric
            full_state(chamber_temp=float("nan")),          # non-finite
            full_state(custom_timer=0),                     # hours range 1..99
        ]
        for b in bad:
            client.fire_state(b)
            await asyncio.sleep(0)
        assert tr.available is True                          # still the old valid one
        env = json.loads(await asyncio.wait_for(tr.poll_latest(), 1))
        assert env["settings"]["warehouse_temper"] == 24.5   # last valid unchanged
        await stop_transport(tr)
    asyncio.run(scenario())


def test_availability_offline_blocks_ordinary_writes():
    async def scenario():
        tr, client, _ = await connected_transport()
        client.fire_state(b"offline", topic=AVL)
        await asyncio.sleep(0)
        assert tr.available is False
        ok = await tr.send(json.dumps({"settings": {"set_temp": 50}}))
        assert ok is False
        assert not [p for p in client.published if p[0] == CMD]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_freshness_window_is_10s_and_disconnect_invalidates():
    async def scenario():
        tr, client, clock = await connected_transport()
        clock.advance(9.0)
        client.fire_state(full_state())                      # keeps it fresh
        await asyncio.sleep(0)
        clock.advance(9.0)
        assert tr.available is True                          # 9s old, still fresh
        clock.advance(2.0)
        assert tr.available is False                         # 11s: stale
        # reconnect invalidates previous freshness until a NEW live packet:
        client.fire_state(full_state())
        await asyncio.sleep(0)
        client.fire_disconnect(1)
        client.fire_connect(0)
        await asyncio.sleep(0)
        assert tr.available is False
        client.fire_state(full_state())
        await asyncio.sleep(0)
        assert tr.available is True
        await stop_transport(tr)
    asyncio.run(scenario())


def test_recv_raises_when_offline_or_stale():
    async def scenario():
        tr, client, clock = await connected_transport()
        env = json.loads(await asyncio.wait_for(tr.poll_latest(), 1))
        assert env["settings"]["warehouse_temper"] == 24.5
        clock.advance(11.0)
        with pytest.raises(DeviceUnavailable):
            await tr.poll_latest()
        clock.advance(-11.0)
        client.fire_state(full_state())
        await asyncio.sleep(0)
        client.fire_disconnect(1)
        await asyncio.sleep(0)
        with pytest.raises(DeviceUnavailable):
            await tr.poll_latest()
        await stop_transport(tr)
    asyncio.run(scenario())


def test_recv_wakes_on_new_packet_and_queue_is_bounded():
    async def scenario():
        tr, client, _ = await connected_transport()
        await asyncio.wait_for(tr.poll_latest(), 1)          # consume current

        async def waiter():
            return json.loads(await tr.recv())

        task = asyncio.ensure_future(waiter())
        await asyncio.sleep(0)
        for i in range(50):                                  # burst of 50 packets
            client.fire_state(full_state(chamber_temp=20.0 + i))
        # coalescing: at most a small backlog of undelivered snapshots
        assert tr.pending_snapshots() <= 4
        env = await asyncio.wait_for(task, 1)
        assert env["settings"]["warehouse_temper"] == 69.0    # the newest one
        await stop_transport(tr)
    asyncio.run(scenario())


# ═════════════════════════════ Normalisation ════════════════════════════════

def test_normalises_native_state_to_settings_envelope():
    async def scenario():
        tr, client, _ = await connected_transport()
        client.fire_state(full_state(mode="filament drying", work_on="ON",
                                     chamber_temp=41.0, target_temp=50,
                                     custom_temp=55, custom_timer=6,
                                     drying_running="ON", drying_remaining_min=120))
        await asyncio.sleep(0)
        s = json.loads(await asyncio.wait_for(tr.poll_latest(), 1))["settings"]
        assert s["warehouse_temper"] == 41.0
        assert s["set_temp"] == 50
        assert s["work_mode"] == 3
        assert s["work_on"] is True
        assert s["custom_temp"] == 55 and s["custom_timer"] == 6
        assert s["filament_temp"] == 55 and s["filament_timer"] == 6
        assert s["remaining_seconds"] == 7200
        assert s["isrunning"] is True
        await stop_transport(tr)
    asyncio.run(scenario())


def test_drying_running_maps_to_isrunning_only_in_dry():
    async def scenario():
        tr, client, _ = await connected_transport()
        client.fire_state(full_state(mode="auto mode", drying_running="ON"))
        await asyncio.sleep(0)
        s = json.loads(await asyncio.wait_for(tr.poll_latest(), 1))["settings"]
        assert "isrunning" not in s
        assert s["work_mode"] == 1
        client.fire_state(full_state(mode="power on", drying_running="ON"))
        await asyncio.sleep(0)
        s = json.loads(await asyncio.wait_for(tr.poll_latest(), 1))["settings"]
        assert "isrunning" not in s
        assert s["work_mode"] == 2
        await stop_transport(tr)
    asyncio.run(scenario())


def test_no_fabricated_heater_or_firmware_fields():
    async def scenario():
        tr, client, _ = await connected_transport()
        s = json.loads(await asyncio.wait_for(tr.poll_latest(), 1))["settings"]
        for forbidden in ("fw_version", "heater_relay", "relay", "bed_temp"):
            assert forbidden not in s
        assert s["work_on"] is False                         # native work_on preserved
        await stop_transport(tr)
    asyncio.run(scenario())


# ═════════════════════════════ Send mapping ═════════════════════════════════

async def one_send(tr, settings):
    ok = await tr.send(json.dumps({"settings": settings}))
    return ok


def cmd_fields(client):
    out = {}
    for topic, payload, kw in client.published:
        if topic != CMD:
            continue
        obj = json.loads(payload)
        assert len(obj) == 1, f"not a one-key native message: {obj}"
        (k, v), = obj.items()
        out[k] = v
    return out


def test_maps_settings_fields_to_native():
    async def scenario():
        tr, client, _ = await connected_transport()
        ok = await one_send(tr, {"set_temp": 45, "filtertemp": 35, "hotbedtemp": 40,
                                 "filament_temp": 55, "filament_timer": 8})
        assert ok is True
        f = cmd_fields(client)
        assert f == {"target_temp": "45", "filter_temp": "35", "heater_temp": "40",
                     "custom_temp": "55", "custom_timer": "8"}
        await stop_transport(tr)
    asyncio.run(scenario())


def test_target_temp_clamped_to_device_range():
    async def scenario():
        tr, client, _ = await connected_transport()
        await one_send(tr, {"set_temp": 70})
        await one_send(tr, {"set_temp": -5})
        vals = [json.loads(p)["target_temp"] for _, p, _ in client.published]
        assert vals == ["60", "0"]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_maps_mode_and_filament_mode_enums():
    async def scenario():
        tr, client, _ = await connected_transport()
        for ws, native in ((1, "auto mode"), (2, "power on"), (3, "filament drying")):
            await one_send(tr, {"work_mode": ws})
        for ws, native in ((1, "pla"), (2, "petg"), (3, "custom")):
            await one_send(tr, {"filament_drying_mode": ws})
        modes = [json.loads(p)["mode"] for t, p, _ in client.published
                 if "mode" in json.loads(p) and "filament_drying_mode" not in json.loads(p)]
        fdm = [json.loads(p)["filament_drying_mode"] for t, p, _ in client.published
               if "filament_drying_mode" in json.loads(p)]
        assert modes == ["auto mode", "power on", "filament drying"]
        assert fdm == ["pla", "petg", "custom"]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_maps_work_on_all_spellings():
    async def scenario():
        tr, client, _ = await connected_transport()
        for v, native in ((True, "ON"), (1, "ON"), (False, "OFF"), (0, "OFF")):
            await one_send(tr, {"work_on": v})
        vals = [json.loads(p)["work_on"] for _, p, _ in client.published]
        assert vals == ["ON", "ON", "OFF", "OFF"]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_work_mode_zero_maps_to_work_on_off_never_a_mode_string():
    async def scenario():
        tr, client, _ = await connected_transport()
        ok = await one_send(tr, {"work_mode": 0})
        assert ok is True
        f = cmd_fields(client)
        assert f == {"work_on": "OFF"}
        await stop_transport(tr)
    asyncio.run(scenario())


def test_rejects_unknown_field_atomically_no_partial_publish():
    async def scenario():
        tr, client, _ = await connected_transport()
        ok = await one_send(tr, {"set_temp": 50, "totally_unknown": 1})
        assert ok is False
        assert not [p for p in client.published if p[0] == CMD]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_rejects_bad_enum_and_nonfinite_atomically():
    async def scenario():
        tr, client, _ = await connected_transport()
        for bad in ({"work_mode": 7}, {"filament_drying_mode": 0},
                    {"set_temp": float("nan")}, {"set_temp": "hot"},
                    {"filament_timer": 0}, {"filament_timer": 100}):
            ok = await one_send(tr, bad)
            assert ok is False, bad
        assert not [p for p in client.published if p[0] == CMD]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_get_settings_is_noop_and_envelopes_never_hit_native_topic():
    async def scenario():
        tr, client, _ = await connected_transport()
        assert await tr.send(json.dumps({"get_settings": 1})) is True
        assert await tr.send(json.dumps({"printer": {"ip": "1", "sn": "2", "access_code": "3"}})) is False
        assert await tr.send("not json") is False
        assert client.published == []
        for _, payload, _ in client.published:
            assert "settings" not in json.loads(payload)
        await stop_transport(tr)
    asyncio.run(scenario())


def test_publishes_are_qos0_retain_false_on_pinned_command_topic():
    async def scenario():
        tr, client, _ = await connected_transport()
        await one_send(tr, {"set_temp": 45})
        assert client.published, "nothing published"
        for topic, payload, kw in client.published:
            assert topic == CMD
            assert kw["qos"] == 0 and kw["retain"] is False
        await stop_transport(tr)
    asyncio.run(scenario())


def test_broker_publish_failure_reports_not_accepted():
    async def scenario():
        client = FakeClient(publish_rc=4)
        tr, client, _ = make_transport(client=client)
        await tr.__aenter__()
        client.fire_connect(0)
        client.fire_state(full_state())
        await asyncio.sleep(0)
        ok = await one_send(tr, {"set_temp": 45})
        assert ok is False
        await stop_transport(tr)
    asyncio.run(scenario())


# ═════════════════════════════ Gating of ordinary writes ════════════════════

def test_no_offline_queue_and_ordinary_send_requires_broker():
    async def scenario():
        tr, client, _ = await connected_transport()
        client.fire_disconnect(1)
        await asyncio.sleep(0)
        ok = await one_send(tr, {"set_temp": 50})
        assert ok is False                                   # dropped, not queued
        client.fire_connect(0)
        await asyncio.sleep(0)
        # reconnected but device state not yet re-established: still stale
        assert tr.available is False
        ok = await one_send(tr, {"set_temp": 50})
        assert ok is False
        assert not [p for p in client.published if p[0] == CMD]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_ordinary_send_requires_fresh_state_stop_does_not():
    async def scenario():
        tr, client, clock = await connected_transport()
        clock.advance(11.0)                                  # device telemetry stale
        assert tr.available is False
        assert await one_send(tr, {"set_temp": 50}) is False  # ordinary blocked
        assert await one_send(tr, {"work_on": False}) is True  # stop allowed
        assert await one_send(tr, {"work_mode": 0}) is True    # stop-family allowed
        assert await one_send(tr, {"isrunning": 0, "work_on": False, "set_temp": 0}) is True
        await stop_transport(tr)
    asyncio.run(scenario())


def test_isrunning_generic_noop_outside_dry_maps_in_dry():
    async def scenario():
        tr, client, _ = await connected_transport()      # mode auto
        ok = await one_send(tr, {"isrunning": 0})
        assert ok is True
        ok = await one_send(tr, {"isrunning": 1})
        assert ok is True
        assert not [p for p in client.published if p[0] == CMD]  # no timer start
        # now device reports Dry mode (from fresh live telemetry):
        client.fire_state(full_state(mode="filament drying"))
        await asyncio.sleep(0)
        await one_send(tr, {"isrunning": 1})
        await one_send(tr, {"isrunning": 0})
        vals = [json.loads(p)["drying_running"] for _, p, _ in client.published]
        assert vals == ["ON", "OFF"]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_compound_stop_maps_work_on_and_drying_running_off():
    async def scenario():
        tr, client, _ = await connected_transport()
        ok = await one_send(tr, {"isrunning": 0, "work_on": False, "set_temp": 0})
        assert ok is True
        f = cmd_fields(client)
        assert f["work_on"] == "OFF"
        assert f["drying_running"] == "OFF"
        await stop_transport(tr)
    asyncio.run(scenario())


# ═════════════════════════════ start_drying ═════════════════════════════════

def start_args(temp=50, hours=8, cancelled=lambda: False):
    return dict(temp=temp, hours=hours, cancelled=cancelled)


def drying_flow(client, clock, temp=50, hours=8):
    """Generator-driven responder: reacts to publishes with matching live telemetry."""
    state = full_state(chamber_temp=24.5)

    def responder(topic, payload, kw):
        obj = json.loads(payload)
        (k, v), = obj.items()
        if k in state and isinstance(state[k], (int, float)) and not isinstance(state[k], bool):
            state[k] = int(v) if str(v).lstrip("-").isdigit() else float(v)
        else:
            state[k] = v
        if k == "drying_running" and v == "ON":
            state["drying_remaining_min"] = hours * 60
        client.fire_state(dict(state))
    return state, responder


def test_start_drying_validates_input():
    async def scenario():
        tr, client, _ = await connected_transport()
        for bad in ({"temp": 39, "hours": 8}, {"temp": 61, "hours": 8},
                    {"temp": 50.5, "hours": 8}, {"temp": 50, "hours": 0},
                    {"temp": 50, "hours": 100}):
            ok = await tr.start_drying(**bad, cancelled=lambda: False)
            assert ok is False, bad
        assert not [p for p in client.published if p[0] == CMD]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_start_drying_rejects_active_cycle():
    async def scenario():
        tr, client, _ = await connected_transport()
        client.fire_state(full_state(mode="filament drying", work_on="ON",
                                     drying_running="ON", drying_remaining_min=90))
        await asyncio.sleep(0)
        ok = await tr.start_drying(**start_args())
        assert ok is False
        assert not [p for p in client.published if p[0] == CMD]
        assert tr.start_status == "failed"
        await stop_transport(tr)
    asyncio.run(scenario())


def test_start_drying_happy_path_exact_frame_sequence_once():
    async def scenario():
        tr, client, clock = await connected_transport()
        state, responder = drying_flow(client, clock)
        orig_publish = client.publish

        def publish(topic, payload=None, qos=0, retain=False):
            r = orig_publish(topic, payload, qos, retain)
            if topic == CMD:
                responder(topic, payload, {"qos": qos, "retain": retain})
            return r
        client.publish = publish

        ok = await tr.start_drying(temp=50, hours=8, cancelled=lambda: False)
        assert ok is True
        keys = [json.loads(p).__iter__().__next__() for t, p, _ in client.published]
        assert keys == ["custom_temp", "custom_timer", "filament_drying_mode",
                        "mode", "work_on", "drying_running"]
        starts = [p for t, p, _ in client.published if "drying_running" in p]
        assert starts == ['{"drying_running": "ON"}']
        f = cmd_fields(client)
        assert f["custom_temp"] == "50" and f["custom_timer"] == "8"
        assert f["filament_drying_mode"] == "custom"
        assert f["mode"] == "filament drying"
        assert f["work_on"] == "ON"
        assert tr.start_status == "confirmed"
        await stop_transport(tr)
    asyncio.run(scenario())


def test_start_drying_config_readback_must_match():
    async def scenario():
        tr, client, clock = await connected_transport()
        # device echoes the WRONG custom_temp: config never confirmed → no start
        def publish(topic, payload=None, qos=0, retain=False):
            r = client.published and FakePublish(0)
            client.published.append((topic, payload, {"qos": qos, "retain": retain}))
            if topic == CMD:
                (k, v), = json.loads(payload).items()
                echo = dict(full_state(mode="filament drying", work_on="ON"))
                echo["custom_temp"] = 41          # mismatched echo
                client.fire_state(echo)
            return FakePublish(0)
        client.publish = publish
        ok = await tr.start_drying(temp=50, hours=8, cancelled=lambda: False)
        assert ok is False
        assert not [p for p in client.published if "drying_running" in p]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_start_drying_checks_cancellation_before_every_publish_and_waits():
    async def scenario():
        tr, client, clock = await connected_transport()
        calls = {"n": 0}

        def cancel_soon():
            calls["n"] += 1
            return calls["n"] > 1                        # cancel after first check

        ok = await tr.start_drying(temp=50, hours=8, cancelled=cancel_soon)
        assert ok is False
        assert not [p for p in client.published if "drying_running" in p]
        await stop_transport(tr)
    asyncio.run(scenario())


def test_start_drying_timeout_no_retry_uncertain_latch():
    async def scenario():
        tr, client, clock = await connected_transport()
        # config echoes fine, but device never reports running ON
        def publish(topic, payload=None, qos=0, retain=False):
            client.published.append((topic, payload, {"qos": qos, "retain": retain}))
            if topic == CMD:
                (k, v), = json.loads(payload).items()
                st = full_state(mode="filament drying", work_on="ON", custom_temp=50,
                                custom_timer=8, drying_running="OFF")
                client.fire_state(st)
            return FakePublish(0)
        client.publish = publish
        # run the wait with a clock we control: transport waits on monotonic clock
        async def advance_clock():
            while True:
                await asyncio.sleep(0.005)
                clock.advance(1.0)
        adv = asyncio.ensure_future(advance_clock())
        try:
            ok = await asyncio.wait_for(
                tr.start_drying(temp=50, hours=8, cancelled=lambda: False), 5)
        finally:
            adv.cancel()
            await asyncio.gather(adv, return_exceptions=True)
        assert ok is False
        assert tr.start_status == "uncertain"
        starts = [p for t, p, _ in client.published if "drying_running" in p]
        assert starts == ['{"drying_running": "ON"}']                # exactly once, no retry
        # latch: another start is refused until fresh telemetry resolves the state
        ok2 = await tr.start_drying(temp=50, hours=8, cancelled=lambda: False)
        assert ok2 is False
        starts2 = [p for t, p, _ in client.published if "drying_running" in p]
        assert starts2 == starts
        await stop_transport(tr)
    asyncio.run(scenario())


def test_uncertain_latch_not_cleared_by_pre_start_cached_packet():
    async def scenario():
        tr, client, clock = await connected_transport()
        cached = full_state(drying_running="OFF", mode="auto mode")
        client.fire_state(cached)                                   # cached idle, pre-start
        await asyncio.sleep(0)

        def publish(topic, payload=None, qos=0, retain=False):
            client.published.append((topic, payload, {"qos": qos, "retain": retain}))
            if topic == CMD:
                st = full_state(mode="filament drying", work_on="ON", custom_temp=50,
                                custom_timer=8, drying_running="OFF")
                client.fire_state(st)
            return FakePublish(0)
        client.publish = publish
        adv_done = asyncio.Event()

        async def advance_clock():
            while not adv_done.is_set():
                await asyncio.sleep(0.005)
                clock.advance(1.0)
        adv = asyncio.ensure_future(advance_clock())
        try:
            ok = await asyncio.wait_for(tr.start_drying(50, 8, lambda: False), 5)
        finally:
            adv_done.set()
            await asyncio.gather(adv, return_exceptions=True)
        assert ok is False and tr.start_status == "uncertain"
        # cached packet is OLDER than the latch point: cannot clear it
        client.fire_state(cached)
        await asyncio.sleep(0)
        ok = await tr.start_drying(50, 8, lambda: False)
        assert ok is False
        # a NEW live packet showing idle resolves the latch
        client.fire_state(full_state(drying_running="OFF", mode="filament drying",
                                     drying_remaining_min=0, work_on="ON"))
        await asyncio.sleep(0)
        assert tr.start_status != "uncertain"
        await stop_transport(tr)
    asyncio.run(scenario())


def test_stop_overtakes_a_waiting_start():
    """An emergency stop must not queue behind the 10s config wait, and the waiting
    start must not publish drying_running ON afterwards."""
    async def scenario():
        tr, client, clock = await connected_transport()
        # publishes land but device never echoes config → start waits for readback
        adv_done = asyncio.Event()

        async def advance_clock():
            while not adv_done.is_set():
                await asyncio.sleep(0.005)
                clock.advance(1.0)
        adv = asyncio.ensure_future(advance_clock())

        async def start():
            try:
                return await asyncio.wait_for(tr.start_drying(50, 8, lambda: False), 8)
            except asyncio.TimeoutError:
                return "start-waiting"
        task = asyncio.ensure_future(start())
        await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()

        async def stop_now():
            return await tr.send(json.dumps({"settings": {"work_on": False, "work_mode": 0}}))
        stop_task = asyncio.ensure_future(stop_now())
        done, _ = await asyncio.wait([stop_task], timeout=1.0)
        assert stop_task in done, "stop blocked behind the dry-start wait"
        assert stop_task.result() is True
        off_seen = any(json.loads(p).get("work_on") == "OFF" for _, p, _ in client.published)
        assert off_seen
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        adv_done.set()
        await asyncio.gather(adv, return_exceptions=True)
        assert not [p for t, p, _ in client.published if "drying_running" in p]
        await stop_transport(tr)
    asyncio.run(scenario())


# ═════════════════════════════ emergency_stop ═══════════════════════════════

def test_emergency_stop_confirms_only_from_device_readback():
    async def scenario():
        tr, client, clock = await connected_transport()

        def publish(topic, payload=None, qos=0, retain=False):
            client.published.append((topic, payload, {"qos": qos, "retain": retain}))
            if topic == CMD and json.loads(payload).get("work_on") == "OFF":
                client.fire_state(full_state(work_on="OFF", drying_running="OFF"))
            return FakePublish(0)
        client.publish = publish
        ok = await tr.emergency_stop()
        assert ok is True
        f = cmd_fields(client)
        assert f.get("work_on") == "OFF" and f.get("drying_running") == "OFF"
        assert client.published[-1][1]  # closed cleanly after
        await stop_transport(tr)
    asyncio.run(scenario())


def test_emergency_stop_timeout_reports_unconfirmed_never_off_claim():
    async def scenario():
        tr, client, clock = await connected_transport()

        def publish(topic, payload=None, qos=0, retain=False):
            client.published.append((topic, payload, {"qos": qos, "retain": retain}))
            # device never echoes: state stays work_on OFF already? device shows ON running
            return FakePublish(0)
        client.fire_state(full_state(work_on="ON", drying_running="ON", mode="filament drying"))
        await asyncio.sleep(0)
        client.publish = publish
        adv_done = asyncio.Event()

        async def advance_clock():
            while not adv_done.is_set():
                await asyncio.sleep(0.005)
                clock.advance(1.0)
        adv = asyncio.ensure_future(advance_clock())
        try:
            ok = await asyncio.wait_for(tr.emergency_stop(), 5)
        finally:
            adv_done.set()
            await asyncio.gather(adv, return_exceptions=True)
        assert ok is False
        await stop_transport(tr)
    asyncio.run(scenario())


def test_emergency_stop_works_with_stale_device_state():
    async def scenario():
        tr, client, clock = await connected_transport()
        clock.advance(30)                                    # stale device telemetry
        adv_done = asyncio.Event()

        async def advance_clock():
            while not adv_done.is_set():
                await asyncio.sleep(0.005)
                clock.advance(1.0)
        adv = asyncio.ensure_future(advance_clock())
        try:
            ok = await asyncio.wait_for(tr.emergency_stop(), 5)
        finally:
            adv_done.set()
            await asyncio.gather(adv, return_exceptions=True)
        assert any(json.loads(p).get("work_on") == "OFF" for t, p, _ in client.published)
        assert ok is False                                    # unconfirmed without readback
        await stop_transport(tr)
    asyncio.run(scenario())


# ═════════════════════════════ Heating proof ════════════════════════════════

def start_with_responder(tr, client, chamber=24.5, temp=50, hours=8,
                         running_after_start=True):
    state, responder = drying_flow(client, None, temp=temp, hours=hours)
    state["chamber_temp"] = chamber
    client.fire_state(dict(state))        # device reports the pre-start chamber temp

    def publish(topic, payload=None, qos=0, retain=False):
        client.published.append((topic, payload, {"qos": qos, "retain": retain}))
        if topic == CMD:
            responder(topic, payload, {"qos": qos, "retain": retain})
        return FakePublish(0)
    client.publish = publish
    return state, responder


def test_heating_confirmed_after_2C_rise_within_5min():
    async def scenario():
        tr, client, clock = await connected_transport()
        state, responder = start_with_responder(tr, client, chamber=24.5)
        ok = await tr.start_drying(50, 8, lambda: False)
        assert ok is True
        assert tr.heating_status == "pending"
        client.fire_state(dict(state, chamber_temp=26.4))     # +1.9 only
        await asyncio.sleep(0)
        assert tr.heating_status == "pending"
        client.fire_state(dict(state, chamber_temp=26.6))     # +2.1
        await asyncio.sleep(0)
        assert tr.heating_status == "confirmed"
        # no heater-changing publishes after the confirmed start
        after = [p for t, p, _ in client.published if t == CMD]
        assert sum(1 for p in after if "drying_running" in p) == 1
        await stop_transport(tr)
    asyncio.run(scenario())


def test_heating_unconfirmed_after_5min_without_rise():
    async def scenario():
        tr, client, clock = await connected_transport()
        start_with_responder(tr, client, chamber=24.5)
        ok = await tr.start_drying(50, 8, lambda: False)
        assert ok is True
        adv_done = asyncio.Event()

        async def advance_clock():
            while not adv_done.is_set():
                await asyncio.sleep(0.005)
                clock.advance(10.0)
        adv = asyncio.ensure_future(advance_clock())
        for _ in range(40):
            await asyncio.sleep(0.005)
            if tr.heating_status != "pending":
                break
        adv_done.set()
        await asyncio.gather(adv, return_exceptions=True)
        assert tr.heating_status == "unconfirmed"
        # temperature/timeouts must not change heater or timer:
        assert sum(1 for t, p, _ in client.published if "drying_running" in p) == 1
        assert sum(1 for t, p, _ in client.published if "custom_timer" in p) == 1
        await stop_transport(tr)
    asyncio.run(scenario())


def test_already_at_target_reports_running_without_rise_claim():
    async def scenario():
        tr, client, clock = await connected_transport()
        start_with_responder(tr, client, chamber=50.0, temp=50)
        ok = await tr.start_drying(50, 8, lambda: False)
        assert ok is True                                      # timer running confirmed
        assert tr.heating_status == "at_target"                # no rise claim
        await stop_transport(tr)
    asyncio.run(scenario())


# ═════════════════════════════ Misc invariants ══════════════════════════════

def test_never_instantiates_real_paho_client_on_module_import():
    import importlib
    import panda_native_mqtt as m
    src = open(m.__file__).read()
    assert "Client(" not in src.split("def ")[0]              # no top-level construction
    # default factory builds lazily:
    tr = NativePandaTransport("h", 1883, "u", "p", DEV)
    assert tr._client is None


def test_callbacks_schedule_safely_from_non_loop_thread():
    """paho fires callbacks on its own thread: ingestion must hop onto the loop."""
    import threading
    async def scenario():
        tr, client, _ = await connected_transport()
        got = asyncio.Event()
        seen = {}

        async def consume():
            env = await tr.recv()
            seen.update(json.loads(env)["settings"])
            got.set()

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0)

        def device_thread():
            client.fire_state(full_state(chamber_temp=33.3))
        t = threading.Thread(target=device_thread)
        t.start()
        t.join()
        await asyncio.wait_for(got.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert seen["warehouse_temper"] == 33.3
        await stop_transport(tr)
    asyncio.run(scenario())
