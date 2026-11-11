# Supervised drying recovery — proposed design

## Goal and user decision
Keep a deliberately started drying session running through Panda network failures or unexpected device stops. Nathan explicitly selected automatic restart even when Panda's physical OFF button may have caused the stop; use the mod Stop control to cancel. This replaces the prior no-automatic-resume policy only for an explicitly armed supervised drying session.

## Approaches
1. Recommended: supervise native timed drying in the existing Docker backend. Keep device temperature regulation and native timer; persist the session deadline and recover only after fresh device confirmation.
2. Forced-on mode plus an external timer would lose the device drying timer backup when the network fails; do not use it.
3. A script inside stock Panda firmware is not an available deployment mechanism. Replacing firmware is outside scope.

## Proposed behavior
- Start from the existing mod drying controls using their selected temperature and duration. Never adopt an idle device or arm a session merely because a service starts.
- Persist the session identity, fixed UTC finish time, target, cancellation and any uncertain command outcome atomically outside source-controlled settings. Do not reset the deadline on reconnect or backend restart. Outages count toward elapsed time.
- Keep a backend monotonic countdown while running. Invalid clocks or invalid persisted data inhibit recovery. On restart, reconcile persisted intent with fresh device state before any write.
- While fresh device status confirms drying running, observe only; do not repeatedly send ON or rewrite its timer.
- If fresh device status repeatedly confirms drying OFF before the deadline, restart that session at its target and remaining duration. Use a separate session-authorized recovery operation: ordinary start rejects positive remaining time even when running is OFF, and that normal active-cycle guard must remain. Fresh status must identify the correct device. Reported faults inhibit recovery when available, but the current native fields cannot establish absence of a thermal fault. Do not clear safety locks or change thermal protection.
- Recovery shares the existing serialized command/cancellation path. Rate-limit recovery attempts to at most one per minute. Never blindly repeat an ambiguous start; wait for newer live device status to resolve it first.
- Because native custom_timer accepts only whole hours, recovery must round remaining duration UP to the next hour. The mod still requests STOP at its original deadline. If disconnected then, physical stopping at that exact deadline cannot be guaranteed; recompute the remaining hours near the final ON write and abort if expired. Native expiry is a backup of approximately one hour beyond the deadline plus command latency, dependent on firmware behavior. Do not claim an unconfirmed stop succeeded.
- Mod Stop and existing lock/power/mode cancellation controls first persist cancellation and invalidate pending recovery, then request device stop. Stop always wins over an in-flight restart. Panda's physical OFF alone does not cancel supervised intent, as Nathan authorized.
- At the fixed deadline, persist completion and a separate pending-stop obligation before requesting stop. Persist pending stop for cancellation too; backend restart must still reconcile and deliver that stop after connectivity returns without rearming recovery. Never resume an expired/cancelled session on reconnect. Continue to report an unconfirmed stop until fresh device evidence establishes stopped state; never restart to resolve it.
- Preserve settings, the separate device broker, existing CC2 behavior and PETG print rules. Expose session deadline/recovering/cancelled/unconfirmed status through existing mod telemetry.

## Verification / done when
Use focused simulated device tests for reconnect while already running (no writes), confirmed OFF recovery with a fixed deadline, ambiguous start, cancellation racing recovery, restart persistence and expiry during outage. Run existing meaningful native/CC2 regressions on the final tree; no general test expansion. Obtain independent review before live deployment. No live heater commands or deployment during design/preparation.

## Approval needed
Nathan confirmed host-owned countdown and Stop at expiry; auto-restart and mod-only cancellation explicitly authorized. Implementation preparation is authorized; live deployment remains a separate approval. This changes the earlier observational reconnect rule only for armed supervised sessions.
