# Work order
## Skills
ship, systematic-debugging, test-driven-development, verification-before-completion
## Files
drying_supervisor.py
panda_native_mqtt.py
Panda.py
Dockerfile
tests/test_drying_supervisor.py
tests/test_panda_native_mqtt.py
tests/test_panda_cc2_integration.py
docs/supervised-drying.md
openspec/changes/supervised-drying/
## Rules
No live device/service access or secrets. Preserve existing dirty files. No gate on suspected-corrupt host presented as trusted. Stop after two failing checks.
## Checks
Use repository containerized pytest only. run-tests.sh does NOT honor TEST_FILE; for focused runs override compose test command explicitly. Full suite once at end. Check host resources first, single lane.
