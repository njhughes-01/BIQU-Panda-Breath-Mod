# Native Panda MQTT

Goal: replace routine Panda device WebSocket control/status with native V1.0.4 MQTT, using a dedicated device broker independent of Home Assistant, while preserving all current drying/CC2 settings and safeguards.

Nathan approved design.md on 2026-11-09 and then clarified that the device connection must not use the HA broker. No new design decision remains for implementation. Production deployment remains a separate approval after the reviewed build is ready.

Done when: design.md acceptance criteria pass; exact source tree tests and independent review are recorded; device-native readback proves MQTT operation after authorized cutover. Never infer success from broker delivery or send a heater test without fresh idle state and the existing 50°C/8h authorization.
