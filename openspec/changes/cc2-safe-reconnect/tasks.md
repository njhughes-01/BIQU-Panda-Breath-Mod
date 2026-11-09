- [x] 1. Extend `tests/test_panda_cc2_integration.py` existing reconnect regression, add timeout/lock controls, and prove the tests fail for binding/poll writes.
- [x] 2. In `Panda.py`, gate both printer bindings and the normal get_settings/timeout poll on not CC2_ACTIVE. Do not change any heater settings or safety ordering. Check: containerized pytest via run-tests.sh.
- [x] 3. Review the incremental diff, rerun final checks, preserve the original working tree, and stage deployment without automatic heater actions. Live reconnect/timer behavior must be verified separately.
Builder note: previous session Strata build attempts timed out; current scout completed. Use Strata for the bounded implementation; do not loop beyond two failed checks.

Implementation lane exception: pi-local returned literal tool-call text without edits; claude-local fallback timed out at90s without edits. Both failed the same three-guard task. Planner applied the bounded patch after those two failures; logs ../builder.log and ../builder-fallback.log.

Final: 153 tests passed on one serial Docker lane (debdocker16threads/22GiB). Read-only Codex re-review found no blockers. Incremental image panda-safe-reconnect:20261109 deployed under prior task approval; source hash verified. Device state received immediately after startup, V1.0.4, Dry idle21C. No new heater commands or active-cycle preservation test were run. AP retry/SNR investigation awaits controller access/location.
