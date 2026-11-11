# Work order
Goal and done-when: proposal.md. Exact decisions: design.md.
Skills: systematic-debugging, ship, test-driven-development, verification-before-completion.
Builder files: Panda.py only. Locked tests: tests/test_panda_cc2_integration.py (planner-owned).
Rules: preserve legacy mode and all current code; no device/router/network calls, no secrets, no deployment. No host pytest. Tests use Docker on ssh://proxmoxdocker, one serial lane (16 threads/22GiB checked).
Checks: DOCKER_HOST=ssh://proxmoxdocker bash run-tests.sh
