"""Supervised drying session owner (openspec/changes/supervised-drying).

One DryingSupervisor instance owns one explicit drying intent across Panda
reconnects and backend restarts:

* ``arm()`` persists the session (uuid, target, fixed wall-clock deadline)
  BEFORE it returns — a session that cannot be persisted does not exist.
* ``cancel()`` persists the cancellation and the pending-stop obligation
  BEFORE returning, so a Stop is still owed after a restart.
* ``tick(transport, cancelled)`` is the single async entry point, serialized
  by an internal lock. It enforces the fixed host deadline (the host owns the
  countdown and requests Stop at expiry) before any recovery, and is the only
  caller of the transport's ``recover_drying`` / ``stop_supervised``.

Rules enforced here (design.md):
* Fresh device state showing drying running means observe only — no writes.
* Recovery requires two distinct fresh OFF snapshots (by transport sequence
  number), then is rate-limited to one attempt per minute; an uncertain or
  in-flight start is never retried until a fresh snapshot arriving after the
  attempt resolves it. Sequence numbers are scoped to the transport object that
  produced them: a replacement transport restarts its own counter, so the
  observation and the resolution gate reset with it instead of wedging.
* Exactly one recovery operation is in flight and it is NOT awaited inside the
  service lock: the 1 Hz tick keeps running (and keeps enforcing the deadline)
  while the transport works through its configuration read-backs.
* Persistence failure, an invalid persisted state, an unusable clock, a
  monotonic clock regression, elapsed deadline or the cancellation callback
  prohibit ON.
* The finish time is enforced twice: the persisted wall-clock deadline survives
  a restart, and an anchored monotonic deadline guarantees a wall-clock
  rollback can never extend a running session.
* A terminal session (cancelled or completed) with a pending Stop attempts the
  device OFF on every pass a transport object exists — stale telemetry may not
  confirm it, but it must not suppress the attempt.
* An idle device or a plain reconnect never adopts a session: only ``arm()``
  creates one, and only the supervisor starts a supervised drying operation.
No file I/O happens at import; state loads only in the constructor.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
import weakref
from typing import Callable, Optional

# Device contract bounds (mirrors panda_native_mqtt; kept here so the
# supervisor validates persisted state without importing the transport).
DRY_TEMP_MIN, DRY_TEMP_MAX = 40, 60
DRY_HOURS_MIN, DRY_HOURS_MAX = 1, 99

_STATUS_VALUES = ("running", "cancelled", "completed")

# A restored running session may never claim more remaining time than the
# device contract allows; anything larger means the persisted file is not
# trustworthy (or the wall clock is) and must inhibit recovery.
MAX_RESTORED_REMAINING_S = DRY_HOURS_MAX * 3600.0


def _safe_logger(logger: Optional[Callable[[str], None]]) -> Callable[[str], None]:
    """Logging must never be the reason a safety path fails.

    The supervisor logs while it loads a corrupt file, i.e. during the host's
    import; a logger that is not ready yet (or that raises) has to be ignored
    so the malformed state still lands as "inactive and inhibited"."""
    def _log(msg: str) -> None:
        if logger is None:
            return
        try:
            logger(msg)
        except Exception:
            pass
    return _log


def _is_finite_number(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return v == v and v not in (float("inf"), float("-inf"))


class DryingSupervisor:
    """Persisted drying intent with one fixed deadline and confirmed-only ON."""

    RECOVER_COOLDOWN_S = 60.0      # at most one recovery attempt per minute
    OFF_SNAPSHOTS_REQUIRED = 2     # distinct fresh OFF snapshots before recovery

    def __init__(self, path, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 logger: Optional[Callable[[str], None]] = None):
        self.path = str(path)
        self._clock = clock
        self._monotonic = monotonic
        self._log = _safe_logger(logger)
        self._lock = asyncio.Lock()
        self._state: Optional[dict] = None   # the one persisted session record
        self._persist_error = False
        # runtime-only recovery bookkeeping (fresh per process by design)
        self._off_count = 0
        self._last_off_seq: Optional[int] = None
        self._gate_seq: Optional[int] = None   # fresh seq > this resolves an attempt
        self._last_attempt_mono: Optional[float] = None
        self._last_mono: Optional[float] = None
        # Anchored monotonic finish time for the running session (never
        # persisted: it is meaningless in another process). ``None`` when the
        # record is terminal or the monotonic clock was unusable at load.
        self._deadline_mono: Optional[float] = None
        # The one recovery operation allowed in flight, tracked so the service
        # lock is never held across its read-back waits.
        self._recover_task: Optional[asyncio.Task] = None
        # Transport identity the observation belongs to (weak on purpose: a
        # dead transport must not be kept alive by the supervisor).
        self._obs_ref = None
        self._obs_keepalive = None
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self._log("[DRY-SUP] persisted session unreadable: starting inactive")
            self._persist_error = True
            return
        if self._valid_record(raw) and raw["status"] == "running":
            # Restored remaining time is bounded and re-anchored to this
            # process' monotonic clock, so neither a bogus persisted deadline
            # nor a wall-clock rollback can stretch a running session.
            try:
                now = float(self._clock())
                mono = float(self._monotonic())
            except Exception:
                now = mono = float("nan")
            remaining = (raw["deadline"] - now) if _is_finite_number(now) else float("nan")
            if not _is_finite_number(remaining) or remaining > MAX_RESTORED_REMAINING_S:
                self._log("[DRY-SUP] persisted session duration unusable: "
                          "starting inactive")
                self._persist_error = True
                return
            self._state = raw
            self._deadline_mono = (mono + max(0.0, remaining)) if _is_finite_number(mono) else None
            return
        if self._valid_record(raw):
            self._state = raw
        else:
            # Invalid persisted data must inhibit recovery, never be trusted.
            self._log("[DRY-SUP] persisted session invalid: starting inactive")
            self._persist_error = True

    @staticmethod
    def _valid_record(r) -> bool:
        if not isinstance(r, dict):
            return False
        if r.get("status") not in _STATUS_VALUES:
            return False
        sid = r.get("session_id")
        if not isinstance(sid, str) or not sid:
            return False
        t = r.get("target_c")
        if isinstance(t, bool) or not isinstance(t, int) \
                or not DRY_TEMP_MIN <= t <= DRY_TEMP_MAX:
            return False
        if not _is_finite_number(r.get("deadline")):
            return False
        if not isinstance(r.get("pending_stop"), bool) \
                or not isinstance(r.get("recovering"), bool):
            return False
        return True

    def _save(self) -> bool:
        """Atomic write: temp file in the target directory, then replace."""
        if self._state is None:
            return True
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".drying_session.", dir=d)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            tmp = None
            self._persist_error = False
            return True
        except OSError as e:
            self._log(f"[DRY-SUP] persistence FAILED: {type(e).__name__}: {e}")
            self._persist_error = True
            return False
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ── public API ───────────────────────────────────────────────────────────
    def arm(self, temp_c, hours) -> bool:
        """Persist an explicit supervised session before returning.

        Rejects invalid parameters and any second start while a session is
        running (the active deadline is never moved). Fails closed if the
        record cannot be persisted."""
        if isinstance(temp_c, bool) or not isinstance(temp_c, int) \
                or not DRY_TEMP_MIN <= temp_c <= DRY_TEMP_MAX:
            self._log(f"[DRY-SUP] arm rejected: bad temperature {temp_c!r}")
            return False
        if isinstance(hours, bool) or not isinstance(hours, int) \
                or not DRY_HOURS_MIN <= hours <= DRY_HOURS_MAX:
            self._log(f"[DRY-SUP] arm rejected: bad duration {hours!r}")
            return False
        if self._state is not None and self._state["status"] == "running":
            self._log("[DRY-SUP] arm rejected: a supervised session is already active")
            return False
        if self._state is not None and self._state["pending_stop"]:
            # A cancelled/expired session still owes a confirmed device Stop:
            # arming over it would silently drop that obligation.
            self._log("[DRY-SUP] arm rejected: a Stop from the previous "
                      "session is still owed")
            return False
        try:
            now = float(self._clock())
            mono = float(self._monotonic())
        except Exception:
            now = mono = float("nan")
        if not _is_finite_number(now) or not _is_finite_number(mono):
            # Without a usable clock there is no defensible finish time.
            self._log("[DRY-SUP] arm rejected: clock unusable")
            return False
        self._state = {
            "session_id": uuid.uuid4().hex,
            "target_c": temp_c,
            "deadline": now + hours * 3600.0,
            "status": "running",
            "pending_stop": False,
            "recovering": False,
        }
        self._deadline_mono = mono + hours * 3600.0
        self._reset_observation()
        if not self._save():
            self._state = None          # fail closed: unpersisted session never exists
            self._deadline_mono = None
            return False
        self._log(f"[DRY-SUP] armed {self._state['session_id']} at "
                  f"{self._state['target_c']}C until {self._state['deadline']:.0f}")
        return True

    def cancel(self) -> bool:
        """Persist cancellation + pending stop before returning. True if persisted.

        The in-memory cancellation lands regardless, so an in-flight recovery
        sees it immediately via the supervisor's cancellation callback."""
        if self._state is None or self._state["status"] != "running":
            return self._state is not None and self._state.get("pending_stop", False)
        self._state["status"] = "cancelled"
        self._state["pending_stop"] = True
        self._state["recovering"] = False
        self._deadline_mono = None
        self._reset_observation()
        ok = self._save()
        self._log("[DRY-SUP] session cancelled (stop pending)"
                  if ok else "[DRY-SUP] cancel NOT fully persisted; still cancelled in memory")
        return ok

    def status(self) -> dict:
        """Plain dict copy of the session for telemetry (never the live record)."""
        s = self._state
        return {
            "status": s["status"] if s else "inactive",
            "session_id": s["session_id"] if s else None,
            "target_c": s["target_c"] if s else None,
            "deadline": s["deadline"] if s else None,
            "pending_stop": bool(s and s["pending_stop"]),
            "recovering": bool(s and s["recovering"]),
            "inhibit": self._inhibit_reason(),
        }

    # ── periodic service ─────────────────────────────────────────────────────
    async def tick(self, transport, cancelled: Callable[[], bool]) -> None:
        """One serialized service pass: expiry first, obligations, then recovery.

        ``transport`` is duck-typed: ``drying_snapshot() -> None | (seq, native)``,
        ``await recover_drying(temp_c, deadline, cancelled)``,
        ``await stop_supervised()``. Called every second by the backend so the
        host deadline holds even while telemetry is down (expiry is persisted
        without any device contact). The recovery operation is handed to a
        tracked task instead of being awaited here: its configuration read-backs
        must never delay the next expiry or Stop by tens of seconds."""
        async with self._lock:
            self._collect_recovery()
            st = self._state
            if st is None:
                return
            if self._persist_error:
                self._save()          # retry: a recovered mount must re-enable ON
            drift, now, mono = self._clock_state()

            # 1. Host countdown owns the fixed finish time - expiry runs BEFORE
            #    any recovery, and persists offline (pending stop survives). It
            #    also runs while a recovery is in flight: expiry never waits for
            #    that operation's read-backs, and the recovery's own cancellation
            #    callback vetoes every ON the in-flight start would still write.
            if st["status"] == "running" and self._expired(now, mono):
                st["status"] = "completed"
                st["pending_stop"] = True
                st["recovering"] = False
                self._deadline_mono = None
                self._reset_observation()
                self._save()
                self._log("[DRY-SUP] deadline reached: Stop owed")
                st = self._state
            if st is None:
                return

            # 2. Terminal sessions owe at most a confirmed stop - never recovery.
            #    The attempt happens whenever a transport object exists; stale
            #    telemetry may withhold the CONFIRMATION but must not suppress
            #    the OFF itself.
            if st["status"] in ("cancelled", "completed"):
                if st["pending_stop"]:
                    if transport is None:
                        return          # unreachable: obligation stays persisted
                    res = await transport.stop_supervised()
                    st = self._state    # cancel()/recovery may have moved the record
                    if st is None or not st["pending_stop"]:
                        return
                    if res == "confirmed":
                        st["pending_stop"] = False
                        self._save()
                        self._log("[DRY-SUP] stop confirmed by fresh device state")
                    else:
                        self._log("[DRY-SUP] stop UNCONFIRMED - pending stop kept, "
                                  "no rearm, no retry-by-restart")
                return

            if self._recover_task is not None:
                return                  # exactly one attempt in flight, tracked

            # 3. Running: observe fresh state; never write from memory alone.
            snap = transport.drying_snapshot() if transport is not None else None
            if snap is None:
                return                  # offline/stale/no link: expiry handled above
            if transport is not self._observed_transport():
                # A replacement transport numbers its packets from scratch: the
                # old sequence gate would never open again and the old OFF count
                # says nothing about this link. Only fresh samples from the NEW
                # transport (and its own unresolved-start latch) decide from here.
                self._gate_seq = None
                self._set_observed_transport(transport)
                self._reset_observation()
            seq, native = snap
            if self._gate_seq is not None and seq > self._gate_seq:
                # Fresh state after the attempt resolves an uncertain outcome.
                self._gate_seq = None
                self._reset_observation()
            if native.get("drying_running") == "ON":
                self._reset_observation()
                return                  # observe only
            if seq != self._last_off_seq:
                self._last_off_seq = seq
                self._off_count += 1
            if self._off_count < self.OFF_SNAPSHOTS_REQUIRED:
                return
            reason = self._inhibit_reason(drift=drift, now=now, mono=mono)
            if reason:
                self._log(f"[DRY-SUP] recovery inhibited: {reason}")
                return
            if self._gate_seq is not None:
                return                  # still awaiting the attempt's own resolution
            if self._last_attempt_mono is not None and \
                    _is_finite_number(mono) and \
                    (mono - self._last_attempt_mono) < self.RECOVER_COOLDOWN_S:
                return
            if cancelled():
                return
            # Crash visibility: persist the recovering marker BEFORE any ON.
            st["recovering"] = True
            if not self._save():
                st["recovering"] = False
                return
            self._last_attempt_mono = mono
            self._gate_seq = seq        # only newer fresh state resolves this attempt
            # Deliberately NOT awaited here: the deadline, the owed Stop and the
            # cancellation callback stay live across the transport's read-backs.
            self._recover_task = asyncio.ensure_future(
                self._run_recovery(transport, st["target_c"], st["deadline"],
                                   self._internal_cancel(cancelled)))

    # ── the one recovery operation allowed in flight ─────────────────────────
    async def _run_recovery(self, transport, temp_c: int, deadline: float,
                            cancelled: Callable[[], bool]) -> None:
        try:
            res = await transport.recover_drying(temp_c, deadline, cancelled)
        except Exception as e:          # a raising transport must not vanish silently
            self._log(f"[DRY-SUP] recovery raised {type(e).__name__}: {e}")
            res = "failed"
        self._finish_recovery(res)

    def _finish_recovery(self, res) -> None:
        """Fold the operation's outcome into the session record.

        No awaits inside: one event-loop thread makes this atomic against the
        rest of ``tick``. A session cancelled or expired while the operation ran
        stays terminal - a late "confirmed" never revives it."""
        st = self._state
        if st is None or st["status"] != "running":
            if st is not None:
                self._save()
            self._log("[DRY-SUP] recovery outcome ignored: the session is already "
                      f"{st['status'] if st else 'gone'}")
            return
        st["recovering"] = False
        if res == "confirmed":
            self._gate_seq = None
            self._reset_observation()
            self._log("[DRY-SUP] recovery start confirmed by device readback")
        elif res == "uncertain":
            self._log("[DRY-SUP] recovery UNCONFIRMED - waiting for fresh "
                      "device state; never blind-retrying the ON")
        else:
            self._log("[DRY-SUP] recovery refused/failed by transport")
        self._save()

    def _collect_recovery(self) -> None:
        """Release the tracked task once it is done (and never leave an
        exception only on the task object)."""
        task = self._recover_task
        if task is not None and task.done():
            self._recover_task = None
            if not task.cancelled():
                task.exception()

    def _observed_transport(self):
        return self._obs_ref() if self._obs_ref is not None else self._obs_keepalive

    def _set_observed_transport(self, transport) -> None:
        try:
            self._obs_ref = weakref.ref(transport)
            self._obs_keepalive = None
        except TypeError:                # weakly-unreferenceable stand-in
            self._obs_ref = None
            self._obs_keepalive = transport

    # ── internals ────────────────────────────────────────────────────────────
    def _internal_cancel(self, caller_cancelled: Callable[[], bool]) -> Callable[[], bool]:
        """Cancellation seen by an in-flight recovery: caller cancellation, a
        supervisor-side cancel, lost persistence, an unusable or regressed clock
        or an elapsed deadline (wall clock OR monotonic anchor) all veto every
        ON - so expiry and cancellation win without anyone waiting for the
        operation's configuration read-backs."""
        def cb() -> bool:
            st = self._state
            if bool(caller_cancelled()):
                return True
            if st is None or st["status"] != "running" or self._persist_error:
                return True
            if not self._valid_record(st) or not _is_finite_number(st["deadline"]):
                return True
            if self._expired():
                return True
            try:
                mono = float(self._monotonic())
                if not _is_finite_number(mono) or \
                        (self._last_mono is not None and mono < self._last_mono):
                    return True
            except Exception:
                return True
            return False
        return cb

    def _clock_state(self):
        """``(regressed, wall_now, monotonic_now)`` for this pass.

        A clock that raises or reports a non-finite value is treated as
        regressed (and handed back as NaN) so recovery is inhibited; the
        anchored monotonic deadline still bounds the session when only the wall
        clock jumped."""
        try:
            mono = float(self._monotonic())
        except Exception:
            mono = float("nan")
        try:
            now = float(self._clock())
        except Exception:
            now = float("nan")
        regressed = not _is_finite_number(mono) or \
            (self._last_mono is not None and mono < self._last_mono)
        # The high-water mark only ever moves up: while the clock sits below
        # it, every pass keeps reporting the regression and stays inhibited.
        if _is_finite_number(mono) and \
                (self._last_mono is None or mono >= self._last_mono):
            self._last_mono = mono
        return regressed, now, mono

    def _expired(self, now: Optional[float] = None, mono: Optional[float] = None) -> bool:
        """True once EITHER finish time passed.

        The persisted wall deadline survives a restart and counts outages; the
        anchored monotonic deadline is what a wall-clock rollback can never push
        further out, so a running session can never be extended by clock trouble."""
        st = self._state
        if st is None or not _is_finite_number(st.get("deadline")):
            return False
        if now is None:
            try:
                now = float(self._clock())
            except Exception:
                now = float("nan")
        if _is_finite_number(now) and now >= st["deadline"]:
            return True
        if self._deadline_mono is not None:
            if mono is None:
                try:
                    mono = float(self._monotonic())
                except Exception:
                    mono = float("nan")
            if _is_finite_number(mono) and mono >= self._deadline_mono:
                return True
        return False

    def _inhibit_reason(self, drift: bool = False, now: Optional[float] = None,
                        mono: Optional[float] = None) -> Optional[str]:
        if self._persist_error:
            return "persistence unavailable"
        st = self._state
        if st is None:
            return "no session"
        if not self._valid_record(st):
            return "invalid session state"
        if drift:
            return "monotonic clock regression"
        if not _is_finite_number(now) or not _is_finite_number(mono):
            return "clock unusable"
        if self._expired(now, mono):
            return "deadline elapsed"
        return None

    def _reset_observation(self) -> None:
        self._off_count = 0
        self._last_off_seq = None
