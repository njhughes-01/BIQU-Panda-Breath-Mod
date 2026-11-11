## ADDED Requirements

### Requirement: Preserve approved MQTT and supervised timer behavior
The integration SHALL preserve the existing approved behavior, including explicitly armed recovery, and SHALL NOT infer a new timer session from startup, reconnect, retained telemetry or idle state.

#### Scenario: Reconnection during a deliberately armed session
- **WHEN** fresh nonretained pinned-device observations report OFF before the persisted fixed deadline
- **THEN** existing bounded recovery may run after two distinct OFF snapshots, including physical Panda OFF, using remaining duration without extending that deadline
- **AND** uncertainty, locks, invalid clocks, persistence failure, cancellation and expiry continue to veto ON

#### Scenario: Mod Stop overtakes queued or active recovery
- **WHEN** mod Stop, power OFF, mode change or a lock cancels the session
- **THEN** cancellation invalidates queued work and persists the owed Stop before device control
- **AND** restart/reconnect cannot rearm it; pending Stop remains until fresh device confirmation

#### Scenario: Broker delivery cannot prove heating or OFF
- **WHEN** a command is sent without matching live device evidence
- **THEN** the mod reports uncertainty rather than claiming heating or physical OFF
- **AND** timer confirmation and temperature-rise proof remain separate

### Requirement: Build artifacts exclude runtime intent
Production and test image build contexts SHALL exclude local preheat state, drying session state, drying state directories and atomic session-write remnants, while packaging the actual implementation and maintaining the writable non-root state directory.

#### Scenario: Building beside stale state
- **WHEN** a source-export build context contains synthetic preheat/session files and a synthetic dry-state directory
- **THEN** none of those files or their contents appear in the image
- **AND** `drying_supervisor.py` and `panda_native_mqtt.py` remain packaged and non-root persistence works on a fresh mounted test volume

### Requirement: Evidence uses actual integration and deployment bases
Review SHALL cover the complete relevant change and test evidence. Merge assessment SHALL use the current actual PR base/head and complete untruncated evidence, with protected-file policy and failures/limitations included.

#### Scenario: Historical dependencies exceed the gate's evidence limit
- **WHEN** the full actual integration evidence does not fit or protected-file policy fails
- **THEN** no merge score or merge success is claimed
- **AND** a production comparison or fabricated stack does not replace the actual merge diff

#### Scenario: Historical test run is not on the candidate
- **WHEN** the healthy historical suite passed at `e84b054` but the reviewed head differs
- **THEN** that result is identified as historical and the final candidate is tested independently on healthy Spinx

### Requirement: Deployment preserves settings and state
Only the parent SHALL deploy after passing Opus review and final offline checks. Deployment SHALL retain the effective configuration, credentials/mounts, device identity, separate broker, preheat state and current supervised session; it SHALL NOT arm a new cycle or overwrite runtime state from Git or backups.

#### Scenario: Deployment encounters an armed or stopping session
- **WHEN** a session is active or a Stop is pending
- **THEN** the parent defers recreation until the existing session naturally reaches a confirmed terminal state without cancelling it or issuing a new command
- **AND** fresh passive state, not the historical completed-cycle report, establishes readiness

#### Scenario: Image rollback
- **WHEN** candidate startup or passive verification fails
- **THEN** the parent restores the prior supervised image and effective deployment configuration while retaining the current runtime state volume and fixed deadline
- **AND** it does not switch to an unsupervised image or stale session backup, drop the device overlay, or issue heater/printer commands
