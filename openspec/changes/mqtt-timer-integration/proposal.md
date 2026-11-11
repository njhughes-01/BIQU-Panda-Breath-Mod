# Finish existing MQTT / supervised timer integration

## Why
The approved native-device MQTT and supervised drying implementation is already deployed, but its source is unpublished, documentation still contains superseded instructions, and Docker's build context does not exclude runtime timer state. Finish that work without inventing a Wi-Fi or readback fix.

## Goal and done-when
Goal: preserve the existing approved controls and timer behavior, finish packaging/documentation, review the actual integrated source, and let the parent deploy and verify the reviewed artifact without losing settings or session ownership.

Done when the narrow changes below pass the final isolated suite and image checks, Claude Opus 5.5 accepts the complete relevant source/evidence, and the parent records a settings-preserving deployment and passive verification. Report GitHub/main integration separately: it is **not complete** while the full-evidence/protected-file merge gate remains unmet. Do not represent a deployed branch as merged main.

## Changes
- Exclude local preheat/session state (including atomic-write remnants) from Docker build context.
- Correct native MQTT documentation to distinguish ordinary reconnect from explicitly armed supervised recovery, and replace unsafe generic rollback advice for this deployment with the state-preserving procedure.
- Revalidate the final committed source and production image on healthy Spinx; reuse existing behavioral regressions, adding none merely to mirror internals.
- Review the full pending integration plus the exact production-to-candidate delta. Publish a truthful draft integration PR only in the parent's later GitHub phase; no merge unless all gates actually pass.

## Non-goals
No MQTT readback algorithm change, new controls, Wi-Fi/AP/radio/firmware work, HA migration, printer/heater commands, new drying cycle, credentials or provider configuration changes, or cleanup of other worktrees. The prior 50°C/eight-hour authorization was consumed by the completed run.

## Authority and real blocker
Nathan selected Astra PLAN → Sol IMPLEMENT → Claude Opus 5.5 REVIEW; failed reviews return to Astra, then Sol, then a fresh Opus review until resolved. This overrides older model/one-resweep instructions for this task. The planner commits only this change directory on the existing branch. Sol does tasks 1–3 only; the parent owns review dispatch, GitHub and deployment.

Main integration cannot honestly pass the installed merge gate today: the main-to-head diff alone is 716,522 bytes and touches two protected CI workflow files. See design.md. A real historical split and resolution of protected CI work requires a separate integration scope decision from the parent/Nathan; this packet does not authorize changing gate policy or checkouts. This does not prevent preparing the narrow finish or obtaining an honest Opus code review.
