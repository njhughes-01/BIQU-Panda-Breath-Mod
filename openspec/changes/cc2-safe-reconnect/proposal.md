# Safe CC2 reconnect
Goal: reconnect without changing the Panda's saved printer binding or timed drying cycle.
Done when: CC2 normal reconnect sends no printer binding or unsupported status request; CC2 emergency lock still sends off; legacy emulation retains its binding; all existing heater/cancellation tests pass. Live verification must distinguish a connection timeout from a command or timer reset.
Scope: Panda.py and the existing websocket-loop tests. Preserve the current uncommitted safety/drying baseline and all device settings. No AP, router, firmware or heater commands as part of building/tests.
