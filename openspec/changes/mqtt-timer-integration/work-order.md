# Closed work order: MQTT / timer correction round

## Assignment and done-when
Sol implements tasks 7–14 serially, in this checkout, without subagents. Nathan explicitly selected Astra PLAN → Sol IMPLEMENT → fresh Claude Opus 5.5 REVIEW; older Strata/default/one-resweep restrictions do not override that choice. Parent dispatches the independent review through the approved existing Pi Claude bridge CLI with per-invocation `PI_TYPESAFE_ENABLED=1` and admitted `typesafe_evaluate`. No provider configuration edits or native Claude substitution.

Done when bounded corrections are committed, focused regressions and the final suite pass with complete evidence, and the rebuilt production image's runtime hashes match the final tested source. Return to parent and stop. Opus/deployment/main integration are separate, unmet until actually completed. No unnecessary interview for routine corrections.

## Read first
`proposal.md`, `design.md`, `correction-round.md`, `specs/integration/spec.md`, `tasks.md` here; the full review/admission/passive-finding artifacts and exact-head baseline evidence listed in `correction-round.md`. Use executing-plans and verification-before-completion within this work order. Task 3's old packaging verification passed at ed159dd externally; do not repeat tasks 1–2 or treat the old image as new-runtime evidence.

## Workspace and permitted edits
Existing `/home/killerherts/src/BIQU-Panda-Breath-Mod`, branch `fix/native-panda-mqtt`; correction plan extends `ed159ddfd6c07866540a6b03b9826f658b1b6b91`. Begin at the handed-off plan commit; do not reset to this baseline. No checkout/worktree creation/change, reset, rebase or history rewriting. Leave older dirty worktrees and all real runtime files untouched.

| Task | Files |
| --- | --- |
| 7: evidence lifecycle | `panda_native_mqtt.py`, `tests/test_panda_native_mqtt.py` |
| 8: native status publishing | `Panda.py`, `tests/test_panda_cc2_integration.py` |
| 9: transport dry-temperature bounds | `panda_native_mqtt.py`, `tests/test_panda_native_mqtt.py` |
| 10: mod bounds / native discovery | `Panda.py`, `tests/test_panda_cc2_integration.py` |
| 11: generic ON bypass | `Panda.py`, `tests/test_panda_cc2_integration.py` |
| 12: clock diagnostic | `drying_supervisor.py`, `tests/test_drying_supervisor.py` |
| 13: truthful implicated descriptions | `panda_web.py`, `docs/native-mqtt.md`, `docs/supervised-drying.md` |
| 14: final verification | no repository files; private scratch evidence/scripts |

Completion marks in `tasks.md` are permitted before the final head is selected, never claims in advance. Keep each task to 1–3 files; separate bookkeeping if a task already owns three. Final results go outside Git so a results-only commit does not invalidate exact-head evidence.

## Non-negotiable preservation
Pinned fresh nonretained telemetry, per-field readback, unresolved-start latch, two distinct OFF observations, recovery cooldown, validated sole session ownership, fixed wall/monotonic deadlines, durable cancellation/pending Stop and cancellation-generation ordering remain. No readback-lag speculation, firmware/network changes, historical CI refactor, or new recovery policy. Work_on ON is still not a cancellation; it simply cannot be a generic alternate Dry-start path.

Do not change `_resolve_latch` in this round. Preserve existing panel OFF recovery; no new panel intent inference. Do not implement PETG arbitration until parent resolves cancel-versus-inhibit semantics and Astra supplies its bounded follow-up. The global NEVER-heat-during-PETG-print rule stands; existing tests do not prove the combined case. This pending decision does not block implementing tasks 7–14, but deployment remains held.

## Verification and stop conditions
Use the exact serial Docker Compose route in tasks.md on healthy Spinx, one 2-CPU/2-GiB network-none test lane. No bare pytest, `TEST_FILE`, remote/suspect test host or old runtime image. Capture dirty-tree hashes for focused runs; final source comes from the exact clean commit via Git archive. Build a new production image from that source; compare Panda/transport/supervisor/web hashes. Preserve every failure, skip, retry and exit status. Same failing check at most two tries, then return to Astra; do not keep looping or broaden the task.

Checks: focused files listed by each task, `git diff --check`, `openspec validate mqtt-timer-integration --strict --no-interactive`, full suite once at final commit, new production image/state isolation/non-root synthetic persistence inspections. No real state/settings/secrets in build exports or logs. No device, network-device, HA, live deployment-host calls, printer/bed/Dry commands, deployment/push/PR/merge/review dispatch. Local image build dependency downloads use the existing approved route.

## Handoff
Return exact commits/final head, changes per finding, full private evidence paths, all command outcomes/skips/limitations, rebuilt image ID/revision/archive and runtime hashes, baseline/historical caveats, deferred latch limitation, unresolved PETG choice and main gate unmet. Parent owns fresh Opus CLI review and any later deployment under preserved authority. The 757,621-byte prior main diff already exceeds installed 256 KiB evidence cap; two protected workflows and incomplete inherited coverage remain. No partial input, substituted base or invented Jev score. Do not broaden this round to unblock historical main integration.
