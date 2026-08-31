# Mandatory Stop Word

## Status

Design proposal for critique. No implementation is included in this document's
initial version.

The proposed stop phrase is **"stop"**. The feature is mandatory for devices
that are eligible to play voice responses.

## Contract

- The stop model accepts `stop` only, with pronunciation variants trained as
  data rather than a broad phrase list.
- It stops voice responses and announcements.
- It cancels an Assist pipeline that is still thinking.
- It does not stop background music.
- It does not start a follow-up Assist turn.
- Detection must flush already-buffered voice audio locally, before a network
  round trip.
- A device is not considered stop-ready until its model, runtime, firmware
  capability, and AEC state are all valid.

## Why It Is Not Current Barge-In

The current controller barge watcher recognizes the ordinary wake word during
thinking or playback. It sets `barge_detected`, flushes playback, and re-enters
the voice loop as a new turn. That is correct for "wake me again", not for
"stop".

The current `turn_engine.cancel_voice_turn(..., abort_ha=True)` also does not
cancel the actual Home Assistant Assist task. It cancels controller turn state
only. This is documented as a current gap in `controller/em_turn_engine.py`.

Stop therefore needs a dedicated interrupt path:

```text
stop model crosses threshold
        |
        +--> device immediately flushes voice speaker plane
        |
        +--> device sends stop_detected to controller
                    |
                    +--> controller cancels the matching turn
                    |
                    +--> HACS cancels Assist/TTS task
                    |
                    +--> controller persists outcome=stopped
```

## Readiness And Latency

The device wake-word detector needs 16 embeddings before it can score a
classifier:

- `ChunkSamples = 1280` at 16 kHz advances the stream by 80 ms.
- `FeatWindow = 16` embeddings are required by the classifier.
- `16 * 80 ms = 1.28 seconds`.

This is documented in `device/internal/wakeword/stream.go` and enforced by
`Detector.Ready()`. The shadow scorer counts frames before readiness as
`NotReady` in `device/internal/wakeword/shadow/shadow.go`.

This does **not** mean a stop utterance is necessarily lost if scoring starts
at playback. It means scoring would have at least roughly 1.28 seconds of
startup latency, and could miss a short command if the arm window ended first.
The selected CPU policy accepts this cold-start latency: the stop feature engine
runs only for an armed post-STT turn, rather than continuously while idle.

## Device Runtime

### Shared Feature Pipeline

Do not run a second complete `shadow.Scorer`. Each scorer duplicates the
expensive mel-spectrogram and embedding stages.

Refactor `device/internal/wakeword` so one streaming feature pipeline feeds
multiple classifier heads:

- active wake classifier;
- stop classifier.

The mel and embedding work is shared. Only the classifier heads are separate.
The engine starts when the controller arms stop detection after STT; this saves
idle CPU at the cost of the approximately 1.28-second initial readiness window.

The shared scorer must preserve the current audio-path guarantees:

- inference never runs inline on the mic deadline;
- the mic goroutine never blocks on inference;
- queue drops are counted;
- maximum inference time and producer gap are reported;
- model replacement is safe while a stream is running;
- the implementation remains fixture-testable without ONNX Runtime.

### Stop Arm

The controller sends a `stop_arm` message containing:

- `turnId`;
- monotonically increasing `generation`;
- phase: `thinking` or `playback`;
- expiry/dead-man interval;
- one calibrated stop threshold.

The device scores the stop model only while the current arm is valid. On a
crossing it:

1. atomically disarms that generation;
2. flushes only the voice speaker plane;
3. renders a short local confirmation pulse;
4. sends `stop_detected` with score, threshold, age, phase, and generation.

Late or duplicate events from older generations are ignored by both device and
controller.

### AFE Requirement

The stop model must hear speech over TTS without reacting to the device's own
voice. The paired Amazon AFE route is therefore mandatory while stop detection
is armed during playback.

- The device refuses an invalid `stop_arm` defensively.
- Stop-specific evaluation must include TTS-overlap and residual-echo tests.

### Capability

Add a firmware capability named `stopword`. Capability negotiation remains the
source of truth; do not compare firmware versions.

Older firmware should show stop as unavailable with a reason. It must not
silently claim to provide mandatory protection.

## Controller Runtime

### Configuration

Add to the Wake word section:

- `stopModel`;
- `stopThreshold` for thinking/quiet audio;
- `stopPlaybackThreshold` for speech over TTS;
- stop model readiness/health state, if persisted or exposed through status.

There should not be a user-facing permanent `stopEnabled` toggle. A valid
stop model is required for voice-response playback.

Update:

- `controller/em_db.py` defaults;
- `controller/em_config_sections.py`;
- the mirrored `CONFIG_SECTIONS` in `dashboard.jsx`;
- migration and config tests;
- add-on/config parity tests if the fields are environment-facing.

### Stop State Machine

Add a pure `controller/em_stop.py` decision/state module. It should model:

- arm;
- disarm;
- stop detection;
- duplicate detection;
- stale generation;
- turn terminal;
- device disconnect;
- arm expiry.

It should return decisions such as:

- ignore stale detection;
- accept local flush;
- cancel matching turn;
- report timeout/unavailable.

This keeps generation and idempotency logic testable without importing the full
controller.

### Arming Lifecycle

- Arm during `on_thinking` after STT has ended.
- Change to the playback threshold when TTS playback starts.
- Disarm at every terminal path: success, stop, cancel, error, timeout, and
  disconnect.
- Stop arming must work independently of the existing optional ordinary
  wake-word barge-in setting.

### `stop_detected` Handling

Add a distinct control-plane handler that validates:

- device capability;
- model readiness;
- active arm generation;
- active turn ID;
- phase;
- score, threshold, and age types.

On acceptance:

- persist detection timing;
- mark the controller turn cancelled/stopped;
- send a targeted cancellation event to HACS;
- do not set `barge_detected`;
- do not start another voice turn;
- do not send `music_flush` or call `em_player.stop()`.

### HACS Cancellation

The HACS integration must track the Assist pipeline task and TTS task by turn
ID. Add a targeted cancellation event/action carrying:

- `device_id`;
- `turn_id`;
- reason `stop`.

For a matching active turn, HACS must:

1. cancel the Assist pipeline task;
2. cancel the active TTS stream;
3. resolve the controller rendezvous with `tts/end` or an explicit cancel
   acknowledgement;
4. close the per-turn audio channel;
5. reject late pipeline/TTS events for that turn.

Adapt `em_runbarrier` so stale pipeline events cannot contaminate a later turn.
The stop path must not leave a cancelled turn holding `voice_lock` or waiting
for a TTS sentinel forever.

### Persisted Observability

Add a terminal outcome such as `stopped`, plus:

- stop model and score;
- threshold used;
- phase;
- device detection time;
- local flush latency;
- controller cancellation latency;
- HACS acknowledgement latency.

Add hourly counters for accepted stops, stale messages, model drops, and model
errors. Keep support bundles limited to aggregate/diagnostic fields; do not
include speech or raw recordings.

## Model Assets

### Model Metadata

Forge models should carry a sidecar manifest declaring:

- `kind: wake` or `kind: stop`;
- target phrase(s);
- Forge version;
- training provenance;
- model checksum.

Existing direct `.onnx` uploads should default to `kind: wake` for backward
compatibility. The controller must reject a wake model selected as a stop
model.

### Asset Planner

Extend `controller/em_oww_assets.py` so `_oww_wanted_models()` returns both
selected classifiers, ordered with the active wake and stop models first.

- Shared runtime, mel, and embedding files remain single copies.
- Both selected classifiers are pinned and cannot be evicted.
- LRU rollback slots must account for two required classifiers.
- Free-space checks must include both models.
- md5 verification and `.part` upload ordering remain unchanged.

### Install Before Arm

Never arm stop detection until the stop classifier is confirmed installed on the
device. Model changes use the existing install-before-switch shape:

1. push and verify the stop model;
2. send the config only after installation succeeds;
3. build the shared scorer;
4. report stop readiness;
5. arm only after readiness is confirmed.

If installation fails, voice playback must be blocked or the device must be
reported as not voice-ready. It must not silently play without the mandatory
stop path.

### New Device Provisioning

The stop classifier is not bundled into the device firmware, just as the
current device-local wake classifier is not bundled into firmware. Firmware
contains neither the ONNX Runtime nor classifier models; the controller owns
their bytes and the provisioning wizard installs them over USB/ADB.

For a new device, `GET /api/provision/oww_assets` must include the fleet's
selected wake model and selected stop model, plus the shared runtime,
melspectrogram, and embedding models. The wizard already transfers every asset
in that manifest with md5 verification before its final reboot; stop simply
becomes another required classifier in the same transaction.

A per-device stop-model override cannot be known before the device exists in
the controller database, so a fresh install always receives the fleet default.
After registration, an approved device override follows the ordinary
install-before-arm asset-sync path.

## Forge UI And Training

### Model Purpose

Add a model-purpose selector when creating a wake word:

- `Wake word`;
- `Stop word`.

Stop-word creation should default the target phrase to `stop`, write
`model_kind: stop`, and show stop-specific training guidance.

### Stop Training Page

The Forge UI should show:

- target phrase fixed to `stop` unless explicitly adding pronunciation variants;
- Google Chirp 3 locales, voices, samples, and QPS;
- separate quiet/thinking and playback thresholds;
- readiness requirements: device capability, installed assets, AEC;
- stop-specific evaluation results.

Google TTS provides broad synthetic coverage, but real post-AEC recordings are
the important data for this model.

### Stop Capture Labels

Add admin-only controller capture categories:

- `stop_act`: stop correctly interrupted;
- `stop_miss`: stop should have interrupted but did not;
- `false_stop`: unrelated speech caused a stop;
- `playback_negative`: TTS/echo that must not stop itself.

Export these with provenance in the ZIP manifest. Forge preserves positive and
negative polarity when importing them.

Training data should include:

- quiet and far-field `stop` recordings;
- speech over TTS;
- different rooms, distances, speakers, accents, and speaking rates;
- hard negatives such as `top`, `shop`, `drop`, `start`, `don't`, and ordinary
  conversation;
- the actual system TTS output and residual echo.

### Evaluation

Report separately for:

- quiet/thinking positives;
- stop over TTS positives;
- ordinary-speech negatives;
- false-stop playback negatives;
- real imported captures;
- synthetic Chirp/Piper clips.

Track recall at both thresholds, false stops per active-response hour, and
detection-to-flush latency. Do not approve a mandatory model from synthetic
recall alone.

## Controller Dashboard

Add a non-optional **Voice stop** panel under Wake word:

- stop-model selector;
- quiet and playback thresholds;
- readiness state;
- installed/missing model state;
- AEC requirement warning;
- firmware capability warning.

Show status values such as:

- `ready`;
- `installing`;
- `unsupported firmware`;
- `model missing`;
- `AEC required`;
- `runtime/model error`.

Activity should render `Stopped` distinctly from normal completion, no speech,
error, and ordinary barge-in.

## Device Feedback

Use a short local confirmation pulse for an accepted stop. Do not make it
dependent on the controller response, because the point of the local detector
is immediate feedback.

The device should log:

- stop model load/readiness;
- arm/disarm generation;
- score and threshold on crossing;
- local flush result;
- stale/duplicate detections;
- inference drops/errors.

## Tests

### Forge

- stop model metadata and purpose selection;
- stop config generation;
- positive/negative stop dataset import;
- stop-specific evaluation grouping;
- threshold and false-stop metrics;
- feature freshness after adding Chirp or imported clips.

### Controller

- `em_stop` state transitions;
- stale and duplicate generation handling;
- local flush acceptance;
- no new turn after stop;
- HACS cancellation acknowledgement;
- late pipeline events rejected;
- model install-before-arm;
- capability and AEC gates;
- asset planner with two pinned classifiers;
- config-section coverage and migration behavior.

### Device

- shared feature pipeline fixture compatibility;
- two classifier heads receiving identical embeddings;
- stop arm, disarm, expiry, refractory, and generation behavior;
- immediate voice-only flush;
- music unaffected;
- mic goroutine never blocked;
- safe model replacement during a live stream;
- capability/register/config protocol compatibility.

### End-to-End

- stop during thinking;
- stop during TTS with several seconds already buffered;
- stop during an announcement;
- TTS does not self-stop;
- music continues after voice stop;
- controller disconnect after local stop still silences voice;
- late Assist events cannot revive the cancelled turn.

## Rollout

1. Ship firmware shared multi-classifier support and `stopword` capability,
   initially reporting not-ready.
2. Ship controller asset roles, readiness, stop state, and real HACS task
   cancellation.
3. Train and shadow-test stop on one device at a time.
4. Install and validate the stop model and AEC on every target device.
5. Enforce the mandatory readiness gate before allowing voice playback.
6. Roll out fleet-wide only after real-room recall and false-stop criteria are
   met.

## Open Questions

- What false-stop rate per active-response hour is acceptable?
- Should a stop during thinking cancel only the current turn, or also suppress
  any queued continuation?
- Should a stop during an announcement report `stopped` or a separate
  `announcement_stopped` outcome?
- Should stop detection remain active during music playback when no voice turn
  is armed? The proposed answer is no: scope is voice responses only.
- How should an unsupported device be handled when a mandatory voice response
  is requested: refuse playback, or allow playback with a visible degraded
  state during rollout?

## Decision Worksheet

Each item below has options considered during design review. Chosen options are
recorded in `## Decisions`; alternatives remain as review history.

## Decisions

### D1. One-syllable model viability

**Decision: build in parallel.** Implement the shared runtime while collecting
post-AEC data. The phrase remains `stop` unless measurements later prove its
false-stop behavior unacceptable.

### D2. False-stop acceptance target

**Decision: fewer than 0.1 false stops per active-response hour.** This is the
initial rollout gate and must be measured on real post-AEC playback data, not
synthetic evaluation alone.

### D3. Stop model ownership

**Decision: fleet default with per-device override.** The stop model belongs
to the existing Wake word controller config section and follows the same fleet
inheritance/per-device override rules as other controller settings. The UI must
show clearly when a device overrides the fleet model.

### D5. HACS cancellation mechanism

**Decision: cancel the tracked pipeline task.** HACS will retain the task that
awaits `async_accept_pipeline_from_satellite()` and cancel it for a matching
turn, after verifying cancellation propagates correctly on supported Home
Assistant versions.

### D6. Device-to-controller delivery

**Decision: best effort.** Immediate device-local voice flush is authoritative.
The controller receives one generation-tagged `stop_detected` message and
recovers through terminal timeouts if it is lost. The message still needs arm
generation validation to avoid stopping a later turn after a delayed delivery.

### D4. What an active stop cancels

**Decision: thinking and voice playback.** Stop is armed after STT has ended.
It cancels Assist while thinking and flushes spoken output, but does not end an
active listening/STT phase.

### D7. Stop before TTS begins

**Decision: both safeguards.** On an accepted stop during thinking, the device
locally latches voice-plane discard-until-EOS and the controller cancels the
matching HACS pipeline. Either network delay or a late TTS result therefore
cannot make the stopped response audible.

### D8. Flush semantics

**Decision: reuse `speaker_flush`.** Stop uses the established voice-plane
drain plus discard-until-EOS behavior. It does not mute the amplifier or touch
the music plane.

### D9. Device inference architecture

**Decision: shared feature engine.** One streaming mel/embedding pipeline feeds
separate wake and stop classifier heads. This is required to keep Echo CPU cost
within budget.

### D10. Stop training signal

**Decision: post-AEC only.** Training and evaluation use the exact processed
signal the stop classifier sees during playback. Raw audio may be retained only
for diagnostics, never substituted into the training set.

### D11. Self-stop prevention

**Decision: AFE-informed stop training and calibration.** Add stop-specific
capture upload and configuration support analogous to wake activation and
near-miss captures. The controller collects post-AEC examples of successful
stops, missed stops, false stops, and playback residuals; Forge uses exported
labelled data to tune the model and its threshold. The exact runtime AEC-health
gate remains open because D12 selected enabled-AEC readiness rather than a
measured convergence gate.

### D12. AFE readiness policy

**Decision: paired AFE is sufficient.** Playback stop arming requires the
active paired AFE route, but does not wait for a separate measured health signal.

### D13. Threshold policy

**Decision: one threshold.** Use one calibrated stop threshold during thinking
and playback. The model/training data, rather than phase-specific threshold
adjustment, must handle post-AEC playback conditions.

### D14. Announcement policy

**Decision: all announcements are stoppable.** Announcements use the same stop
arm, local voice-plane flush, and HACS cancellation path as voice responses.

### D15. Unsupported firmware rollout

**Decision: hard gate.** Once stop is mandatory, a device without the stopword
capability cannot begin voice-response playback.

### D16. Runtime health failure

**Decision: visible degraded.** Continue voice playback if the stop scorer has
errors or sustained drops, but expose a persistent device/dashboard warning and
a repair action. Record the health state and do not report stop protection as
ready.

### D17. Mute behavior

**Decision: mute prevents output.** A hardware-muted device cannot start voice
responses or announcements, so there is never voice output that requires a
stop command while the microphone ADC is muted.

### D18. Multi-device announcements

**Decision: local only.** A stop heard by one device stops only that device's
voice plane and matching announcement turn. It does not cancel synchronized
audio on other devices.

### D19. Capturing missed stops

**Decision: armed post-AEC ring, matching wake capture workflow.** Keep a
short rolling post-AEC buffer for every armed turn. Snapshot it for accepted
stop activations and configurable stop near misses, then expose the clips for
admin labeling in the controller UI just like wake-word captures. The labels
remain positive/negative and are exported for Forge training.

### D20. Model package format

**Decision: controller registry.** Upload an ONNX file through the existing
model transport, then assign its `wake` or `stop` role and training metadata in
the controller dashboard. The registry is authoritative; no separate sidecar
upload is required.

### D21. Asset slots

**Decision: keep four slots.** The selected wake and stop classifiers occupy
two protected slots, leaving two slots for rollback or A/B testing.

### D22. Persistence taxonomy

**Decision: single `stopped` outcome.** Use one terminal outcome for response
and announcement interruption; the existing turn kind supplies the context.

### D23. User confirmation

**Decision: brief LED pulse.** The device renders a short local pulse after an
accepted stop, independently of controller/HACS acknowledgement.

### D24. Continuous feature processing cost

**Decision: active turns only.** Start the shared stop feature engine only
after STT ends and the controller arms the turn. This accepts the roughly
1.28-second initial readiness latency in exchange for no permanent idle CPU
cost.

### D11/D12. Final AEC runtime policy

**Decision: active AFE is the sole runtime gate.** Stop training/capture
workflow must collect post-AFE playback residuals and false stops for later
model and threshold tuning, but the device does not delay arming on a measured
AFE convergence/health signal. The active paired AFE route remains mandatory
for playback arming.

### D1. One-syllable model viability

- **A. Prove first:** train a small `stop` model with real post-AEC data and
  require recall/false-stop targets before infrastructure work.
- **B. Build in parallel:** implement the shared runtime first, while gathering
  data and accepting that the phrase may need to change later.
- **C. Change phrase now:** use a longer phrase such as `stop listening` to
  reduce ambiguity, at the cost of slower interruption.

### D2. False-stop acceptance target

- **A. Strict gate:** no false stops in a 10-hour real playback test before a
  model can be mandatory.
- **B. Measured budget:** set a target such as fewer than 0.1 false stops per
  active-response hour, then revise from fleet data.
- **C. Recall-first:** accept a higher early false-stop rate while tuning the
  threshold from field captures.

### D3. Stop model ownership

- **A. Controller-managed default:** ship one versioned stop model and do not
  expose model selection in normal UI.
- **B. Advanced custom model:** ship a default but permit admins to replace it
  after explicit health checks and warning acknowledgement.
- **C. Per-device model:** permit different stop models per room/device.

### D4. What an active stop cancels

- **A. Thinking and voice playback:** cancel Assist after STT ends and flush
  TTS/announcements. This is the proposal's current default.
- **B. Entire active voice interaction:** also end listening/STT before STT has
  completed.
- **C. Playback only:** do not cancel thinking; stop only audio already playing.

### D5. HACS cancellation mechanism

- **A. Cancel tracked task:** retain the task created for
  `async_accept_pipeline_from_satellite()` and cancel it, after verifying HA
  stops the underlying pipeline.
- **B. HA-native abort API:** use a documented Assist cancellation API if the
  supported HA versions provide one.
- **C. Cooperative cancellation:** retain the task but make the audio channel
  and pipeline-event handlers observe a cancellation token and resolve early.

### D6. Device-to-controller delivery

- **A. Acknowledged retry:** `stop_detected` carries an arm generation and is
  retried until `stop_ack` arrives or the arm expires.
- **B. Best effort only:** one control message; the local flush is authoritative
  and the controller recovers on terminal timeout.
- **C. State reconciliation:** device includes its last accepted stop generation
  in periodic stats until the controller observes it.

### D7. Stop before TTS begins

- **A. Voice-plane discard latch:** local stop enables discard-until-EOS even
  when no voice PCM has arrived yet.
- **B. Controller-only prevention:** rely on controller/HACS cancellation to
  prevent future TTS frames.
- **C. Both:** local discard latch immediately, plus controller cancellation.

### D8. Flush semantics

- **A. Reuse `speaker_flush`:** use the existing voice-plane drain plus
  discard-until-EOS contract unchanged.
- **B. New `stop_flush`:** make an explicit command/state with separate stop
  telemetry, while sharing the underlying implementation.
- **C. Hard mute:** temporarily mute the amplifier in addition to flushing.

### D9. Device inference architecture

- **A. Shared feature engine:** one mel/embedding stream, two classifier heads.
  This is the recommended architecture.
- **B. Two independent scorers:** simplest code but duplicates the expensive
  feature work and is likely too costly.
- **C. One multi-class ONNX head:** train/export a new multi-output model that
  emits wake and stop scores together.

### D10. Stop training signal

- **A. Post-AEC only:** train/evaluate against the exact controller-bound audio
  signal the device scores during playback.
- **B. Mixed raw and post-AEC:** retain raw clips for diagnosis, but train on
  both signal types.
- **C. Synthetic mix:** add TTS to recordings in Forge to approximate overlap.

### D11. Self-stop prevention

- **A. Data and threshold:** train TTS/echo negatives and use separate playback
  threshold.
- **B. AEC-health gate:** arm only once measured AEC residual is below a health
  threshold.
- **C. Playback content suppression:** suppress stop scoring while outbound TTS
  contains the word `stop`.

### D12. AFE readiness policy

- **A. Active route is sufficient:** require paired AFE, accept early-turn risk.
- **B. Measured health:** require a healthy AFE signal before arming.
- **C. Delay arming:** wait a fixed settling period after playback begins.

### D13. Threshold policy

- **A. Two thresholds:** normal threshold during thinking and lower calibrated
  threshold during playback.
- **B. One conservative threshold:** same threshold in both phases for simpler
  operation and fewer self-stops.
- **C. Adaptive threshold:** derive playback threshold from AEC residual/noise.

### D14. Announcement policy

- **A. All announcements stoppable:** same behavior as TTS responses.
- **B. Per-announcement flag:** callers choose stoppable/non-stoppable.
- **C. Safety class:** ordinary announcements stop; safety/critical ones do not.

### D15. Unsupported firmware rollout

- **A. Hard gate:** refuse voice playback on non-stop-capable devices once the
  feature is mandatory.
- **B. Timed transition:** show a degraded warning during a defined rollout
  window, then enforce the hard gate.
- **C. Controller fallback:** use controller-side stop detection temporarily.

### D16. Runtime health failure

- **A. Fail closed:** refuse new voice playback when stop scorer errors or drops
  exceed a threshold.
- **B. Degraded but visible:** continue playback with persistent dashboard and
  device warning until repaired.
- **C. Automatic recovery:** restart/reload the scorer, then fail closed only if
  recovery fails.

### D17. Mute behavior

- **A. Mute prevents output:** muted devices cannot start voice responses, so
  stop unavailability while muted is irrelevant.
- **B. Mute permits announcements:** announcements may play, but stop remains
  unavailable because mic ADC is muted.
- **C. Special stop path:** keep enough microphone processing active to detect
  stop while hardware mute is on.

### D18. Multi-device announcements

- **A. Local-only stop:** stopping one device stops only that device's response.
- **B. Group stop:** a stop on any participating device cancels all synchronized
  announcement turns.
- **C. Configurable grouping:** caller marks announcements local or grouped.

### D19. Capturing missed stops

- **A. Manual capture button:** admin marks a missed stop immediately; the
  controller saves the preceding post-AEC ring.
- **B. Always-on armed ring:** retain a short rolling post-AEC buffer for every
  armed turn, then expose it after turn completion for admin labeling.
- **C. Follow-up report:** user says the wake word and reports a missed stop;
  controller correlates to the prior turn.

### D20. Model package format

- **A. ZIP package:** upload `.onnx` plus required JSON manifest atomically.
- **B. ONNX sidecar:** keep current ONNX upload and require a matching JSON file.
- **C. Controller registry:** upload ONNX only; admin assigns role/metadata in
  the controller dashboard.

### D21. Asset slots

- **A. Keep four slots:** wake + stop + two rollback/A-B slots.
- **B. Expand to six slots:** wake + stop + four rollback/experiment slots.
- **C. Dedicated stop slot:** reserve one non-LRU stop classifier plus existing
  wake classifier slots.

### D22. Persistence taxonomy

- **A. Single `stopped` outcome:** one terminal outcome for response and
  announcement interruption, with a `turn.kind` field for context.
- **B. Separate outcomes:** `response_stopped` and `announcement_stopped`.
- **C. Event plus cancellation:** retain existing terminal outcomes and add a
  separate stop event record.

### D23. User confirmation

- **A. Brief LED pulse:** local ring pulse confirms accepted stop.
- **B. Silence only:** immediate silence is the confirmation.
- **C. LED plus short tone:** explicit audiovisual confirmation.

### D24. Continuous feature processing cost

- **A. Always warm:** accept permanent shared feature-engine CPU cost for
  immediate stop response.
- **B. Warm only in active turns:** accept cold-start latency during thinking
  and playback.
- **C. Device-specific policy:** always warm only on devices meeting measured
  CPU/thermal headroom criteria.
