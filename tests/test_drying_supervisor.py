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
import time

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
        # mirrors emergency_stop: the OFF is published whenever the broker is
        # there, but only post-write FRESH telemetry can confirm it.
        return "confirmed" if self._snap is not None else "uncertain"


async def tick(sup, tr, cancelled=None):
    """One service pass, plus the recovery it may have handed to a task.

    The supervisor deliberately does NOT await the recovery under its service
    lock, so the test settles the tracked task here — the 1 Hz loop would just
    keep ticking while that operation runs."""
    await sup.tick(tr, cancelled or (lambda: False))
    task = sup._recover_task
    if task is not None:
        await task


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
        await tick(sup2, tr)
        await tick(sup2, tr)         # reconnect, still no state
        assert tr.recover_calls == [] and tr.stop_calls == 0
        tr.push(running=True)                      # fresh device confirms running
        await tick(sup2, tr)
        tr.push(running=True)
        await tick(sup2, tr)
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
        await tick(sup, tr)          # first fresh OFF snapshot
        assert tr.recover_calls == []
        tr.push()
        await tick(sup, tr)          # second, distinct one
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
        tr.push(); await tick(sup, tr)
        tr.push(); await tick(sup, tr)
        assert len(tr.recover_calls) == 1
        # awaiting an uncertain outcome: NO retry until a fresh snapshot
        # arriving after the attempt resolves it...
        for _ in range(4):
            tr.push()
            await tick(sup, tr)
        assert len(tr.recover_calls) == 1
        # ...and then at most one attempt per 60 s of monotonic time
        for _ in range(3):
            tr.push()
            await tick(sup, tr)
        assert len(tr.recover_calls) == 1
        mono.advance(61.0)
        tr.push()
        await tick(sup, tr)          # resolves + first fresh OFF
        tr.push()
        await tick(sup, tr)          # second fresh OFF: retry
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
        tr.push(); await tick(sup, tr)
        tr.push()
        await asyncio.wait_for(sup.tick(tr, lambda: False), 1)   # hands the attempt off
        for _ in range(50):                        # wait until the attempt is in flight
            if tr.recover_calls:
                break
            await asyncio.sleep(0.01)
        assert tr.recover_calls, "recovery never started"
        # the service lock stays free while the operation runs: the next tick
        # returns at once and starts no second attempt
        await asyncio.wait_for(sup.tick(tr, lambda: False), 1)
        assert len(tr.recover_calls) == 1
        assert sup.cancel() is True
        tr.recover_gate.set()
        await asyncio.wait_for(sup._recover_task, 5)
        assert tr.recover_cancel_seen is True      # cancellation seen by the operation
        saved = persisted(tmp_path)
        assert saved["status"] == "cancelled" and saved["pending_stop"] is True
        # the cancelled session must never recover again, and its pending
        # stop is delivered from fresh state
        tr.push(); await tick(sup, tr)
        assert len(tr.recover_calls) == 1
        assert tr.stop_calls == 1
        assert persisted(tmp_path)["pending_stop"] is False
        await tick(sup, tr)
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
            await tick(sup2, tr)
        assert tr.recover_calls == []
        assert tr.stop_calls == 1
        assert persisted(tmp_path)["pending_stop"] is False
        assert sup2.status()["status"] == "cancelled"
    asyncio.run(scenario())


# ═════════════════════════════ Host deadline ownership ═══════════════════════

def test_expiry_offline_or_stale_still_attempts_off_and_confirms_only_fresh(tmp_path):
    # A stale/restarted snapshot must never suppress the owed Stop: the OFF is
    # attempted whenever a transport exists, and only FRESH telemetry confirms.
    async def scenario():
        sup, clock, mono = make_sup(tmp_path)
        assert sup.arm(50, 1) is True
        tr = FakeTransport()
        tr.stop_results = ["uncertain", "confirmed"]
        clock.advance(3601.0)                      # expiry during the outage
        tr.drop()                                  # telemetry stale
        await tick(sup, tr)                        # Stop attempted anyway
        assert tr.stop_calls == 1
        saved = persisted(tmp_path)
        assert saved["status"] == "completed" and saved["pending_stop"] is True
        await tick(sup, None)                      # no transport object: obligation stands
        assert tr.stop_calls == 1
        assert persisted(tmp_path)["pending_stop"] is True
        tr.push()                                  # device reachable again
        await tick(sup, tr)
        assert tr.stop_calls == 2                  # Stop requested again at expiry
        assert persisted(tmp_path)["pending_stop"] is False  # fresh OFF confirms
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
        await tick(sup, tr)          # expiry runs BEFORE recovery
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
        tr.push(); await tick(sup, tr)
        tr.push(); await tick(sup, tr)
        assert tr.recover_calls == []              # no ON without persistence
        del sup._save                              # persistence restored
        mono.advance(-5.0)                         # monotonic clock regression
        tr.push(); await tick(sup, tr)
        assert tr.recover_calls == []
        tr.push(); await tick(sup, tr)
        assert tr.recover_calls == []
        mono.advance(70.0)
        tr.push(); await tick(sup, tr)
        tr.push(); await tick(sup, tr)
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
            await tick(sup, tr)
        assert tr.recover_calls == [] and tr.stop_calls == 0
        # a plain dict copy: mutating the caller's copy changes nothing
        st["status"] = "hacked"
        assert sup.status()["status"] == "inactive"
        # an explicit new request may arm again over the invalid file
        assert sup.arm(50, 8) is True
        assert persisted(tmp_path)["status"] == "running"
    asyncio.run(scenario())


# ═════════════ Monotonic anchor, transport identity, owed Stop, robust load ══

def test_wall_rollback_never_extends_the_anchored_monotonic_deadline(tmp_path):
    async def scenario():
        sup, clock, mono = make_sup(tmp_path)
        assert sup.arm(50, 1) is True
        clock.advance(-7200.0)                     # NTP rolls the wall clock back 2 h
        mono.advance(3601.0)                       # the real hour elapsed anyway
        tr = FakeTransport()
        tr.stop_results = ["uncertain"]            # nobody claims an unproven stop
        tr.push()
        await tick(sup, tr)
        assert sup.status()["status"] == "completed"
        assert tr.recover_calls == []              # a rolled-back clock earns no extra ON
        assert tr.stop_calls == 1                  # expiry still owned its Stop
        saved = persisted(tmp_path)
        assert saved["pending_stop"] is True
        # the persisted wall deadline is never rewritten to follow the rollback
        assert saved["deadline"] == pytest.approx(1000.0 + 3600.0)
    asyncio.run(scenario())


def test_restored_duration_is_bounded_and_re_anchored(tmp_path):
    path = tmp_path / "drying_session.json"
    bogus = {"session_id": "x" * 8, "target_c": 50, "status": "running",
             "pending_stop": False, "recovering": False,
             "deadline": 1000.0 + 400 * 3600.0}    # longer than the device contract
    path.write_text(json.dumps(bogus))
    sup, clock, mono = make_sup(tmp_path)          # clock starts at 1000 → 400 h left
    assert sup.status()["status"] == "inactive"    # never trusted, recovery inhibited

    async def scenario():
        tr = FakeTransport()
        for _ in range(4):
            tr.push()
            await tick(sup, tr)
        assert tr.recover_calls == [] and tr.stop_calls == 0
        # a legitimate remaining duration restores and expires on this
        # process' own monotonic anchor, not on a rolled-back wall clock
        sane = dict(bogus, session_id="y" * 8, deadline=1000.0 + 2 * 3600.0)
        path.write_text(json.dumps(sane))
        sup2, clock2, mono2 = make_sup(tmp_path)
        assert sup2.status()["status"] == "running"
        clock2.advance(-10800.0)                   # wall clock two hours back
        mono2.advance(7201.0)                      # its own two hours elapsed
        tr.push()
        await tick(sup2, tr)
        assert sup2.status()["status"] == "completed"
        assert tr.recover_calls == []
    asyncio.run(scenario())


def test_transport_replacement_resets_observation_and_unwedges_the_gate(tmp_path):
    async def scenario():
        sup, clock, mono = make_sup(tmp_path)
        assert sup.arm(50, 8) is True
        tr1 = FakeTransport()
        tr1._seq = 500                             # the old link numbered packets for hours
        tr1.recover_results = ["uncertain"]
        tr1.push(); await tick(sup, tr1)
        tr1.push(); await tick(sup, tr1)
        assert len(tr1.recover_calls) == 1         # resolution gate pinned to ~502
        mono.advance(61.0)                         # cooldown over
        # Replacement transport: its sequence counter starts again at 1, so a
        # gate inherited from the old link would not open for hundreds of packets.
        tr2 = FakeTransport()
        tr2.push()
        await tick(sup, tr2)                       # one fresh OFF on the new link
        assert tr2.recover_calls == []
        tr2.push()
        await tick(sup, tr2)                       # second one: decides on THIS link
        assert len(tr2.recover_calls) == 1
        assert len(tr1.recover_calls) == 1
        assert persisted(tmp_path)["status"] == "running"
    asyncio.run(scenario())


def test_expiry_during_in_flight_recovery_stops_at_once_and_vetoes_the_on(tmp_path):
    async def scenario():
        sup, clock, mono = make_sup(tmp_path)
        assert sup.arm(50, 1) is True
        tr = FakeTransport()
        tr.recover_gate = asyncio.Event()          # a long operation in the transport
        tr.stop_results = ["uncertain"]
        tr.push(); await tick(sup, tr)
        tr.push(); await asyncio.wait_for(sup.tick(tr, lambda: False), 1)
        while not tr.recover_calls:
            await asyncio.sleep(0.01)
        clock.advance(3601.0)                      # deadline passes mid-operation
        tr.push()
        await asyncio.wait_for(sup.tick(tr, lambda: False), 1)
        assert sup.status()["status"] == "completed"
        assert tr.stop_calls == 1                  # Stop now, not after its read-backs
        assert persisted(tmp_path)["pending_stop"] is True
        assert len(tr.recover_calls) == 1          # expiry also closes the recovery door
        tr.recover_gate.set()
        await asyncio.wait_for(sup._recover_task, 5)
        assert tr.recover_cancel_seen is True      # the veto reached the operation
        assert len(tr.recover_calls) == 1
        saved = persisted(tmp_path)
        assert saved["status"] == "completed" and saved["recovering"] is False
        # and the owed Stop keeps being attempted until fresh state confirms it
        tr.push(); await tick(sup, tr)
        assert tr.stop_calls == 2
    asyncio.run(scenario())


def test_arm_refuses_while_a_stop_from_the_previous_session_is_owed(tmp_path):
    async def scenario():
        sup, clock, _ = make_sup(tmp_path)
        assert sup.arm(50, 1) is True
        assert sup.cancel() is True
        assert sup.arm(45, 2) is False             # the owed Stop is never overwritten
        saved = persisted(tmp_path)
        assert saved["status"] == "cancelled" and saved["pending_stop"] is True
        tr = FakeTransport()
        tr.push()
        await tick(sup, tr)                        # confirmed by fresh device state
        assert persisted(tmp_path)["pending_stop"] is False
        assert sup.arm(50, 1) is True              # only then may a new session arm
        assert persisted(tmp_path)["status"] == "running"
    asyncio.run(scenario())


def test_malformed_state_with_an_unusable_logger_still_inhibits(tmp_path):
    # Panda builds the supervisor while its own logger may not exist yet: a
    # corrupt session file must land as inactive+inhibited, never as a crash.
    def boom(msg):
        raise RuntimeError("logger not ready")

    path = tmp_path / "drying_session.json"
    path.write_text("{not json at all")
    sup = DryingSupervisor(str(path), logger=boom)
    assert sup.status()["status"] == "inactive"

    async def scenario():
        tr = FakeTransport()
        for _ in range(4):
            tr.push()
            await tick(sup, tr)
        assert tr.recover_calls == [] and tr.stop_calls == 0
        # an out-of-contract restored duration is unusable too, same handling
        path.write_text(json.dumps({
            "session_id": "z" * 8, "target_c": 50, "status": "running",
            "pending_stop": False, "recovering": False,
            "deadline": time.time() + 500 * 3600.0}))
        sup2 = DryingSupervisor(str(path), logger=boom)
        assert sup2.status()["status"] == "inactive"
        for _ in range(4):
            tr.push()
            await tick(sup2, tr)
        assert tr.recover_calls == []
    asyncio.run(scenario())


def test_cancel_remains_durable_after_older_save_finishes(tmp_path, monkeypatch):
    """An older serialized RUNNING record must not replace a saved Stop."""
    import threading

    path = tmp_path / "session.json"
    supervisor = DryingSupervisor(path)
    assert supervisor.arm(50, 8)
    replace_waiting = threading.Event()
    release_old_save = threading.Event()
    cancel_finished = threading.Event()
    real_replace = os.replace
    failures = []

    def held_replace(src, dst):
        if threading.current_thread().name == "older-save":
            replace_waiting.set()
            if not release_old_save.wait(3):
                raise RuntimeError("test did not release old save")
        return real_replace(src, dst)

    def old_save():
        try:
            supervisor._save()
        except Exception as exc:
            failures.append(exc)

    def cancel():
        try:
            assert supervisor.cancel()
        except Exception as exc:
            failures.append(exc)
        finally:
            cancel_finished.set()

    monkeypatch.setattr(os, "replace", held_replace)
    writer = threading.Thread(target=old_save, name="older-save")
    stopper = threading.Thread(target=cancel, name="stopper")
    writer.start()
    assert replace_waiting.wait(3)
    stopper.start()
    try:
        # Without synchronization cancellation finishes first, then the old
        # RUNNING file replaces it. With synchronization Stop waits its turn.
        cancel_finished.wait(1)
    finally:
        release_old_save.set()
        writer.join(3)
        stopper.join(3)
    assert not writer.is_alive() and not stopper.is_alive()
    assert not failures
    restored = DryingSupervisor(path).status()
    assert restored["status"] == "cancelled"
    assert restored["pending_stop"] is True
