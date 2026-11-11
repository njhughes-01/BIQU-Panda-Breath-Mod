# Finish existing MQTT / supervised timer integration

## Why
The approved native-device MQTT and supervised drying implementation is already deployed, but its source is unpublished, documentation still contains superseded instructions, and Docker's build context does not exclude runtime timer state. Finish that work without inventing a Wi-Fi or readback fix.

## Goal and done-when
Goal: preserve approved timer ownership, correct the demonstrated reporting and native temperature-bound defects and the generic Dry-start bypass, finish packaging/documentation, and let the parent review and verify the rebuilt artifact without losing settings or session ownership.

Done when the narrow changes below pass the final isolated suite and image checks, Claude Opus 5.5 accepts the complete relevant source/evidence, and the parent records a settings-preserving deployment and passive verification. Report GitHub/main integration separately: it is **not complete** while the full-evidence/protected-file merge gate remains unmet. Do not represent a deployed branch as merged main.

The final F1 amendment in [correction-round.md](correction-round.md) is authoritative. Tasks 7–14 passed at c63ea556; preserve their history/evidence. Sol now completes task 15's locked Stop/Heat reporting correction, then repeats task 14 at a new clean head/image. Planner changes only this packet. Deployment requires corrected reporting, rebuilt-artifact checks, fresh deployment-focused review and passive idle OFF, followed by serial backend and separate web image updates preserving each service and current state. Nathan withdrew hypothetical PETG arbitration; existing deployment authority remains, with no new cycle or permission interview.

## Changes
- Exclude local preheat/session state (including atomic-write remnants) from Docker build context.
- Correct native MQTT documentation to distinguish ordinary reconnect from explicitly armed supervised recovery, and replace unsafe generic rollback advice for this deployment with the state-preserving procedure.
- Revalidate the final committed source and production image on healthy Spinx; reuse existing behavioral regressions, adding none merely to mirror internals.
- Review the complete bounded deployment-relevant correction packet; retain full main and production diffs without claiming inherited-main coverage. Publish a truthful draft integration PR only in the parent's later GitHub phase; no merge unless all gates actually pass.
- Invalidate obsolete temperature-rise evidence and publish a consistent native Heat/proof status, enforce 40–60°C on direct native dry-temperature writes, keep generic `work_on ON` from bypassing supervised Dry, correct the clock diagnostic and false physical-relay wording, and include `Panda.py` in rebuilt-image hash evidence.

## Non-goals
No uncertainty-latch/readback relaxation, new controls, Wi-Fi/AP/radio/firmware work, HA migration, live printer/heater commands, new drying cycle, credentials or provider configuration changes, or cleanup of other worktrees. No PETG print/session arbitration, interlock or overlap test: preserve ordinary PETG printing and explicitly armed Dry contracts, without a general policy waiver or a pending product decision. The prior 50°C/eight-hour authorization was consumed by the completed run.

## Authority and real blocker
Nathan selected Astra PLAN → Sol IMPLEMENT → Claude Opus 5.5 REVIEW → parent deployment; material failures return through that loop. This overrides older model/one-resweep instructions. Planner commits only this change directory on the existing branch. Sol now does 15 → 14 only; parent owns review dispatch, GitHub and deployment. Main raw diff at c63 is 836,883 bytes, protected CI and inherited coverage remain unmet; none is a substitute deployment gate or permission request.

Main integration cannot honestly pass the installed merge gate: at the reviewed `ed159dd` head the raw main diff is 757,621 bytes, above the installed 256 KiB envelope cap, and touches two protected CI workflow files. Inherited-file review coverage was also incomplete. No score or partial input was submitted. A real historical split and resolution of protected CI work requires a separate integration scope decision; this packet does not authorize it or change gate policy/checkouts. This does not prevent the bounded corrections or an honest fresh Opus code review.
