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
  attempt resolves it.
* Persistence failure, an invalid persisted state, a monotonic clock
  regression, elapsed deadline or the cancellation callback prohibit ON.
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
from typing import Callable, Optional

# Device contract bounds (mirrors panda_native_mqtt; kept here so the
# supervisor validates persisted state without importing the transport).
DRY_TEMP_MIN, DRY_TEMP_MAX = 40, 60
DRY_HOURS_MIN, DRY_HOURS_MAX = 1, 99

_STATUS_VALUES = ("running", "cancelled", "completed")


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
        self._log = logger or (lambda _m: None)
        self._lock = asyncio.Lock()
        self._state: Optional[dict] = None   # the one persisted session record
        self._persist_error = False
        # runtime-only recovery bookkeeping (fresh per process by design)
        self._off_count = 0
        self._last_off_seq: Optional[int] = None
        self._gate_seq: Optional[int] = None   # fresh seq > this resolves an attempt
        self._last_attempt_mono: Optional[float] = None
        self._last_mono: Optional[float] = None
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
        self._state = {
            "session_id": uuid.uuid4().hex,
            "target_c": temp_c,
            "deadline": float(self._clock()) + hours * 3600.0,
            "status": "running",
            "pending_stop": False,
            "recovering": False,
        }
        self._reset_observation()
        if not self._save():
            self._state = None          # fail closed: unpersisted session never exists
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
        without any device contact)."""
        async with self._lock:
            st = self._state
            now = self._clock()
            drift = self._clock_regression()
            if st is None:
                return
            if self._persist_error:
                self._save()          # retry: a recovered mount must re-enable ON

            # 1. Host countdown owns the fixed finish time — expiry runs BEFORE
            #    any recovery, and persists offline (pending stop survives).
            if st["status"] == "running" and now >= st["deadline"]:
                st["status"] = "completed"
                st["pending_stop"] = True
                st["recovering"] = False
                self._reset_observation()
                self._save()
                self._log("[DRY-SUP] deadline reached: Stop owed")
                st = self._state
            if st is None:
                return

            # 2. Terminal sessions owe at most a confirmed stop — never recovery.
            if st["status"] in ("cancelled", "completed"):
                if st["pending_stop"]:
                    if transport.drying_snapshot() is None:
                        return          # unreachable: obligation stays persisted
                    res = await transport.stop_supervised()
                    if res == "confirmed":
                        st["pending_stop"] = False
                        self._save()
                        self._log("[DRY-SUP] stop confirmed by fresh device state")
                    else:
                        self._log("[DRY-SUP] stop UNCONFIRMED — pending stop kept, "
                                  "no rearm, no retry-by-restart")
                return

            # 3. Running: observe fresh state; never write from memory alone.
            snap = transport.drying_snapshot()
            if snap is None:
                return                  # offline/stale: expiry handled above
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
            reason = self._inhibit_reason(drift=drift, now=now)
            if reason:
                self._log(f"[DRY-SUP] recovery inhibited: {reason}")
                return
            if self._gate_seq is not None:
                return                  # still awaiting the attempt's own resolution
            if self._last_attempt_mono is not None and \
                    (self._monotonic() - self._last_attempt_mono) < self.RECOVER_COOLDOWN_S:
                return
            if cancelled():
                return
            # Crash visibility: persist the recovering marker BEFORE any ON.
            st["recovering"] = True
            if not self._save():
                st["recovering"] = False
                return
            self._last_attempt_mono = self._monotonic()
            self._gate_seq = seq        # only newer fresh state resolves this attempt
            res = await transport.recover_drying(st["target_c"], st["deadline"],
                                                 self._internal_cancel(cancelled))
            st = self._state            # a cancel() may have replaced the record
            if st is None or st["status"] != "running":
                st and self._save()
                return
            st["recovering"] = False
            if res == "confirmed":
                self._gate_seq = None
                self._reset_observation()
                self._log("[DRY-SUP] recovery start confirmed by device readback")
            elif res == "uncertain":
                self._log("[DRY-SUP] recovery UNCONFIRMED — waiting for fresh "
                          "device state; never blind-retrying the ON")
            else:
                self._log("[DRY-SUP] recovery refused/failed by transport")
            self._save()

    # ── internals ────────────────────────────────────────────────────────────
    def _internal_cancel(self, caller_cancelled: Callable[[], bool]) -> Callable[[], bool]:
        """Cancellation seen by an in-flight recovery: caller cancellation, a
        supervisor-side cancel, lost persistence, clock regression or an
        elapsed deadline all veto every ON."""
        def cb() -> bool:
            st = self._state
            if bool(caller_cancelled()):
                return True
            if st is None or st["status"] != "running" or self._persist_error:
                return True
            if not self._valid_record(st) or not _is_finite_number(st["deadline"]) \
                    or self._clock() >= st["deadline"]:
                return True
            try:
                if self._last_mono is not None and self._monotonic() < self._last_mono:
                    return True
            except Exception:
                return True
            return False
        return cb

    def _clock_regression(self) -> bool:
        m = self._monotonic()
        regressed = self._last_mono is not None and m < self._last_mono
        if not regressed:
            self._last_mono = m
        return regressed

    def _inhibit_reason(self, drift: bool = False, now: Optional[float] = None) -> Optional[str]:
        if self._persist_error:
            return "persistence unavailable"
        st = self._state
        if st is None:
            return "no session"
        if not self._valid_record(st):
            return "invalid session state"
        if drift:
            return "monotonic clock regression"
        if now is None:
            now = self._clock()
        if now >= st["deadline"]:
            return "deadline elapsed"
        return None

    def _reset_observation(self) -> None:
        self._off_count = 0
        self._last_off_seq = None
