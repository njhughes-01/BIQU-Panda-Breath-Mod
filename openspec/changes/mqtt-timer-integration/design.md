# Closed design and evidence boundaries

## Observed source / remote state (2026-11-11)
Only read-only local Git and GitHub queries were used; no device or deployment host was contacted. The checkout was clean, branch `fix/native-panda-mqtt`, at `ad7965acae8cd5018e51dcc263b793240e0a9a51`. Preserve its unpublished ancestry; no reset, rebase, checkout/worktree change or force push.

| Meaning | Exact revision / state |
| --- | --- |
| Actual main / eventual aggregate PR base | `3aa586c5114b8f49cbfeb5ab6c7268dbc22aef57` (`origin/main`, also returned by `git ls-remote`) |
| Existing remote CC2 integration branch | `863071992733b5e61f778617b90e0e61818430fa` |
| Merge base of current head and CC2 integration | `b344a00ca1017c2678180d7923ca9396cd269880`; 22 commits only on our head, 2 only on remote CC2 |
| Current implementation baseline | `ad7965acae8cd5018e51dcc263b793240e0a9a51` (plan commit will extend this) |
| Reported production source | `c5aa93241436396a0ff183351a299a72a5c38cf1`, image `panda-supervised:20261110-reviewed` |
| Historically healthy full-suite revision | `e84b0540c7145a645d2b9263708eada4fe3b1d33` |
| Earlier deployed safety baseline in this ancestry | `37e4c6398b3feefd4993747d09cd6c6c450b835e` (not a remote PR base) |

Production → initial head is documentation/ignore/OpenSpec only. Healthy-suite revision → production adds only Dockerfile preparation of writable `/app/dry_state`; neither observation is a fresh test of the candidate. Historical verification records 260 passing tests, 23.44 s, one Spinx container, 2 CPUs/2 GiB, network none. Earlier proxmoxdocker results remain untrusted.

GitHub has PR #1 (`cc2-docker-integration` → main), #2 (`fix/petg-no-chamber-heat` → CC2 integration), and #4 (`fix/cc2-safe-reconnect` → PETG) open. PR #3 (`fix/preheat-startup` → CC2 integration) merged November 8. No native-MQTT branch or PR exists remotely. PR #1's successful build/publish check does not test this branch; #2/#4 expose no test checks. Main protection API returns 404/“Branch not protected”; repository rulesets are empty. Local rules and Jev still apply.

The two remote-only CC2 commits are #3's fix plus its merge. Its logging-before-preheat-load correction is independently present here at `8e5efa9`; its CHANGELOG entry is absent. Do not silently merge/cherry-pick the remote branch to resolve bookkeeping. Leave the dirty `panda-cc2-safe-reconnect` and `panda-preheat-startup` worktrees and their PRs untouched.

## Smallest supported finish
1. `Dockerfile` and `Dockerfile.test` use `COPY . .`. A local ignored `preheat_state.json` exists, but `.dockerignore` excludes neither it nor drying state. A working-directory build can package runtime intent. Add root exclusions for `preheat_state.json`, `drying_session.json`, `dry_state/`, and `.drying_session.*`. Preserve the local files. Verify with synthetic state in a disposable source export, never actual state.
2. `docs/native-mqtt.md` says boot/reconnect cannot restart drying and suggests dropping the overlay as rollback. Those statements predate the supervisor. Ordinary reconnect remains observational, while persisted, explicitly armed sessions may recover under the existing rules. A rollback must preserve timer ownership and pending Stop; restoring an old state snapshot can resurrect intent. Correct this and the superseded deployment wording in `docs/supervised-drying.md`.
3. No runtime correction is established by this planning pass. `_wait_field` deliberately rejects the first contradictory post-write packet, and `test_start_drying_config_readback_must_match` exercises refusal. This proves current behavior, not that delayed readback caused the live trouble. Do not relax confirmation, lengthen waits, or add retries from that lead. A real failing review must supply a reproducer and return to Astra for a separately bounded correction.

## Behavior contract
The dedicated authenticated device broker stays separate from HA. Only the pinned device's fresh nonretained telemetry can confirm operations. Ordinary writes require freshness; uncertain ON is never blindly replayed. Fixed UTC deadlines, monotonic runtime enforcement, durable cancel/expiry and pending Stop remain. Recovery is bounded to explicitly armed sessions, including physical Panda OFF as authorized; mod Stop/power OFF/mode changes/locks cancel. Queued work cannot rearm after Stop. Preserve timer-running versus heating-proof versus unconfirmed-OFF distinctions, legacy behavior, CC2 and PETG no-chamber-heat policy. Specs and existing tests below are the acceptance contract, not a request for more features.

## Full-review / merge evidence strategy
Use TWO clearly labeled views: full main-to-candidate integration, and production-to-candidate deployment delta. Only the first is the aggregate PR's merge diff. Opus must inspect all changed paths and the safety interactions, reading file-sized batches if necessary; an index or a summary does not replace the actual diff. Keep complete raw diff/evidence on disk outside Git. Review all earlier material findings and their dispositions, including corrected cancellation/state-replacement/import-order issues.

At initial head, binary Git diffs are 716,522 bytes against main, 470,582 bytes against the divergent remote CC2 tip (two-dot comparison), and 40,643 bytes against production. The gate's `jev-gate-log diff` is authoritative for a PR; for divergent bases record its actual merge-base diff, not the two-dot comparison. Production is a deployment comparison, never a substitute PR base.

Installed `~/.claude/skills/ship/jev-gate-log` and current ship instructions specify a **256 KiB JSON envelope**, including wrapper/question reserve, rather than the historical 64 KiB. Tool-side admission/limit still needs verification in the actual review route; do not modify it. Even the full raw main diff exceeds either cap. Provider token limits also apply; byte estimates cannot prove acceptance. Do not submit partial evidence, enable long context, rescore for a better number, or raise caps here.

The deterministic protected-file check against main currently returns **1.0** for `.github/workflows/docker-publish.yml` and `.github/workflows/update-hidden-traffic.yml`; the allowed maximum is 0.25. This is an independent gate failure, not waived by GitHub's lack of branch protection. No automatic-approval rejection occurred; these are observed gate constraints.

A genuine split would require separately tested/reviewed historical dependencies, real PR bases and complete evidence for EVERY layer before main is declared integrated, plus resolution of protected CI changes. Publishing a branch at production or the safety baseline and treating its ancestry as accepted would evade that work. Such a split would broaden the selected finish task and violate the current no-checkout-change constraint if executed here. Return that scope decision to the parent; do not manufacture a stack. Until resolved, a full-scope draft PR may preserve source/provenance, but no merge is allowed. Parent may deploy the independently reviewed branch under Nathan's existing deployment authorization; that is not a Jev merge pass.

## Review route and private evidence
Parent must discover the requested Opus 5.5 provider and verify actual TypeSafe enablement AND admission of `typesafe_evaluate` before dispatch. For a supported headless Pi route these require `PI_TYPESAFE_ENABLED=1` and tool allowlisting; a task prompt or model option is not proof. No silent provider/CLI substitution, secret access or unrelated settings edits. The planner has not verified that launch route. If unavailable, report the precise unmet capability before launching. Enabled tooling does not make oversized evidence scoreable; a textual Opus verdict and a Jev gate are separate results.

Evidence root: `~/docs/3d-printing/panda-session-evidence-20261111/`, specifically `panda-supervised-drying-20261110/spinx/verification.json`, its full `final-suite.log`/race/build logs, and `correction/REPORT.md` plus the referenced test logs; the sibling `panda-native-mqtt-20261109/` contains prior test failures/retries. Read only explicitly identified nonsecret test/review artifacts. Do not ingest private responses, credentials, env, packet/AP logs or whole archives. Record missing historical command/exit/revision metadata as unavailable; never reconstruct it as observed fact. Privacy-screen locally before external submission. If complete relevant evidence cannot be safely supplied, leave the gate unmet.
