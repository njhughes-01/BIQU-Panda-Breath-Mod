"""Tests for drying_supervisor — the persisted supervised-drying session owner.

The supervisor owns one drying intent: it persists arm/cancel/complete state,
enforces one fixed host deadline, and is the sole caller of the transport's
recovery and supervised-stop operations. No real device, broker or file
service: state lives in a tmp_path JSON file and the transport is a duck-typed
fake implementing exactly the three methods the supervisor may call
(drying_snapshot / recover_drying / stop_supervised).
"""
import asyncio
import json
import os

import pytest

from drying_supervisor import DryingSupervisor


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeTransport:
    """Duck-typed transport seam. snapshot None == device offline/stale."""

    def __init__(self):
        self._snap = None
        self._seq = 0
        self.recover_calls = []          # (temp_c, deadline, cancelled)
        self.recover_results = []        # consumed in order; default confirmed
        self.recover_gate = None         # asyncio.Event gating completion
        self.recover_cancel_seen = False
        self.stop_calls = 0
        self.stop_results = []           # consumed in order; default confirmed

    def push(self, running=False, **over):
        """Deliver a NEW fresh live snapshot (distinct sequence number)."""
        self._seq += 1
        native = {
            "mode": "filament drying" if running or over else "auto mode",
            "work_on": "ON" if running else "OFF",
            "chamber_temp": 24.5,
            "drying_running": "ON" if running else "OFF",
            "custom_temp": 50,
            "custom_timer": 8,
            "drying_remaining_min": 120 if running else 0,
        }
        native.update(over)
        self._snap = (self._seq, native)

    def drop(self):
        self._snap = None

    def drying_snapshot(self):
        return self._snap

    async def recover_drying(self, temp_c, deadline, cancelled):
        self.recover_calls.append((temp_c, deadline, cancelled))
        if self.recover_gate is not None:
            while not self.recover_gate.is_set():
                if cancelled():
                    self.recover_cancel_seen = True
                    return "failed"
                await asyncio.sleep(0.01)
            if cancelled():
                self.recover_cancel_seen = True
                return "failed"
        if self.recover_results:
            return self.recover_results.pop(0)
        return "confirmed"

    async def stop_supervised(self):
        self.stop_calls += 1
        if self.stop_results:
            return self.stop_results.pop(0)
        return "confirmed"


def make_sup(tmp_path, **kw):
    clock = kw.pop("clock", None) or FakeClock()
    mono = kw.pop("monotonic", None) or FakeClock()
    sup = DryingSupervisor(str(tmp_path / "drying_session.json"),
                           clock=clock, monotonic=mono, **kw)
    return sup, clock, mono


def persisted(tmp_path):
    with open(tmp_path / "drying_session.json") as f:
        return json.load(f)


# ═════════════════════════════ Session persistence and arm ══════════════════

def test_arm_persists_session_and_rejects_second_start_without_moving_deadline(tmp_path):
    sup, clock, _ = make_sup(tmp_path)
    assert sup.arm(50, 8) is True
    saved = persisted(tmp_path)
    assert saved["status"] == "running"
    assert saved["target_c"] == 50
    assert saved["deadline"] == pytest.approx(clock.t + 8 * 3600)
    assert saved["pending_stop"] is False
    assert isinstance(saved["session_id"], str) and saved["session_id"]
    # second explicit start while a supervised session is active: rejected,
    # the fixed deadline is not moved
    clock.advance(120.0)
    assert sup.arm(45, 1) is False
    assert persisted(tmp_path)["deadline"] == pytest.approx(saved["deadline"])
    assert persisted(tmp_path)["session_id"] == saved["session_id"]


def test_arm_fails_closed_without_persistence(tmp_path):
    # a missing parent directory can never be written: no arm, no state
    sup = DryingSupervisor(str(tmp_path / "gone" / "session.json"))
    assert sup.arm(50, 8) is False
    assert sup.status()["status"] == "inactive"


# ═════════════════════════════ Reconnect observation rules ═══════════════════

def test_running_reconnect_is_observe_only_with_no_writes(tmp_path):
    async def scenario():
        sup, clock, mono = make_sup(tmp_path)
        assert sup.arm(50, 8) is True
        deadline = sup.status()["deadline"]
        tr = FakeTransport()
        # backend restart mid-session: fresh supervisor over the same file
        sup2 = DryingSupervisor(str(tmp_path / "drying_session.json"),
                                clock=clock, monotonic=mono)
        clock.advance(300.0)                       # outage time counts
        tr.drop()
        await sup2.tick(tr, lambda: False)
        await sup2.tick(tr, lambda: False)         # reconnect, still no state
        assert tr.recover_calls == [] and tr.stop_calls == 0
        tr.push(running=True)                      # fresh device confirms running
        await sup2.tick(tr, lambda: False)
        tr.push(running=True)
        await sup2.tick(tr, lambda: False)
        assert tr.recover_calls == [] and tr.stop_calls == 0
        assert sup2.status()["deadline"] == pytest.approx(deadline)
        assert persisted(tmp_path)["deadline"] == pytest.approx(deadline)
    asyncio.run(scenario())


def test_fresh_off_recovery_uses_persisted_deadline_after_two_distinct_snapshots(tmp_path):
    async def scenario():
        sup, clock, mono = make_sup(tmp_path)
        assert sup.arm(50, 8) is True
        deadline = sup.status()["deadline"]
        clock.advance(3600.0)                      # clock moves; deadline fixed
        tr = FakeTransport()
        tr.push()
        await sup.tick(tr, lambda: False)          # first fresh OFF snapshot
        assert tr.recover_calls == []
        tr.push()
        await sup.tick(tr, lambda: False)          # second, distinct one
        assert len(tr.recover_calls) == 1
        assert tr.recover_calls[0][0] == 50
        assert tr.recover_calls[0][1] == pytest.approx(deadline)
        assert tr.stop_calls == 0
        assert persisted(tmp_path)["recovering"] is False
        assert persisted(tmp_path)["status"] == "running"
    asyncio.run(scenario())


# ═════════════════════════════ Uncertain start handling ══════════════════════

def test_uncertain_start_waits_for_fresh_state_then_cooldown_before_retry(tmp_path):
    async def scenario():
        sup, clock, mono = make_sup(tmp_path)
        assert sup.arm(50, 8) is True
        tr = FakeTransport()
        tr.recover_results = ["uncertain", "confirmed"]
        tr.push(); await sup.tick(tr, lambda: False)
        tr.push(); await sup.tick(tr, lambda: False)
        assert len(tr.recover_calls) == 1
        # awaiting an uncertain outcome: NO retry until a fresh snapshot
        # arriving after the attempt resolves it...
        for _ in range(4):
            tr.push()
            await sup.tick(tr, lambda: False)
        assert len(tr.recover_calls) == 1
        # ...and then at most one attempt per 60 s of monotonic time
        for _ in range(3):
            tr.push()
            await sup.tick(tr, lambda: False)
        assert len(tr.recover_calls) == 1
        mono.advance(61.0)
        tr.push()
        await sup.tick(tr, lambda: False)          # resolves + first fresh OFF
        tr.push()
        await sup.tick(tr, lambda: False)          # second fresh OFF: retry
        assert len(tr.recover_calls) == 2
        assert persisted(tmp_path)["recovering"] is False
    asyncio.run(scenario())


# ═════════════════════════════ Cancellation races and reload ═════════════════

def test_cancel_wins_over_in_flight_recovery_persists_and_stops(tmp_path):
    async def scenario():
        sup, clock, _ = make_sup(tmp_path)
        assert sup.arm(50, 8) is True
        tr = FakeTransport()
        tr.recover_gate = asyncio.Event()
        tr.push(); await sup.tick(tr, lambda: False)
        tr.push()
        running = asyncio.create_task(sup.tick(tr, lambda: False))
        for _ in range(50):                        # wait until the attempt is in flight
            if tr.recover_calls:
                break
            await asyncio.sleep(0.01)
        assert tr.recover_calls, "recovery never started"
        assert sup.cancel() is True
        tr.recover_gate.set()
        await asyncio.wait_for(running, 5)
        assert tr.recover_cancel_seen is True      # cancellation seen by the operation
        saved = persisted(tmp_path)
        assert saved["status"] == "cancelled" and saved["pending_stop"] is True
        # the cancelled session must never recover again, and its pending
        # stop is delivered from fresh state
        tr.push(); await sup.tick(tr, lambda: False)
        assert len(tr.recover_calls) == 1
        assert tr.stop_calls == 1
        assert persisted(tmp_path)["pending_stop"] is False
        await sup.tick(tr, lambda: False)
        assert tr.stop_calls == 1                  # delivered once, then done
    asyncio.run(scenario())


def test_reload_of_cancelled_session_keeps_pending_stop_and_never_recovers(tmp_path):
    async def scenario():
        sup, clock, _ = make_sup(tmp_path)
        assert sup.arm(50, 8) is True
        assert sup.cancel() is True
        sup2, clock2, mono2 = make_sup(tmp_path)   # backend restart over same file
        st = sup2.status()
        assert st["status"] == "cancelled" and st["pending_stop"] is True
        tr = FakeTransport()
        for _ in range(3):
            tr.push()
            await sup2.tick(tr, lambda: False)
        assert tr.recover_calls == []
        assert tr.stop_calls == 1
        assert persisted(tmp_path)["pending_stop"] is False
        assert sup2.status()["status"] == "cancelled"
    asyncio.run(scenario())


# ═════════════════════════════ Host deadline ownership ═══════════════════════

def test_expiry_while_offline_persists_pending_stop_then_delivers_it(tmp_path):
    async def scenario():
        sup, clock, mono = make_sup(tmp_path)
        assert sup.arm(50, 1) is True
        tr = FakeTransport()
        tr.stop_results = ["uncertain", "confirmed"]
        clock.advance(3601.0)                      # expiry during the outage
        tr.drop()
        await sup.tick(tr, lambda: False)          # offline: obligation persisted
        await sup.tick(None, lambda: False)        # no transport object either
        saved = persisted(tmp_path)
        assert saved["status"] == "completed" and saved["pending_stop"] is True
        assert tr.stop_calls == 0
        tr.push()                                  # device reachable again
        await sup.tick(tr, lambda: False)
        assert tr.stop_calls == 1                  # Stop requested at expiry
        assert persisted(tmp_path)["pending_stop"] is True   # uncertain → kept
        tr.push()
        await sup.tick(tr, lambda: False)          # fresh OFF confirms
        assert persisted(tmp_path)["pending_stop"] is False
        assert sup.status()["status"] == "completed"
        assert tr.recover_calls == []              # expired session never rearms
    asyncio.run(scenario())


def test_expiry_tick_requests_stop_instead_of_recovering(tmp_path):
    async def scenario():
        sup, clock, _ = make_sup(tmp_path)
        assert sup.arm(50, 1) is True
        clock.advance(3600.0)                      # exactly at the fixed deadline
        tr = FakeTransport()
        tr.push()                                  # fresh OFF: would invite recovery
        await sup.tick(tr, lambda: False)          # expiry runs BEFORE recovery
        assert tr.recover_calls == []
        assert tr.stop_calls == 1
        assert sup.status()["status"] == "completed"
        assert persisted(tmp_path)["pending_stop"] is False  # confirmed stop cleared
    asyncio.run(scenario())


# ═════════════════════════════ Inhibitions (never arm ON blindly) ════════════

def test_persistence_failure_and_clock_regression_inhibit_start(tmp_path):
    async def scenario():
        sup, clock, mono = make_sup(tmp_path)
        assert sup.arm(50, 8) is True
        tr = FakeTransport()
        sup._save = lambda: False                  # persistence just died
        tr.push(); await sup.tick(tr, lambda: False)
        tr.push(); await sup.tick(tr, lambda: False)
        assert tr.recover_calls == []              # no ON without persistence
        del sup._save                              # persistence restored
        mono.advance(-5.0)                         # monotonic clock regression
        tr.push(); await sup.tick(tr, lambda: False)
        assert tr.recover_calls == []
        tr.push(); await sup.tick(tr, lambda: False)
        assert tr.recover_calls == []
        mono.advance(70.0)
        tr.push(); await sup.tick(tr, lambda: False)
        tr.push(); await sup.tick(tr, lambda: False)
        assert len(tr.recover_calls) == 1
    asyncio.run(scenario())


def test_invalid_persisted_state_stays_inactive_and_inhibits(tmp_path):
    async def scenario():
        path = tmp_path / "drying_session.json"
        path.write_text("{not json at all")
        sup, clock, mono = make_sup(tmp_path)
        st = sup.status()
        assert st["status"] == "inactive"
        tr = FakeTransport()
        for _ in range(4):
            tr.push()
            await sup.tick(tr, lambda: False)
        assert tr.recover_calls == [] and tr.stop_calls == 0
        # a plain dict copy: mutating the caller's copy changes nothing
        st["status"] = "hacked"
        assert sup.status()["status"] == "inactive"
        # an explicit new request may arm again over the invalid file
        assert sup.arm(50, 8) is True
        assert persisted(tmp_path)["status"] == "running"
    asyncio.run(scenario())
