"""Native firmware-V1.0.4 MQTT transport for the Panda Breath device.

The device path goes to its OWN authenticated broker with its own paho client,
never through Home Assistant or the mod's HA broker. The firmware publishes
flat native state on ``panda_breath/<device-id>/state`` and accepts flat,
single-key, non-retained QoS 0 commands on ``panda_breath/<device-id>/command``;
availability on ``.../availability``.

Protocol rules implemented here (openspec/changes/native-panda-mqtt/design.md):

* Device identity is pinned: only the exact three topics of one device id are
  subscribed and only that device's packets count.
* Retained packets, other devices, malformed/non-object payloads and invalid
  enums/values never establish state. A complete live snapshot (mode, work_on,
  chamber_temp, drying_running, custom_temp, custom_timer,
  drying_remaining_min) is required before any control.
* State is fresh for FRESH_WINDOW_S measured on an injectable monotonic clock;
  broker disconnect and reconnect invalidate freshness until a NEW live packet
  arrives. ``recv()`` then raises so the backend clears its bind status.
* Commands are one-key native messages, QoS 0, retain=False, never queued for
  replay: if the broker is not connected the send is dropped and reported
  False. Broker acceptance (rc 0) proves broker delivery ONLY — device
  acceptance is always established by fresh post-write telemetry readback.
* ``work_on`` reports the firmware's enabled state, not physical heater-relay
  truth; nothing here fabricates heater or firmware-version fields.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Callable, Deque, Optional, Tuple

TOPIC_STATE_SUFFIX = "state"
TOPIC_COMMAND_SUFFIX = "command"
TOPIC_AVAILABILITY_SUFFIX = "availability"

FRESH_WINDOW_S = 10.0        # live telemetry older than this blocks ordinary writes
CONFIG_READBACK_S = 10.0     # wait for post-config telemetry matching a write
START_READBACK_S = 10.0      # wait for drying_running ON + remaining after start
OFF_READBACK_S = 10.0        # wait for work_on/drying_running OFF readback
HEAT_PROOF_S = 300.0         # window for the >= 2 C rise after a confirmed start
HEAT_RISE_C = 2.0
TARGET_TEMP_MIN, TARGET_TEMP_MAX = 0, 60       # device hardware safety cap
DRY_TEMP_MIN, DRY_TEMP_MAX = 40, 60
DRY_HOURS_MIN, DRY_HOURS_MAX = 1, 99           # custom_timer is whole hours
_QUEUE_MAX = 4                 # coalescing recv mailbox bound

_MODE_TO_NUM = {"auto mode": 1, "power on": 2, "filament drying": 3}
_NUM_TO_MODE = {v: k for k, v in _MODE_TO_NUM.items()}
_FDM_TO_NUM = {"pla": 1, "petg": 2, "custom": 3}
_NUM_TO_FDM = {v: k for k, v in _FDM_TO_NUM.items()}

_REQUIRED_SNAPSHOT = ("mode", "work_on", "chamber_temp", "drying_running",
                      "custom_temp", "custom_timer", "drying_remaining_min")


class DeviceUnavailable(RuntimeError):
    """Broker offline, availability offline, or no fresh live device state."""


def _is_finite_number(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return v == v and v not in (float("inf"), float("-inf"))


def _strict_on_off(v) -> Optional[bool]:
    if v == "ON":
        return True
    if v == "OFF":
        return False
    return None


def _native_num_str(v):
    """Render a validated number the way the firmware reports it."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _default_client_factory():
    import paho.mqtt.client as mqtt
    try:
        from paho.mqtt.client import CallbackAPIVersion
        return mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2,
                           clean_session=True)
    except (ImportError, TypeError):
        return mqtt.Client(clean_session=True)


def _running_loop_is(loop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


class NativePandaTransport:
    """Async send/recv seam speaking native device MQTT on a dedicated broker.

    Mirrors what Panda.py used to get from a persistent device WebSocket:
    ``send(payload)`` accepts the legacy ``{"settings": {...}}`` envelope and
    translates it to flat native messages; ``recv()`` yields normalised
    ``{"settings": {...}}`` envelopes built from live native telemetry.
    """

    def __init__(self, host, port, username, password, device_id, *,
                 client_factory: Optional[Callable[[], object]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 logger: Optional[Callable[[str], None]] = None):
        if not host or not str(host).strip():
            raise ValueError("device MQTT host is required")
        if not device_id or not str(device_id).strip():
            raise ValueError("device MQTT id (topic id) is required")
        self.host = str(host).strip()
        self.port = int(port)
        self.username = username or ""
        self.password = password or ""
        self.device_id = str(device_id).strip()
        self._client_factory = client_factory
        self._clock = clock
        self._log = logger or (lambda _m: None)

        self.topic_state = f"panda_breath/{self.device_id}/{TOPIC_STATE_SUFFIX}"
        self.topic_command = f"panda_breath/{self.device_id}/{TOPIC_COMMAND_SUFFIX}"
        self.topic_availability = (
            f"panda_breath/{self.device_id}/{TOPIC_AVAILABILITY_SUFFIX}")

        self._client = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._broker_connected = False
        self.policy_authorized = False
        self._stop_generation = 0
        self._startup_deadline = None
        self._session_has_state = False
        self._availability_online = True

        self._seq = 0
        self._latest: Optional[Tuple[int, float, dict, dict]] = None  # seq, ts, native, settings
        self._last_live_ts: Optional[float] = None
        self._queue: Deque[Tuple[int, float, dict, dict]] = deque(maxlen=_QUEUE_MAX)
        self._new_data: Optional[asyncio.Event] = None

        # Dry-start / heating observation (for the backend to publish verbatim)
        self.start_status: Optional[str] = None     # confirming|uncertain|confirmed|failed
        self.heating_status: Optional[str] = None   # pending|confirmed|at_target|unconfirmed
        self._latch_seq: Optional[int] = None       # publish point of an unresolved start
        self._start_op_busy = False                 # a start operation is in flight
        self._heat_task: Optional[asyncio.Task] = None
        self._heat_pre_temp: Optional[float] = None
        self._heat_deadline: Optional[float] = None

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def __aenter__(self):
        self._loop = asyncio.get_running_loop()
        self._new_data = asyncio.Event()
        self._broker_connected = False
        self._availability_online = True
        self.policy_authorized = False
        self._session_has_state = False
        self._startup_deadline = self._loop.time() + 20.0
        self._invalidate_freshness()
        factory = self._client_factory or _default_client_factory
        client = factory()
        self._client = client
        client.on_connect = self._cb_connect
        client.on_disconnect = self._cb_disconnect
        client.on_message = self._cb_message
        if self.username:
            client.username_pw_set(self.username, self.password)
        client.connect_async(self.host, self.port, 60)
        client.loop_start()
        return self

    async def __aexit__(self, *exc):
        client, self._client = self._client, None
        if self._heat_task is not None:
            self._heat_task.cancel()
            self._heat_task = None
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.loop_stop()
            except Exception:
                pass
        self._broker_connected = False
        self._invalidate_freshness()
        return False

    # ── paho callbacks (run on the paho network thread) ────────────────────
    def _schedule(self, coro_factory) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            self._log("[NATIVE-MQTT] callback dropped: event loop not running")
            return
        try:
            if _running_loop_is(loop):
                # paho normally fires callbacks on its own thread; tests that
                # simulate a device on the loop thread get deterministic,
                # synchronous ingestion instead of two more loop hops.
                loop.create_task(coro_factory())
            else:
                asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        except Exception as e:
            self._log(f"[NATIVE-MQTT] callback scheduling failed: {type(e).__name__}: {e}")

    def _cb_connect(self, client, userdata, flags, rc, *rest):
        self._schedule(lambda: self._handle_connect(rc))

    def _cb_disconnect(self, client, userdata, *args):
        self._schedule(lambda: self._handle_disconnect())

    def _cb_message(self, client, userdata, msg):
        topic, payload, retain = msg.topic, bytes(msg.payload), bool(msg.retain)
        self._schedule(lambda: self._handle_message(topic, payload, retain))

    async def _handle_connect(self, rc) -> None:
        ok = (rc == 0) if isinstance(rc, int) else not getattr(rc, "is_failure", True)
        self._broker_connected = bool(ok)
        if ok:
            for topic in (self.topic_state, self.topic_availability):
                try:
                    self._client.subscribe(topic, qos=0)
                except Exception as e:
                    self._log(f"[NATIVE-MQTT] subscribe failed {topic}: {e}")
        # A (re)connect never restores freshness: only a NEW live packet does.
        self._invalidate_freshness()

    async def _handle_disconnect(self) -> None:
        self._broker_connected = False
        self._invalidate_freshness()

    # ── state ingestion ─────────────────────────────────────────────────────
    async def _handle_message(self, topic: str, payload: bytes, retain: bool) -> None:
        if topic == self.topic_availability:
            text = payload.decode("utf-8", "replace").strip().lower()
            if text == "offline":
                self._availability_online = False
                self._invalidate_freshness()
            elif text == "online":
                self._availability_online = True
            return
        if topic != self.topic_state:
            return                       # other devices / command echo: ignored
        if retain:
            return                       # retained control/status never establishes freshness
        if not self._broker_connected or not self._availability_online:
            return
        try:
            native = json.loads(payload.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(native, dict):
            return
        settings = self._validate_and_normalize(native)
        if settings is None:
            return
        ts = self._clock()
        self._seq += 1
        snap = (self._seq, ts, dict(native), settings)
        self._latest = snap
        self._queue.append(snap)
        self._last_live_ts = ts
        self._session_has_state = True
        if self.available:
            self._resolve_latch(native, snap[0])
            self._observe_heating(native)
        if self._new_data is not None:
            self._new_data.set()

    @staticmethod
    def _validate_and_normalize(native: dict) -> Optional[dict]:
        for key in _REQUIRED_SNAPSHOT:
            if key not in native:
                return None
        mode = native.get("mode")
        if mode not in _MODE_TO_NUM:
            return None
        work_on = _strict_on_off(native.get("work_on"))
        drying = _strict_on_off(native.get("drying_running"))
        if work_on is None or drying is None:
            return None
        if not _is_finite_number(native.get("chamber_temp")):
            return None
        if not _is_finite_number(native.get("custom_temp")):
            return None
        timer = native.get("custom_timer")
        if not _is_finite_number(timer) or not (DRY_HOURS_MIN <= int(timer) <= DRY_HOURS_MAX):
            return None
        remaining = native.get("drying_remaining_min")
        if not _is_finite_number(remaining) or remaining < 0:
            return None
        for opt in ("target_temp", "filter_temp", "heater_temp"):
            if opt in native and not _is_finite_number(native[opt]):
                return None
        s = {
            "warehouse_temper": float(native["chamber_temp"]),
            "work_mode": _MODE_TO_NUM[mode],
            "work_on": work_on,
            "custom_temp": int(native["custom_temp"]),
            "custom_timer": int(timer),
            "filament_temp": int(native["custom_temp"]),
            "filament_timer": int(timer),
            "remaining_seconds": int(float(remaining) * 60),
        }
        if "target_temp" in native:
            s["set_temp"] = _native_num_str(native["target_temp"])
        if "filter_temp" in native:
            s["filtertemp"] = float(native["filter_temp"])
        # drying_running is DRY-only: outside the firmware's drying mode it must
        # never look like the generic chamber isrunning (review focus #1).
        if mode == "filament drying":
            s["isrunning"] = drying
        return s

    def _invalidate_freshness(self) -> None:
        self._last_live_ts = None
        self._queue.clear()
        self._latest = None
        if self._new_data is not None:
            self._new_data.set()

    @property
    def connected(self) -> bool:
        return self._broker_connected

    @property
    def available(self) -> bool:
        """Fresh, live, non-retained state from the pinned device on our broker."""
        if not self._broker_connected or not self._availability_online:
            return False
        if self._last_live_ts is None:
            return False
        return (self._clock() - self._last_live_ts) <= FRESH_WINDOW_S

    @property
    def status_age(self) -> Optional[float]:
        if self._last_live_ts is None:
            return None
        return self._clock() - self._last_live_ts

    def _latest_native(self) -> Optional[dict]:
        return self._latest[2] if self._latest else None

    def _resolve_latch(self, native: dict, seq: int) -> None:
        # Only telemetry that arrives AFTER an abandoned start resolved it, and
        # only the firmware's own drying state counts: a cached idle packet from
        # outside drying cannot clear the uncertain latch.
        if self._latch_seq is None or self._start_op_busy or seq <= self._latch_seq:
            return
        if native.get("mode") != "filament drying":
            return
        running = _strict_on_off(native.get("drying_running"))
        if running is None:
            return
        remaining = native.get("drying_remaining_min")
        if running and _is_finite_number(remaining) and remaining > 0:
            self.start_status = "confirmed"
            self._latch_seq = None
        elif not running:
            self.start_status = None
            self._latch_seq = None

    # ── recv / poll ──────────────────────────────────────────────────────────
    def pending_snapshots(self) -> int:
        return len(self._queue)

    async def poll_latest(self) -> str:
        if not self.available or self._latest is None:
            raise DeviceUnavailable("no fresh live device state")
        return json.dumps({"settings": self._latest[3]})

    async def recv(self) -> str:
        """Next normalised snapshot envelope; raises if offline or freshness dies.

        Only packets that ARRIVE after this call are delivered (live events
        only, never a cached snapshot), and a burst coalesces to the newest.
        """
        assert self._loop is not None
        entry_seq = self._seq
        while True:
            if not self._session_has_state and self._availability_online:
                # Allow connect_async and the first firmware push to finish.
                if self._loop.time() < self._startup_deadline:
                    await self._tick()
                    continue
                raise DeviceUnavailable("device startup timed out")
            if not self._broker_connected:
                raise DeviceUnavailable("device broker offline")
            while self._queue:
                snap = self._queue.pop()          # coalesce: newest wins
                if snap[0] > entry_seq:
                    self._queue.clear()
                    return json.dumps({"settings": snap[3]})
            if not self._availability_online:
                raise DeviceUnavailable("device reports offline")
            if self._last_live_ts is None:
                raise DeviceUnavailable("no live device state since (re)connect")
            remaining = FRESH_WINDOW_S - (self._clock() - self._last_live_ts)
            if remaining <= 0:
                raise DeviceUnavailable("device telemetry is stale")
            assert self._new_data is not None
            self._new_data.clear()
            try:
                await asyncio.wait_for(self._new_data.wait(),
                                       timeout=min(0.25, max(0.01, remaining)))
            except asyncio.TimeoutError:
                pass

    # ── send: legacy envelope -> flat native commands ───────────────────────
    async def send(self, payload: str) -> bool:
        """Translate a legacy WS envelope and publish flat one-key commands.

        True means the BROKER accepted every message (rc 0) — never that the
        device acted on it. Returns False (nothing queued) when a write is
        rejected: unsupported envelope/field, broker offline, or — for ordinary
        (non-stop) writes — no fresh live device state.
        """
        try:
            envelope = json.loads(payload)
        except (ValueError, TypeError):
            self._log("[NATIVE-MQTT] rejected non-JSON send")
            return False
        if not isinstance(envelope, dict):
            return False
        if "get_settings" in envelope:
            return True                        # native state pushes on its own
        settings = envelope.get("settings")
        if not isinstance(settings, dict):
            self._log(f"[NATIVE-MQTT] non-settings envelope not sent: {list(envelope)}")
            return False
        cmds = self._translate(settings)
        if cmds is None:
            return False                       # rejected atomically: nothing published
        if not cmds:
            return True                        # mapping chose to skip (generic no-op)
        if not self._broker_connected or self._client is None:
            self._log("[NATIVE-MQTT] broker offline — command dropped, not queued")
            return False
        stop_command = any((k == "work_on" and v == "OFF") or
                           (k == "drying_running" and v == "OFF") for k, v in cmds)
        if not stop_command and not self.available:
            self._log("[NATIVE-MQTT] ordinary write rejected: device state stale/offline")
            return False
        if stop_command:
            self._stop_generation += 1
        for field, value in cmds:
            try:
                r = self._client.publish(self.topic_command,
                                         json.dumps({field: self._wire_value(field, value)}), qos=0, retain=False)
            except Exception as e:
                self._log(f"[NATIVE-MQTT] publish error: {e}")
                return False
            rc = r[0] if isinstance(r, tuple) else getattr(r, "rc", -1)
            if rc != 0:
                self._log(f"[NATIVE-MQTT] broker refused {field} (rc={rc})")
                return False
        if not stop_command and (settings.get("work_on") in (True, 1) or settings.get("set_temp", 0) > 0):
            self.policy_authorized = True
        return True

    @staticmethod
    def _wire_value(field, value):
        if field in {"target_temp", "filter_temp", "heater_temp", "custom_temp", "custom_timer"}:
            return _native_num_str(float(value))
        return value

    def _translate(self, settings: dict):
        """Ordered [(native_field, str value)] or None to reject atomically."""
        # Compound heater-off: the mod's canonical stop envelope. Native maps it
        # to work_on OFF + drying_running OFF (both stop-family, allowed stale).
        keys = set(settings)
        if (keys and keys <= {"isrunning", "work_on", "set_temp", "work_mode"}
                and settings.get("isrunning") in (0, False)
                and settings.get("work_on") in (0, False)
                and ("set_temp" not in settings or settings.get("set_temp") in (0, 0.0))
                and ("work_mode" not in settings or settings.get("work_mode") in (0, False))):
            return [("work_on", "OFF"), ("drying_running", "OFF")]

        dry_now = (settings.get("work_mode") == 3 if "work_mode" in settings else
                   self._latest is not None and self._latest[2].get("mode") == "filament drying")
        out: dict = {}
        for key, val in settings.items():
            if key == "set_temp":
                if not _is_finite_number(val):
                    return None
                out["target_temp"] = str(max(TARGET_TEMP_MIN,
                                             min(TARGET_TEMP_MAX, int(round(float(val))))))
            elif key == "filtertemp":
                if not _is_finite_number(val):
                    return None
                out["filter_temp"] = str(_native_num_str(val))
            elif key == "hotbedtemp":
                if not _is_finite_number(val):
                    return None
                out["heater_temp"] = str(_native_num_str(val))
            elif key == "filament_temp":
                if not _is_finite_number(val):
                    return None
                out["custom_temp"] = str(int(val))
            elif key == "filament_timer":
                if not _is_finite_number(val) or not (DRY_HOURS_MIN <= int(val) <= DRY_HOURS_MAX):
                    return None
                out["custom_timer"] = str(int(val))
            elif key == "filament_drying_mode":
                if isinstance(val, bool) or not isinstance(val, int) or val not in _NUM_TO_FDM:
                    return None
                out["filament_drying_mode"] = _NUM_TO_FDM[val]
            elif key == "work_mode":
                if isinstance(val, bool) or not isinstance(val, int):
                    return None
                if val == 0:
                    out["work_on"] = "OFF"       # stop; the firmware has no mode-0 string
                elif val in _NUM_TO_MODE:
                    out["mode"] = _NUM_TO_MODE[val]
                else:
                    return None
            elif key == "work_on":
                if val in (True, 1):
                    out["work_on"] = "ON"
                elif val in (False, 0):
                    out["work_on"] = "OFF"
                else:
                    return None
            elif key == "isrunning":
                if val in (True, 1):
                    # Outside firmware drying this must not look like a timer start.
                    if dry_now:
                        out["drying_running"] = "ON"
                elif val in (False, 0):
                    if dry_now:
                        out["drying_running"] = "OFF"
                else:
                    return None
            else:
                self._log(f"[NATIVE-MQTT] unsupported field rejected: {key}")
                return None
        return list(out.items())

    # ── timed drying start ───────────────────────────────────────────────────
    async def start_drying(self, temp: int, hours: int,
                           cancelled: Callable[[], bool]) -> bool:
        """Configure + start a native dry cycle with full readback confirmation.

        One transport operation: publish custom_temp, custom_timer,
        filament_drying_mode custom, mode filament drying and work_on ON as
        individual one-key messages, each confirmed by matching POST-WRITE live
        telemetry, then publish drying_running ON exactly once at QoS 0 with no
        retry. Uncertain outcomes latch: later fresh telemetry resolves them;
        duplicate requests cannot clear the latch or restart the cycle.
        Cancellation is honoured before every publish and after every wait, and
        never blocks a concurrent stop.
        """
        stop_generation = self._stop_generation
        caller_cancelled = cancelled
        cancelled = lambda: caller_cancelled() or self._stop_generation != stop_generation
        if self._latch_seq is not None:
            # An earlier start is unresolved: refuse until FRESH telemetry after
            # that publish resolves running/idle. Duplicate requests cannot
            # clear the latch or restart the cycle.
            self.start_status = "uncertain"
            self._log("[NATIVE-DRY] start refused: earlier start unresolved")
            return False
        self.start_status = None
        if isinstance(temp, bool) or not isinstance(temp, int) \
                or not DRY_TEMP_MIN <= temp <= DRY_TEMP_MAX \
                or isinstance(hours, bool) or not isinstance(hours, int) \
                or not DRY_HOURS_MIN <= hours <= DRY_HOURS_MAX:
            self.start_status = "failed"
            self._log(f"[NATIVE-DRY] rejected start {temp}C/{hours}h: out of range")
            return False
        if not self._broker_connected:
            self.start_status = "failed"
            return False
        latest = self._latest_native()
        if not self.available or latest is None:
            self.start_status = "failed"
            self._log("[NATIVE-DRY] start refused: no fresh live device state")
            return False
        running = _strict_on_off(latest.get("drying_running"))
        remaining = latest.get("drying_remaining_min")
        if running or (latest.get("mode") == "filament drying"
                       and _is_finite_number(remaining) and remaining > 0):
            self.start_status = "failed"
            self._log("[NATIVE-DRY] start refused: device reports an active cycle")
            return False

        if self._start_op_busy:
            # A start is already in flight (config writes or timer publish):
            # a second concurrent start could double-publish drying_running ON
            # and reset the running timer (Sol review).
            self.start_status = "failed"
            self._log("[NATIVE-DRY] start refused: another start in flight")
            return False
        self.start_status = "confirming"

        steps = [("custom_temp", str(temp)), ("custom_timer", str(hours)),
                 ("filament_drying_mode", "custom"), ("mode", "filament drying"),
                 ("work_on", "ON")]
        self._start_op_busy = True
        try:
            steps_ok = await self._run_start_steps(steps, cancelled)
        finally:
            self._start_op_busy = False
        if not steps_ok:
            self.start_status = "failed"
            return False

        # Pre-start temperature for the heating proof: the freshest device
        # reading just before the timer start, not the one at operation entry.
        pre_start_temp = float(self._latest[2].get("chamber_temp", 0.0))
        if self._latch_seq is not None or self._start_op_busy:
            # Another start armed its uncertain latch while we configured.
            self.start_status = "failed"
            self._log("[NATIVE-DRY] start aborted before timer ON: "
                      "another start is active")
            return False
        point = self._seq
        self._arm_uncertain(point)
        self._start_op_busy = True
        try:
            if not await self._publish_one("drying_running", "ON"):
                # Publish failed after arming: stay uncertain until telemetry says otherwise
                self.start_status = "uncertain"
                self._latch_seq = self._seq
                return False
            ok = await self._wait_start_confirmed(after_seq=point, hours=hours,
                                                  timeout=START_READBACK_S,
                                                  cancelled=cancelled)
        finally:
            self._start_op_busy = False
        if not ok:
            self.start_status = "uncertain"     # latch stands until fresh state resolves
            self._latch_seq = self._seq         # only packets arriving AFTER now can resolve
            self._log("[NATIVE-DRY] start UNCONFIRMED — no retry; waiting on telemetry")
            return False
        self.start_status = "confirmed"
        self._latch_seq = None
        if pre_start_temp >= float(temp):
            self.heating_status = "at_target"
            self._log("[NATIVE-DRY] timer running; chamber already at target — "
                      "no new temperature rise claimed")
        else:
            self.heating_status = "pending"
            self._start_heat_watch(pre_start_temp, float(temp))
        return True

    async def _run_start_steps(self, steps, cancelled) -> bool:
        """Config writes with per-field device read-back.

        The caller owns _start_op_busy across this call."""
        for field, value in steps:
            if cancelled():
                self.start_status = "failed"
                return False
            point = self._seq
            if not await self._publish_one(field, value):
                self.start_status = "failed"
                return False
            ok = await self._wait_field(field, value, after_seq=point,
                                        timeout=CONFIG_READBACK_S,
                                        cancelled=cancelled)
            if not ok:
                self.start_status = "failed"
                if cancelled():
                    return False
                self._log(f"[NATIVE-DRY] config {field}={value} not confirmed by device")
                return False
            if cancelled():
                self.start_status = "failed"
                return False

        if cancelled():
            self.start_status = "failed"
            return False
        return True

    async def _publish_one(self, field: str, value) -> bool:
        if not self._broker_connected or self._client is None:
            return False
        is_off = field in ("work_on", "drying_running") and value == "OFF"
        if not is_off and not self.available:
            return False
        try:
            r = self._client.publish(self.topic_command, json.dumps({field: self._wire_value(field, value)}),
                                     qos=0, retain=False)
        except Exception as e:
            self._log(f"[NATIVE-DRY] publish error: {e}")
            return False
        rc = r[0] if isinstance(r, tuple) else getattr(r, "rc", -1)
        return rc == 0

    def _arm_uncertain(self, publish_seq: int) -> None:
        self._latch_seq = publish_seq
        self.start_status = "uncertain"

    POLL_TICK_S = 0.015    # real-time ceiling for waits measured on the injectable clock

    async def _tick(self) -> None:
        """Bounded wait for the next live packet.

        Deadlines run on the injectable clock (tests drive it), so every poll
        must also yield real event-loop time — otherwise an un-advanced fake
        clock could spin a wait forever in wall-clock terms."""
        if self._new_data is not None:
            self._new_data.clear()
            try:
                await asyncio.wait_for(self._new_data.wait(), timeout=self.POLL_TICK_S)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(self.POLL_TICK_S)

    async def _wait_field(self, field: str, expected: str, *, after_seq: int,
                          timeout: float, cancelled: Callable[[], bool]) -> bool:
        """Wait for a POST-WRITE live snapshot whose native field matches.

        A fresh packet that CONTRADICTS the write fails immediately; packets at
        or before ``after_seq`` (cached / pre-write) are never accepted.
        """
        deadline = self._clock() + timeout
        while True:
            if cancelled() or not self.available:
                return False
            snap = self._latest
            if snap is not None and snap[0] > after_seq:
                native = snap[2]
                if field in native:
                    if self._field_matches(native[field], field, expected):
                        return True
                    return False
                if field == "filament_drying_mode":
                    # An unrelated live packet does not confirm custom selection.
                    return False
            if cancelled():
                return False
            if not self._broker_connected:
                return False
            if self._clock() >= deadline:
                return False
            await self._tick()

    @staticmethod
    def _field_matches(actual, field, expected: str) -> bool:
        if field in ("custom_temp", "custom_timer"):
            # Exact numeric equality: a device echo of 4.5 for a requested 4 is
            # a MISMATCH, not an int()-truncated confirmation (Sol review).
            return _is_finite_number(actual) and float(actual) == float(expected)
        return actual == expected

    async def _wait_start_confirmed(self, *, after_seq: int, hours: int,
                                    timeout: float,
                                    cancelled: Callable[[], bool]) -> bool:
        deadline = self._clock() + timeout
        while True:
            if cancelled() or not self.available:
                return False
            snap = self._latest
            if snap is not None and snap[0] > after_seq:
                native = snap[2]
                running = _strict_on_off(native.get("drying_running"))
                remaining = native.get("drying_remaining_min")
                # A drying_running=ON echo from a NON-drying mode snapshot does
                # not prove the dry cycle runs: require the firmware's own mode
                # field (Sol review).
                if running and native.get("mode") == "filament drying" \
                        and _is_finite_number(remaining) \
                        and 1 <= float(remaining) <= hours * 60:
                    return True
            if cancelled() or not self._broker_connected:
                return False
            if self._clock() >= deadline:
                return False
            await self._tick()

    # ── heating proof (separate from timer confirmation) ───────────────────
    def _start_heat_watch(self, pre_temp: float, target: float) -> None:
        self._heat_pre_temp = pre_temp
        self._heat_deadline = self._clock() + HEAT_PROOF_S
        if self._heat_task is not None:
            self._heat_task.cancel()
        try:
            self._heat_task = asyncio.get_running_loop().create_task(self._heat_watchdog())
        except RuntimeError:
            self._heat_task = None

    async def _heat_watchdog(self) -> None:
        try:
            while self.heating_status == "pending" and self._client is not None:
                native = self._latest_native()
                if native is not None:
                    self._observe_heating(native)
                if self.heating_status == "pending" and \
                        self._clock() >= (self._heat_deadline or 0):
                    self.heating_status = "unconfirmed"
                    self._log("[NATIVE-DRY] no >=2C rise within 5 min — "
                              "heating NOT confirmed (timer state unchanged)")
                await self._tick()
        except asyncio.CancelledError:
            pass

    def _observe_heating(self, native: dict) -> None:
        if not self.available or self.heating_status != "pending" or self._heat_pre_temp is None:
            return
        # Deadline is checked FIRST: a >=2C rise observed after the 5-minute
        # window must NOT confirm (Sol review).
        if self._heat_deadline is not None and self._clock() >= self._heat_deadline:
            self.heating_status = "unconfirmed"
            return
        temp = native.get("chamber_temp")
        if _is_finite_number(temp) and temp >= self._heat_pre_temp + HEAT_RISE_C:
            self.heating_status = "confirmed"
            self._log(f"[NATIVE-DRY] heating confirmed: {self._heat_pre_temp:.1f}C "
                      f"-> {temp:.1f}C (>= +{HEAT_RISE_C}C)")

    # ── emergency stop ───────────────────────────────────────────────────────
    async def emergency_stop(self) -> bool:
        """Publish the native off pair and CONFIRM it only from post-write
        device telemetry. Never claims heater-off from broker rc alone."""
        self._stop_generation += 1
        if not self._broker_connected or self._client is None:
            return False
        point = self._seq
        for field in ("work_on", "drying_running"):
            if not await self._publish_one(field, "OFF"):
                return False
        deadline = self._clock() + OFF_READBACK_S
        while True:
            snap = self._latest
            if self.available and snap is not None and snap[0] > point:
                native = snap[2]
                if _strict_on_off(native.get("work_on")) is False and \
                        _strict_on_off(native.get("drying_running")) is False:
                    return True
            if self._clock() >= deadline:
                return False
            await self._tick()
