# Closed work order: MQTT / timer finish

## Assignment
Sol implements tasks 1–3 in this checkout; do the work yourself, no subagents. Nathan expressly selected Sol instead of Strata. Astra replans demonstrated failures; parent dispatches fresh Claude Opus 5.5 reviews and handles GitHub/deployment. Older work-order model/approval language is superseded by this task's explicit authorization.

## Read first
`proposal.md`, `design.md`, `specs/integration/spec.md`, `tasks.md` here; `docs/panda-findings-20261111.md`, `docs/native-mqtt.md`, `docs/supervised-drying.md`; applicable global rules. Use executing-plans and verification-before-completion, honoring this exact scope and test route.

## Workspace / files
Existing `/home/killerherts/src/BIQU-Panda-Breath-Mod`, branch `fix/native-panda-mqtt`, starting with the planner commit descended from `ad7965acae8cd5018e51dcc263b793240e0a9a51`. Do not create/change checkouts or worktrees; do not reset to main. Leave both older dirty worktrees untouched.

Allowed implementation files, in task order:
- Task 1: `.dockerignore`.
- Task 2: `docs/native-mqtt.md`, `docs/supervised-drying.md`.
- Task 3: no application files; scratch-only test/build evidence.
- Task completion bookkeeping: this change's `tasks.md` only; commit task 1–2 marks before selecting final tested head; never mark task 3 passed before running it, and explicitly revalidate a later head.

No application-code change is preauthorized by this packet. A material new finding returns to Astra with file/location, concrete scenario/reproducer and complete failed check; do not widen scope. Readback lag is not an established defect.

## Checks
`git diff --check`
`openspec validate mqtt-timer-integration --strict --no-interactive`

Task 3 specifies exact serial Compose route, healthy Spinx resource check, network-none fake-device suite, synthetic build-context image inspection and non-root volume persistence. No bare pytest. No `TEST_FILE` shortcut. Disk scratch outside repo and `/tmp`; no real settings/state/secrets enter artifacts or logs. No device or live service calls, push, PR, merge, deployment, provider changes or review dispatch by Sol.

## Handoff
Stop when 1–3 pass. Return exact commits/head, touched files, complete private evidence locations, all commands/outcomes/skips/limitations, image identity and unresolved findings. Do not claim Opus/Jev/deployment/main integration passed. Parent must resolve actual review-route admission and the oversized/protected historical integration gate separately; neither is a reason to fake a smaller base or abandon the authorized narrow implementation.
