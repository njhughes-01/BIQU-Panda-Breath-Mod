# Work order

## Skills
/home/killerherts/.codex/skills/executing-plans
/home/killerherts/.codex/skills/test-driven-development
/home/killerherts/.codex/skills/verification-before-completion

## Tools
read,edit,write,bash,grep,find,ls

## Files
- panda_native_mqtt.py
- tests/test_panda_native_mqtt.py
- Panda.py
- tests/test_panda_cc2_integration.py
- docker-compose.panda-native.yml
- deploy/panda-native-mosquitto.conf
- docs/native-mqtt.md
- openspec/changes/native-panda-mqtt/tasks.md

## Rules
Goal and done-when: proposal.md and design.md; interfaces: tasks.md. No real-device/HA/broker/router calls, secrets, runtime state, deployments, live services, pushes, PRs or extra builders. Dedicated device broker stays separate from HA. Preserve legacy behavior and safety baseline. Stop after two failures of the same check, with evidence.

## Checks
env DOCKER_HOST=ssh://proxmoxdocker docker-compose -f docker-compose.test.yml config --quiet
