# Work order
Goal and done-when: proposal.md and design.md. Exact task interfaces/decisions: tasks.md.
Skills: executing-plans, test-driven-development, verification-before-completion.
Files: panda_native_mqtt.py, tests/test_panda_native_mqtt.py, Panda.py, tests/test_panda_cc2_integration.py, docker-compose.panda-native.yml, deploy/panda-native-mosquitto.conf, docs/native-mqtt.md, openspec/changes/native-panda-mqtt/tasks.md.
Rules: no real-device/HA/broker/router calls; no secrets or runtime state; no live deployments/services; no pushes/PRs; no extra builders. Dedicated device broker is separate from HA. Preserve legacy path and existing safety baseline. Task failures: isolate failing file and stop after two same-check failures with evidence.
Checks: Docker Compose pytest targeted task files on ssh://proxmoxdocker; one serial lane; final full gate planner-only.
