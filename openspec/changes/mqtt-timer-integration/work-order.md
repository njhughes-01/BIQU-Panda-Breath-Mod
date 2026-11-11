# Closed work order: MQTT / timer correction round

## Assignment and done-when
Sol implements task 15, then repeats task 14 on the new final head, serially in this checkout, without subagents. Tasks 7–14 already passed at c63ea556 and remain baseline. Nathan selected Astra PLAN → Sol IMPLEMENT → fresh Claude Opus 5.5 REVIEW → parent deployment; older Strata/default/one-resweep restrictions do not override that choice. Parent owns review through the approved Pi Claude CLI with per-invocation `PI_TYPESAFE_ENABLED=1` and admitted `typesafe_evaluate`. No provider edits/substitution.

Done when bounded corrections are committed, focused regressions and the final suite pass with complete evidence, and the rebuilt production image's runtime hashes match the final tested source. Return to parent and stop. Opus/deployment/main integration are separate, unmet until actually completed. No unnecessary interview for routine corrections.

## Read first
Read this packet and the complete second review `~/docs/3d-printing/panda-opus-correction-review-20261111/review.log`, exact resume `~/docs/3d-printing/panda-mqtt-timer-correction-resume-20261111/HANDOFF.md`/`verification.json` and relevant source/callers before task 15. Retain original/resume COMMANDS, full results/manifests and limitations unchanged; index complete final/failing per-run evidence for parent review. Use executing-plans and verification-before-completion within this work order. Do not repeat tasks 1–13, baseline suites or treat c63's image as F1 acceptance.

## Workspace and permitted edits
Existing `/home/killerherts/src/BIQU-Panda-Breath-Mod`, branch `fix/native-panda-mqtt`; this plan extends clean `c63ea556e8919483787bc849b0865dad970191de`. Begin at the handed-off plan commit; never reset to c63 or earlier WIP. Preserve all ancestry, older worktrees and runtime files. No checkout/worktree creation/change, reset, rebase or history rewriting.

| Task | Files |
| --- | --- |
| 15: one locked Stop/Heat flow regression and publication-only fix | `Panda.py`, `tests/test_panda_cc2_integration.py` |
| 14: final verification | no repository files; private scratch evidence/scripts |

Completion marks in `tasks.md` are permitted before the final head is selected, never claims in advance. Keep each task to 1–3 files; separate bookkeeping if a task already owns three. Final results go outside Git so a results-only commit does not invalidate exact-head evidence.

## Non-negotiable preservation
Pinned fresh nonretained telemetry, per-field readback, unresolved-start latch, two distinct OFF observations, recovery cooldown, validated sole session ownership, fixed wall/monotonic deadlines, durable cancellation/pending Stop and cancellation-generation ordering remain. No readback-lag speculation, firmware/network changes, historical CI refactor, or new recovery policy. Work_on ON is still not a cancellation; it simply cannot be a generic alternate Dry-start path.

Do not change `_resolve_latch` in this round. Preserve existing panel OFF recovery; no new panel intent inference. Nathan rejected hypothetical PETG/Dry arbitration and parent withdrew its added deployment blocker. No actual overlap was observed. Preserve ordinary PETG printing and explicitly armed Dry contracts without a new interlock/test, pending product decision, general policy waiver or new heat-cycle authority. Task 14 is explicitly restored and authorized.

## Verification and stop conditions
Use the exact serial Docker Compose route in tasks.md on healthy Spinx, one 2-CPU/2-GiB network-none lane and new `panda-mqtt-timer-f1-20261111` labels/paths. No bare pytest, `TEST_FILE`, suspect host or old runtime image. Capture dirty-source hashes and full RED/whole-file GREEN results; then pure final-commit Git archive, full suite once, new production image, all four module hashes and synthetic persistence. Preserve every historical/current failure, skip, retry and exit status unchanged. Two unintended failures of the same post-fix check return to Astra; expected RED is separate. Task 7's old fixture failures are resolved baseline, not a reason to repeat its work. No full-suite debug loop or policy change.

Checks: focused files listed by each task, `git diff --check`, `openspec validate mqtt-timer-integration --strict --no-interactive`, full suite once at final commit, new production image/state isolation/non-root synthetic persistence inspections. No real state/settings/secrets in build exports or logs. No device, network-device, HA, live deployment-host calls, printer/bed/Dry commands, deployment/push/PR/merge/review dispatch. Local image build dependency downloads use the existing approved route.

## Handoff
Return exact commits/final head, F1 disposition, full private evidence paths, all command outcomes/skips/limitations, rebuilt image ID/revision/archive and four hashes, baseline caveats and deferred latch. PETG arbitration stays withdrawn. Parent owns task 4's complete deployment-relevant Opus review and task 6's serial backend/web deployment under existing authority; no fresh approval interview. c63's 836,883-byte raw main diff exceeds 256 KiB; protected workflows/inherited coverage remain unmet. Retain full actual-main/production diffs, no partial Jev input, substituted base, score or merge. Main integration is not an additional blocker to authorized deployment.
