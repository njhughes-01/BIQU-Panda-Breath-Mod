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
- **AND** `Panda.py`, `drying_supervisor.py`, `panda_native_mqtt.py` and `panda_web.py` match the final source hashes in the test and rebuilt production images, and non-root persistence works on a fresh mounted test volume

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

#### Scenario: Corrected runtime supersedes a packaging-only review
- **WHEN** runtime code changes after the ed159dd packaging review and passive status failure
- **THEN** the candidate requires new exact-head focused/full-suite evidence, a rebuilt production image and a fresh independent Opus review
- **AND** deployment requires fresh passive terminal/OFF readiness; the withdrawn hypothetical PETG/session decision SHALL NOT be reinstated as a blocker

#### Scenario: Deployment encounters an armed or stopping session
- **WHEN** a session is active or a Stop is pending
- **THEN** the parent defers recreation until the existing session naturally reaches a confirmed terminal state without cancelling it or issuing a new command
- **AND** fresh passive state, not the historical completed-cycle report, establishes readiness

#### Scenario: Image rollback
- **WHEN** candidate startup or passive verification fails
- **THEN** the parent restores changed services serially to each service's captured prior image (supervised backend and separate web) and effective configuration while retaining current settings, runtime state volume and fixed deadline
- **AND** it does not switch to an unsupervised image or stale session backup, drop the device overlay, or issue heater/printer commands

#### Scenario: Stock startup reproduces current settings
- **WHEN** the actual stock entrypoint generates configuration from the preserved environment
- **THEN** the parent requires its pure in-memory prediction to match current mounted bytes immediately before and after recreation and preserves actual entrypoint/command/environment without a bypass
- **AND** mismatch, active preheat pause or newly present unpreserved preheat state defers recreation rather than overwriting current state

#### Scenario: Separate web image needs the reviewed help
- **WHEN** the candidate contains corrected backend reporting and web help but the running web is a separate old image
- **THEN** the parent applies image-only overrides preserving each service's effective startup/environment/networks/mounts, updates backend first, verifies fresh OFF/proof none, and only then updates web to the same reviewed artifact
- **AND** existing API/web GETs verify help and status; only the backend connects directly to Panda, no command POST or new cycle occurs, and broker container identity and current settings/session remain unchanged

### Requirement: Native heating evidence belongs to the current observed episode
The transport SHALL expose `heating_evidence_status()` as the source for native Heat and drying-proof reports. Confirmed evidence SHALL require the existing temperature-rise proof within the current continuously observed running Dry episode. No report SHALL claim physical relay measurement or infer OFF from command delivery.

#### Scenario: Previously confirmed Dry is now stopped
- **WHEN** a fresh complete pinned-device snapshot reports Dry OFF, firmware work OFF or non-Dry mode after an earlier confirmed rise
- **THEN** the old proof and pending observation are invalidated and proof reports none, with binary Heat OFF meaning no current confirmed rise evidence
- **AND** residual remaining time or later temperature rise cannot revive that proof; no session mutation or device command is emitted by reporting

#### Scenario: Mod Stop holds the safety lock while telemetry continues
- **WHEN** the connected receive loop has published confirmed proof and Heat ON, then real mod Stop locks controls and fresh OFF telemetry arrives on that same connection
- **THEN** retained proof becomes none and retained Heat becomes OFF while the lock remains held, without needing unlock or disconnect
- **AND** both native publications use the same current evidence sample before the existing lock skip; reporting adds no commands and changes neither Stop order, locks, supervisor state nor deadline

#### Scenario: Stop is unconfirmed or telemetry is interrupted
- **WHEN** a stop intent is accepted or a disconnect, availability loss or telemetry gap invalidates the observation
- **THEN** old heating proof cannot survive into a later episode; unavailable state reports unknown proof
- **AND** this does not confirm physical OFF, clear pending Stop, resolve the uncertain-start latch or authorize an ON

### Requirement: Direct native dry-temperature commands obey established bounds
Native command paths SHALL reject nonfinite or out-of-range dry temperature before integer conversion, local selection acceptance or device publication. Inclusive bounds SHALL be 40–60°C. Native discovery SHALL advertise those bounds; legacy behavior SHALL remain unchanged.

#### Scenario: Direct number bypasses the supervised start form
- **WHEN** a non-retained dry_temp/set or transport filament_temp command supplies 80, -5 or another value outside the native range
- **THEN** selected temperature remains unchanged and no custom_temp, timer, mode or ON command is published
- **AND** mixed transport envelopes are rejected atomically, while out-of-range incoming telemetry remains observable for status/Stop

#### Scenario: Valid number changes configuration only
- **WHEN** 40, 48 or 60 is explicitly selected with fresh native state
- **THEN** only custom_temp may be sent; the command does not arm, reset or start a timer

### Requirement: Generic native ON cannot bypass Dry ownership
The generic work_on ON control SHALL NOT start/reset a native Dry timer, arm a session or cancel existing supervised intent. Explicit Dry and the existing guarded supervisor SHALL retain start ownership.

#### Scenario: Generic ON arrives in Dry or during supervised intent
- **WHEN** native mode is Dry, supervised intent is running or owes Stop, telemetry is stale, or cancellation/lock overtakes the queued ON
- **THEN** generic ON emits no device command or successful optimistic ON and does not change session identity/deadline
- **AND** explicit OFF still persists cancellation before device stop and keeps the fresh post-write confirmation requirement

#### Scenario: Generic ON outside native Dry
- **WHEN** fresh non-Dry state and uncancelled/unlocked controls allow generic ON with no running/pending-stop session
- **THEN** native mode sends only firmware work_on ON, never drying_running ON
- **AND** legacy WebSocket behavior remains unchanged

### Requirement: Clock status is an observational diagnostic
Supervisor status SHALL report sampled clock inhibition accurately without changing operational clock tracking, persisted state, deadlines or recovery decisions.

#### Scenario: Valid session is inspected before deadline
- **WHEN** status is requested with valid clocks and no applicable inhibit reason
- **THEN** inhibit is None rather than clock unusable
- **AND** repeated status calls leave state bytes, high-water mark, deadline anchors and observation bookkeeping unchanged; real invalid/regressed/elapsed clocks retain their existing reason vocabulary

### Requirement: Preserve existing PETG and explicitly armed Dry scope
The integration SHALL preserve existing ordinary PETG printing and explicitly armed Dry contracts. This finish SHALL NOT add PETG/Dry arbitration, an interlock, an overlap test or a pending product decision. It SHALL NOT infer a general PETG policy waiver or new heat-cycle authority.

#### Scenario: Hypothetical overlap requirement is withdrawn
- **WHEN** Nathan rejects the added overlap question as scope expansion with no actual overlap observed
- **THEN** the finish proceeds through authorized task 14 and the parent's existing review/deployment checks without that added blocker
- **AND** ordinary PETG tests are not represented as combined-case verification, and no new print/cycle action is authorized
