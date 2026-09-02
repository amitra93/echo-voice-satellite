# Device-Only Wake Word Design

Status: implemented; manual single-device hardware verification pending

## Target Architecture

Wake word detection runs only on the Echo device. The controller never receives
an idle microphone stream and never loads or runs a wake word model.

The device continues capturing microphone audio locally because local capture is
required for detection. Except for opted-in wake training captures, microphone
audio does not leave the device until the controller grants a voice session.

The required startup sequence is:

1. The device detects a wake word locally.
2. The device asks the controller for permission to start a session.
3. The controller prepares the Home Assistant turn and grants or denies the
   request.
4. Only after a grant does the device start its configured listening animation
   and begin sending audio for STT.

Physical action-button turns use the same admission sequence. Wakeword-based
barge-in is also detected only on the device.

## Implementation State

The controller accepts wake turns only through `wake_request_v1`. Firmware that
lacks that capability remains visible for recovery and update, but is explicitly
incompatible for wake detection. The controller drops idle microphone PCM and
does not load a wake model, OpenWakeWord, ONNX Runtime, Speex wake denoise, or a
GPU inference backend.

Wake model selection, thresholding, detector health, near-miss selection,
barge-in, and capture generation are device responsibilities. The controller
retains classifier upload, asset distribution, install-before-switch behavior,
turn admission, capture storage, labeling, and Forge export. Its image carries
ONNX files and the ARM runtime solely to distribute them to devices.

The dashboard exposes device model and threshold controls, detector readiness,
asset repair, barge-in, and capture privacy settings. It contains no
controller/device detector-location modes or controller inference controls.

## Accepted Decisions

- The final controller requires firmware supporting the new protocol. It does
  not retain controller wake detection as a compatibility fallback.
- Current code, UI, comments, and current-architecture documentation will no
  longer describe controller wake detection. Historical changelogs, engineering
  journals, and deployed append-only database migrations remain accurate
  historical records.
- The controller grants a request only after Home Assistant has accepted the
  provisional turn and attached its audio connection.
- After a grant, the device sends local PCM preroll before live audio so STT does
  not lose the beginning of the command.
- Opted-in wake captures are retained for detector activations, including
  requests denied because of arbitration, a busy device, or backend failure,
  subject to the bounded RAM queue. Locally muted crossings are discarded.
- A near-miss capture represents the peak score within one debounced utterance,
  not every frame or the first score over the near-miss floor.
- Training captures may use the device's configured data link, whether plaintext
  or TLS.
- Multi-device arbitration remains immediate first-request-wins. It adds no
  proposal-window latency, with the accepted limitation that network jitter can
  occasionally select the wrong room.
- Barge-in wake detection is device-only.
- Physical action-button turns use the same request/grant boundary as wakeword
  turns.
- If the selected classifier is missing or fails to load, wake detection is
  explicitly unavailable. The device does not fall back to its previous model,
  a stock model, or controller detection.

## Protocol

### Capability and Readiness

Firmware advertises a new capability for the complete request/grant contract,
for example `wake_request_v1`. Existing `oww_trigger` capability alone is not
sufficient because it does not prove support for admission, bounded turn audio,
or capture upload. The current controller gates request admission on this
capability; capture frames are accepted through the same data connection based
on their protocol contents and live model/privacy state rather than a second
capability check.

The device reports detector readiness after detector configuration is applied or
a model load is attempted:

```json
{
  "type": "wake_status",
  "model": "hey_jarvis",
  "classifierMd5": "...",
  "ready": true
}
```

Successful status includes `classifierMd5`; failed status includes `error`.
`ready` currently means that the scorer loaded, not that its feature pipeline is
already warm. There is no asset-directory watcher: an asset change is observed
when reconciliation/configuration causes the detector to be applied again.

The controller treats a request-capable device as unavailable for wake turns
until the status matches the selected model and reports `ready: true`.

### Wake Request

The device sends a correlated request without changing its LEDs or starting a
network audio stream:

```json
{
  "type": "wake_request",
  "requestId": "boot-nonce:counter",
  "source": "wakeword",
  "model": "hey_jarvis",
  "score": 0.82,
  "threshold": 0.5,
  "ageMs": 31,
  "activationSeq": 4201
}
```

Button releases normally carry the same correlated `requestId` on their existing
`button` message. The device permits one pending admission at a time: a second
button/wake source is locally coalesced with that pending request rather than
creating another request. The controller classifies the gesture before admitting
a tap, so a hold or timer-dismiss tap cannot race a separate request into a voice
turn. Once classified as a voice turn, it follows the same grant/deny/start
boundary.

The request ID combines a per-process random nonce with a monotonic counter. A
16-bit audio sequence alone is not a safe request identifier because it wraps
and resets when a stream restarts.

### Admission

Before granting, the controller verifies that:

- The request belongs to the current control connection.
- The device is approved, connected, unmuted, and data-ready.
- The matching detector model is ready for wakeword requests.
- No ordinary device-owned turn or admission reservation already owns the
  device. A HACS-created announcement does not hold this lock; in that case HACS
  can reject the provisional offer instead of the controller issuing an
  immediate `busy` denial.
- Required stop-word resources are ready.
- The request is fresh.
- Fleet arbitration grants this device.
- Home Assistant accepts the provisional turn and attaches its audio WebSocket.

Admission takes a reservation before awaiting Home Assistant. Checking only
`voice_lock.locked()` leaves a race in which two requests can both pass before
either turn acquires the lock.

Ordinary requests received while the device is busy are denied immediately,
not queued. A valid wakeword request during an active turn follows the separate
barge-in path when barge-in is enabled.

### Grant and Denial

```json
{
  "type": "wake_grant",
  "requestId": "boot-nonce:counter",
  "turnId": 123
}
```

```json
{
  "type": "wake_deny",
  "requestId": "boot-nonce:counter",
  "reason": "busy"
}
```

Denial reasons include `muted`, `busy`, `arbitration`, `not_ready`, `no_data`,
`stale`, `backend_unavailable`, `disconnected`, and `barge_disabled`.

The device opens its microphone gate only after receiving a matching,
non-expired grant and rechecking local mute and readiness state. It starts the
configured listening animation at that point when the animation is present and
valid; there is no device-side fallback animation. It ignores duplicate, stale,
and mismatched decisions.

After opening the microphone gate, and starting the animation when configured,
the device sends:

```json
{
  "type": "wake_started",
  "requestId": "boot-nonce:counter",
  "turnId": 123
}
```

This acknowledgement distinguishes a granted request from one whose response
was lost before the device acted on it.

## Device Audio Flow

### Local Capture and Detection

While the data connection is established and the device is unmuted, the Amazon
AFE's 16 kHz mono PCM is fed to the local detector and a bounded RAM ring. The
stream closes on mute or data/control disconnect, so local detection is not yet
available through a controller/data-plane outage. Idle PCM from a
`wake_request_v1` device is not sent to the controller.

The ring preserves 80 ms frame boundaries and sequence numbers and retains up
to eight seconds: two seconds of turn preroll plus the four-second request
lifetime and margin. It is cleared on stream restart/stop, detector reset, model
replacement, and capture disable. A sequence gap is not cleared at insertion;
turn replay rejects a gapped window and training snapshots retain only the safe
contiguous suffix. The production audio format is fixed rather than changed at
runtime.

Detector inference remains off the microphone goroutine. A full inference queue
drops frames and reports health counters rather than blocking capture.

### Granted Turn Stream

After a grant, the device starts a bounded turn stream over `/data`:

1. Send a contiguous local window beginning at the configured pre-activation
   point and extending through frames accumulated while admission was pending.
2. Continue with live PCM frames.
3. Stop on endpoint/VAD, cancellation, mute, or disconnect. A five-second local
   deadline covers the no-speech case; there is no separate total-duration
   deadline after speech begins.

Outside opted-in training capture upload, the stream is the only ordinary
microphone audio sent to the controller. The controller forwards it to the
already-accepted Home Assistant turn.

Local wake scoring remains active during all bounded streams, including button
turns, so barge-in never requires controller inference.

## Home Assistant Admission

The turn `accept` and `reject` routes are now functional rather than no-ops.
Turn startup is split into preparation and execution:

1. The controller reserves the device and creates a provisional turn.
2. The controller broadcasts `wake.offer`.
3. HACS attaches the turn audio WebSocket.
4. HACS posts `accept`, or posts `reject` when it cannot own the turn.
5. The controller sends the device grant only after acceptance.
6. The device opens turn audio and starts its configured listening animation.
7. The controller executes and persists the prepared turn.

An HA rejection, attachment failure, or acceptance timeout produces a device
denial. It does not light the ring, start audio, interrupt music, or take a beam
lock.

## Arbitration

The first eligible wake request received by the controller wins immediately.
The winner is reserved before any awaited work. Other requests in the existing
suppression window receive `wake_deny` with reason `arbitration` and never light
their rings.

This deliberately preserves the current no-added-latency policy. Reported
device crossing age remains useful for metrics but does not revoke a grant or
introduce a proposal window.

## Barge-In

The controller no longer runs a barge-in wake model. The device continues local
scoring while a turn is listening, thinking, or speaking and uses the effective
barge threshold while speaker playback is active.

For a fresh wake request during an active turn, the current controller checks
that barge-in is enabled and that an active turn exists. It does not yet apply a
separate per-phase eligibility policy. The live sequence is:

1. Cancel the exact active Home Assistant turn and emit a targeted
   `turn.cancel` to HACS.
2. Flush speaker playback when needed and end the old turn as barged.
3. Let the old turn unwind and release ownership.
4. Create and admit the replacement turn through the ordinary HA-before-grant
   sequence, then accept its bounded audio stream and local preroll.

The device-originated barge path now reaches the exact running Assist pipeline:
HACS matches both device and turn IDs, cancels its pipeline/TTS tasks, and closes
the old audio channel. Generic button/mute cancellation still uses the ordinary
turn cancellation path and is not the same immediate targeted barge operation.

### Barge-In vs. the Stopword

Two separate device-originated interrupts exist during an active turn, and they
are expected to do different things:

- **The wake phrase (this section's mechanism)** cancels the active turn AND
  immediately opens a replacement turn — a fresh listening session, because
  saying the wake word again means "I have something new to say." This is the
  `wake_request`-during-`voice_lock` path documented above, ending in
  `admit_barge` and the `device.barge_detected` restart loop in
  `_run_voice_locked`.
- **The stopword ("stop")** cancels the active turn and stops there — no
  replacement turn opens. It is a pure interrupt, not a new command, and rides
  a completely separate protocol: the generation-checked `stop_arm`/
  `stop_detected` messages, `em_stop.StopState`, and its own dedicated
  classifier (`stop.onnx`), calling `turn_engine.stop_voice_turn` — which only
  ever cancels, never sets `barge_detected`. "Stop" during a ringing timer
  alarm dismisses the alarm specifically rather than cancelling a voice turn.

Confirmed on hardware 2026-09-02: both paths work — the wake-phrase path
(5.1/5.2) and the stopword path (5.3, after the controller and firmware fixes
recorded there).

## Wake Training Captures

Training capture remains opt-in and writes recognizable speech. For
`wake_request_v1` devices, selection runs on the device because the controller
does not receive idle audio or score it.

### Selection

For trusted local detector scores:

- A score at or above the effective threshold is an activation candidate.
- A score above `wakeNearMissFloor` and below the threshold is a near-miss
  candidate.
- Activation and near-miss candidates share a per-utterance debounce window.
- The device retains the peak-scoring near-miss within the debounced utterance
  and snapshots PCM ending at that frame.

Captures use the configured `wakeCaptureSec`, with 62 complete 80 ms frames
(4.96 seconds) as the hard maximum and one frame as the minimum. A sequence gap
produces only the safe contiguous suffix and marks the capture incomplete.

Activation captures are retained whether the controller grants or denies the
associated session when queue space or an evictable near miss is available.
Locally muted crossings are removed without capture, and a queue containing four
activations drops another activation rather than becoming unbounded.

### Device Queue

Capture PCM is held only in bounded RAM and is never written to device flash.
The queue holds four captures, complete or safe incomplete suffixes. Queued
activations precede near misses; when full, an activation may evict the oldest
non-in-flight near miss, but never another activation.

Disabling capture synchronously invalidates the upload generation, clears queued
training captures and retries, clears the shared PCM ring at that instant, and
closes the data connection to terminate a partial upload. The ring is repopulated
after reconnect because it is also required for turn preroll. A reboot or power
loss loses queued captures by design.

### Upload Timing

- Near misses upload whenever no granted STT stream owns the upload direction and
  the data connection is available; thinking or playback does not itself block
  them.
- An accepted activation capture is snapshotted immediately but uploads after
  the STT microphone phase ends.
- A denied activation capture uploads immediately after the denial because no
  STT stream will start.
- Live STT audio pauses capture traffic after at most the current 80 ms capture
  chunk.
- Ready activation captures are selected before queued near misses. An already
  in-flight near-miss transfer is paused for STT rather than replaced by a newly
  ready activation.
- A disconnected device retains captures within its bounded RAM queue and
  retries after reconnect.
- A normally completed upload remains queued until the controller acknowledges
  durable storage. Capture disable/model identity change, bounded-queue eviction,
  reboot, and power loss are deliberate non-ACK removal paths.

Capture uploads use the device's configured data connection. They are permitted
over either plaintext or TLS; enabling TLS remains recommended but is not a
capture requirement.

### Binary Transfer

Capture PCM uses dedicated chunked frame types on `/data`, not base64 JSON on
the control plane:

```text
CAPTURE_BEGIN
CAPTURE_PCM
CAPTURE_END
```

Metadata includes the capture ID, kind, model, classifier checksum, score,
effective threshold, near-miss floor, activation sequence, requested and actual
preroll, completeness, audio format, and whether the barge threshold was active.

The end frame includes byte and chunk counts plus an MD5 digest. MD5 is transfer
integrity detection, not a security primitive. The sender yields between chunks
so live turn audio can preempt capture traffic.

The controller validates protocol version, metadata size, PCM size and format,
chunk order, digest, model identity, and the current effective privacy setting.
It deduplicates by device and capture ID, stores the WAV atomically, then sends a
control-plane acknowledgement.

The existing training dashboard, labeling, trimming, retention, ZIP export, and
Forge import remain. Metadata sidecars move, delete, prune, and export with their
WAV files.

## Controller, UI, and Configuration

The controller has no wake-model construction, idle microphone queue, wake
warm-up, near-miss scoring, detector comparison, controller barge-in inference,
Speex wake denoise, or GPU inference option. `owwOnDevice`, `owwSpeexNs`,
`OWW_MODEL`, and `OWW_THRESHOLD` were removed from stored settings, add-on
options, environment configuration, and the dashboard.

Generic persisted `wake_model`, `wake_score`, and `wake_threshold` remain as
device-reported turn data. Historical comparison columns remain readable in
deployed databases but are not populated or presented as current functionality.

The controller retains device model upload, asset distribution, install-before-
switch behavior, classifier reconciliation, turn handling, training storage,
and Forge export. Device provisioning assets are separated from the Python
OpenWakeWord runtime, which is absent from the running image.

The final dashboard retains and presents:

- Device wake model and threshold.
- Detector readiness and selected-model match.
- Device inference health, errors, dropped frames, and inference latency.
- Asset installation and repair status.
- Device-side barge-in settings.
- Device-side wake capture settings and privacy warning.
- An explicit unsupported-firmware or detector-unavailable state.

Model, threshold, capture, barge-in, and arbitration settings remain device
configuration in the final architecture.

## Compatibility and Migration

The database migration list remains append-only. In the device-only architecture,
existing detector-comparison columns and old trigger labels remain readable for
historical rows, but new turns do not populate controller score fields.

Deployment is firmware-first:

1. Define and test the request/grant, readiness, bounded turn audio, and capture
   upload protocols.
2. Ship firmware with local buffering, admission, capture generation, and
   explicit detector readiness.
3. Install and verify runtime, shared models, and selected classifiers on every
   device.
4. Confirm devices report the new capability and matching ready status.
5. Ship the controller and HACS changes together.
6. The new controller marks older firmware incompatible rather than accepting a
   device that cannot wake.
7. Remove controller-inference code, dependencies, UI, comments, and
   current-architecture documentation.

The controller marks firmware without `wake_request_v1` incompatible and never
starts a compatibility detector.

## Implementation Plan

Ownership below names the component responsible for enforcing each decision;
cross-component changes are verified by controller integration tests and the
hardware release checklist.

| Step | Status | Owner | Work and decisions owned |
|---|---|---|---|
| 1 | Complete | Device firmware | Implement `wake_request_v1`, detector load readiness, request expiry, and unavailable-on-model-failure behavior. Unsupported firmware is incompatible for wake detection. |
| 2 | Complete | Device firmware | Add the bounded local PCM ring and grant-gated turn stream. Open turn audio and the configured listening animation only after a matching grant. Apply the same boundary to button turns. |
| 3 | Complete | Controller and HACS | Add provisional turn preparation plus real `accept`/`reject` handling. Send a grant only after HACS attaches and accepts; deny on timeout or failure. |
| 4 | Complete | Controller | Add admission reservations, immediate first-request-wins arbitration, explicit loser denial, stale-connection protection, and cleanup on cancellation or disconnect. |
| 5 | Complete | Device firmware and controller | Move live barge detection to the device, route active-turn wake requests through targeted cancellation and replacement admission, and make cancellation reach the exact running HA pipeline. |
| 6 | Software complete; hardware verification pending | Device firmware and controller capture storage | Select peak-score near misses, retain bounded best-effort activation captures including denied requests, and upload them after STT or whenever no STT stream owns the configured plaintext or TLS link. |
| 7 | Complete | Controller, dashboard, and packaging | Removed controller inference, comparison modes, Speex/GPU options, fallback behavior, and runtime dependencies while retaining device model asset distribution. Unsupported firmware and detector-unavailable states are surfaced. |
| 8 | Complete | Documentation and release | Removed controller-detection claims from current UI/configuration and current documentation while preserving historical changelogs, journals, and append-only migrations. Firmware deployed before the controller; the HACS client protocol is unchanged. |

The software migration is complete. Hardware tests still need to prove that no
idle PCM leaves the device and no listening animation or turn audio starts
before HA-backed admission.

## Verification

Automated coverage proves the device-only implementation:

- A detector crossing sends a correlated request without opening the microphone
  gate, and only a fresh matching grant opens the bounded turn stream.
- Denial, request timeout, mute recheck, stale decisions, and disconnect cannot
  start a late turn.
- A request-capable device is handled without controller idle microphone frames.
- HACS attaches audio and accepts before the controller sends a device grant.
- Ordinary busy requests are denied rather than queued, and fleet arbitration
  grants one request while explicitly denying losers.
- Local scoring remains active during granted button/wake streams and playback.
- Device-originated barge-in emits targeted cancellation for the exact active HA
  turn before replacement admission.
- Turn preroll is contiguous and anchored to the local activation sequence.
- Capture selection keeps the peak near miss, preserves safe incomplete suffixes,
  bounds and prioritizes the queue, and retries unacknowledged uploads.
- Live STT pauses capture upload after the current chunk, and capture disable
  invalidates retained upload tokens and PCM.
- Partial, malformed, oversized, out-of-order, duration-inconsistent, or
  digest-mismatched uploads produce no acknowledged capture.
- Exact duplicate capture IDs produce one stored capture and a repeat
  acknowledgement; conflicting duplicates receive no acknowledgement.
- Current privacy, model, checksum, connection identity, and durable commit are
  checked before acknowledgement.
- Metadata sidecars follow labeling, pruning, deletion, and export, and support
  bundles continue to exclude training audio and metadata paths.

The controller and firmware now additionally assert:

- The controller does not import or construct OpenWakeWord at runtime.
- Firmware without `wake_request_v1` is marked incompatible rather than given a
  controller wake listener.
- The UI/configuration contains no detector-location modes, controller
  Speex/GPU controls, comparison surfaces, or fallback language.

Single-device hardware verification remains pending on the available `study`
device for ordinary wakes, button turns, mute races, missing models, controller
and HA outages, reconnects, accepted and denied captures, TTS playback, and
device-only barge-in. No second physical Echo is available, so multi-device
arbitration remains automated-test-only residual risk rather than a hardware
release gate. The manual hardware checklist follows.

## Single-Device Manual Hardware Test Plan

Status: automated verification complete; manual single-device validation started
2026-09-02 with an operator present with Study. 1.1, 2.1, 2.2, 2.3, 5.1, 5.2,
5.3, and 6.1 passed. 6.2 failed: the threshold-raise step never reached the
device (see the item's notes), so the test did not run as specified; stopword
captures are additionally not a device-side feature yet. TLS capture
validation is also blocked: the deployed controller has `SERVER_TLS_PORT=0` and
`REQUIRE_DEVICE_TLS=0`; do not change that link configuration solely for this
check.

5.1/5.2 took four fix-and-retest rounds to reach PASS, all against the same
symptom category ("barge-in doesn't work") with four unrelated root causes,
each masking the next once fixed:

1. **The deployed HA HACS integration was a stale copy with no `turn.cancel`
   handler.** The controller's cancellation broadcast was silently ignored, so
   HA's Assist pipeline kept running and `_active_turn_id` never cleared,
   causing the replacement turn's `wake.offer` to be rejected. Fixed by
   deploying the repo's current `assist_satellite.py` (turn-ownership tokens,
   `turn.cancel` handling, an offer lock) and restarting Home Assistant.
2. **`bargeInThreshold` was misconfigured at `0.3`** — the same as the normal
   wake threshold, and six times the documented safe default (`0.05`).
   Speech spoken over TTS playback is acoustically depressed by the louder
   echo at the mic (documented at ~0.10–0.12), so a real barge attempt could
   not cross `0.3` at all; the device was not declining to act on a crossing,
   it was never scoring one. Fixed by resetting it to `0.05`.
3. **`em_controller.py`'s dispatch loop denied every barge attempt as `"busy"`
   before it ever reached the barge-aware code three lines later.**
   `device.wake_request_id` stays set for an admitted turn's entire active
   duration, not just the admission handshake, so it is non-`None` for
   exactly the window a barge attempt arrives in — the ordinary busy gate ran
   first and denied it unconditionally, with no existing test catching it
   because every prior `_handle_wake_request` test pre-set
   `wake_request_id` to match, which is what this ordering bug prevented from
   happening for real. Fixed by reordering the checks
   (`_wake_request_admission_gate`, with regression tests).
4. **The replacement turn's `admission_valid` closure inherited the ORIGINAL
   wake's already-expired deadline.** `_run_voice_locked`'s barge-restart
   branch reused whichever `admission_valid` the function was first called
   with; for a barge admitted well into an active turn, that deadline
   belonged to the original wake and had long since passed, so the
   replacement turn's freshness check failed the instant HA accepted its
   offer. From the outside this read as "barge-in stops TTS but never opens
   the follow-up turn — it just ends the voice session," with the actual
   cause (a stale timestamp check) invisible in the symptom. Fixed by giving
   the barge-restart branch a fresh deadline (`_make_admission_valid`, with a
   regression test).

A fifth, unrelated bug surfaced along the way during a mid-testing controller
restart: **`device.stop_generation` reset to 0 on every controller restart**,
while the device's own `stopword.Manager` generation counter is long-lived
across control-plane reconnects — so a restarted controller sent generation
numbers the device had already passed, and every `stop_arm` was rejected as
`"invalid arm"` until the controller's counter organically climbed back above
what the device remembered. Fixed by seeding from wall-clock time instead of 0.

### Scope

This checklist uses the only available Echo, dashboard label `study`. Assume the
latest controller container and latest device firmware are already deployed.
Study is connected and approved, Home Assistant and the EchoMuse integration are
running, and the headset jack is unplugged.

No shell commands, packet captures, fault-injection scripts, or additional Echo
devices are required. Use the dashboard, Home Assistant UI, and normal spoken
queries only.

This validates real-room behavior. Exact wire ordering, idle-PCM suppression,
malformed upload rejection, duplicate ACK behavior, and multi-device arbitration
remain automated-test coverage because one device and spoken queries cannot prove
them.

### Before Starting

Set the effective Study configuration:

| Setting | Value |
|---|---|
| On-device wake word | `On` |
| Wake model and threshold | Normal known-working values |
| Barge-in | On |
| Wake captures | Off initially |
| Button single tap event | Off |

Confirm Study is connected and unmuted, its Link row shows the expected link
(preferably `wss (TLS)`), and one ordinary wake query creates one completed
Activity row. Stop if this baseline fails.

For every test record `PASS`, `FAIL`, or `BLOCKED`. A retry can confirm a pass
but cannot erase a failure. Note the Activity outcome and optionally record the
ring/audio behavior on a phone.

### Step 1: Device Readiness

#### 1.1 Normal On-Device Wake

1. Say the wake phrase followed by three ordinary requests, waiting for each
   response to finish. Examples: “what time is it?” and “what is the weather?”.

Pass:

- Study starts listening only after the wake phrase.
- Each request creates exactly one response and one completed Activity row.
- No button press, reboot, or controller restart is needed.

Fail: no wake response, duplicate response, or a stuck listening/thinking state.

#### 1.2 Missing-Model Limitation

Do not deliberately create a missing classifier for this manual checklist. The
dashboard normally installs or holds back model changes, while forcing a missing
file requires shell intervention. Record this item as `AUTOMATED ONLY`; the
fail-closed behavior is covered by automated tests.

### Step 2: Admission, Button, and Mute

#### 2.1 Wake Starts Only When Expected

1. Say normal room speech or a near-match without the wake phrase.
2. Wait five seconds, then say the real wake phrase followed by “what time is
   it?”. Repeat three times.

Pass: non-wake speech produces no listening animation or response; each real
wake produces one turn and one answer.

Fail: a response starts without a wake phrase, starts later without a new wake,
or one wake creates multiple turns.

#### 2.2 Button Turn

1. Tap the Dot action button, then say “what time is it?” once listening starts.
2. Repeat three times, then hold the button for about one second.

Pass: each tap starts one listening turn and answer; the hold does not start an
ordinary voice turn; Activity shows the expected number of button turns.

#### 2.3 Mute

1. Press physical mute so the ring is red.
2. Say the wake phrase three times and tap the action button once.
3. Unmute and make one ordinary wake query.

Pass: muted wake/button attempts start no voice turn; red mute remains visible;
the first wake after unmuting works.

Fail: muted speech starts listening/answers, mute clears unexpectedly, or wake
requires a restart after unmuting.

### Step 3: Home Assistant Admission Failure

1. Disable the EchoMuse integration in Home Assistant.
2. Say the wake phrase twice.
3. Re-enable the integration, wait for Study to reconnect, then make one normal
   wake query.

Pass: failed attempts do not remain stuck listening or produce a delayed answer;
the first recovery wake works without restarting controller or device; any failed
Activity rows are terminal.

### Step 4: Busy Turn Behavior

1. In the dashboard, turn Barge-in off.
2. Ask for a long response, for example “tell me a long story about the solar
   system”.
3. While Study is speaking, say the wake phrase and a short query.
4. Wait for the original response to finish, then remain silent ten seconds.
5. Restore Barge-in on.

Pass: original speech continues; the second request does not produce an answer
during or after the original response.

Fail: the second request is queued and speaks later, or the original response is
interrupted while Barge-in is off.

#### Multi-Device Arbitration Limitation

Only Study is available. Do not claim physical validation of multi-device
first-request-wins behavior. The release note must state:

> Multi-device wake arbitration was not field-tested because only one physical
> Echo was available. Automated controller arbitration tests passed.

### Step 5: Device-Originated Barge-In

Turn Barge-in back on. Run each test three times.

#### 5.1 Interrupt a Spoken Response

1. Ask for a long response.
2. While it is speaking, say the wake phrase followed by “what time is it?”.

Pass: old speech stops promptly; Study answers the replacement request; Activity
shows one barged/cancelled turn and one replacement turn.

Fail: both responses play, the new request is ignored, or a duplicate response
appears later.

#### 5.2 Interrupt Thinking

If Home Assistant has a slow automation or deliberate slow Assist action, start
it with a query. While Study shows thinking but before it speaks, issue a new wake
phrase and short query.

Pass: the original request produces no late response and the replacement
completes normally. If no slow action exists, record `BLOCKED`; do not invent a
timing-sensitive substitute.

#### 5.3 Stopword Interrupt

Not part of the original checklist numbering; added 2026-09-02 after manual
testing surfaced it as a distinct failure from 5.1/5.2. See "Barge-In vs. the
Stopword" above for why this is expected to behave differently from the wake
phrase, not identically.

1. Ask for a long response.
2. While it is speaking, say "stop".

Pass: speech stops promptly; no replacement turn opens; Study returns to idle
listening for the wake phrase; Activity shows one stopped/cancelled turn and no
second turn.

Fail: speech does not stop, or a replacement turn opens (i.e. it behaves like
5.1 instead of stopping outright).

Result 2026-09-02: **PASS** — after two fix-and-retest rounds against two
unrelated root causes:

1. **Controller: the accepted branch of the `stop_detected` handler counted
   the stop with `loop.run_in_executor(None, db.bump_wake_counters, device_id,
   stops_accepted=1)` — and `run_in_executor` takes POSITIONAL args only.**
   The kwargs call raised `TypeError` inside the control-plane dispatch loop;
   the exception was swallowed by `handle_control`'s blanket handler, which
   then tore the device's control and data connections down. On hardware this
   presented as "stop does nothing," the device disconnecting, and it
   reconnecting a few seconds later with the response still playing. All
   three counter bumps in the handler are now positional, with a regression
   test that drives the real dispatch loop end to end
   (`test_stop_detected_cancels_turn_without_killing_the_handler`).
2. **Device: the stopword crossing handler painted a green pulse anim the
   moment "stop" was detected** — between the word and the audio actually
   stopping, over a ring the TTS meter already owned. Removed; the local
   speaker flush stays, so silence timing is unchanged. Flashed as
   firmware 20260902-1611-dev via adb (md5-verified inactive-slot write +
   symlink flip, the same procedure the controller's OTA performs).

### Step 6: Wake Training Captures

Wake captures contain recognizable speech. Keep them private and delete test
clips afterward unless intentionally retaining them for training.

#### 6.1 Accepted Wake Capture

1. Enable `Save wake captures` for Study.
2. Make one normal wake query.
3. Open Settings > Training after the response ends.

Pass: one activation capture appears for the selected model, plays as a valid
short clip containing the wake phrase, and has one corresponding completed turn.

Fail: no capture, multiple captures for one wake, empty/corrupt clip, or wrong
wake model.

#### 6.2 Near-Miss Capture

1. Temporarily raise wake threshold so a quieter wake phrase does not activate.
2. Say the quieter phrase twice within about one second; make the second clearer
   but still below the raised threshold.
3. Wait five seconds and inspect Settings > Training.
4. Restore the normal threshold.

Pass: no voice turn starts; exactly one near-miss capture appears and contains
the clearer phrase. If neither phrase creates a near miss, record `BLOCKED`.

Result 2026-09-02: **FAIL**, with the cause identified rather than a code fault
suspected. No near-miss capture appeared for either the wake model or the
stopword, and the two halves have different explanations:

1. **Wake model: the threshold was never actually raised.** The controller
   logged no config save and no config push after the 16:20 connect, and the
   device's own log shows `local detection ready ... threshold 0.30` at every
   config apply all day. With the threshold still at 0.30 the test was run by
   speaking more quietly instead — and no utterance produced a turn (so
   everything scored < 0.30) yet no capture appeared either, which places the
   scores at or below `wakeNearMissFloor` (0.03), where `Observe` discards
   candidates **silently**. Historical near misses cluster at 0.031–0.22
   (the score is embedded in the capture filename), so sub-floor is a hair
   away from working values and there is no telemetry distinguishing "below
   floor" from "broken". Redo the test by raising the threshold to ~0.85 and
   speaking the phrase at NORMAL volume — measured phrase scores today were
   0.42–0.75, which lands them inside (floor, threshold) where near misses
   are selected. (Near misses demonstrably work: one was captured and labeled
   at 16:12, minutes before this test, on the previous firmware.)
2. **Stopword: capture for the stopword model is not implemented on the
   device at all.** `ScoreEvent`s reach the capture manager only from the wake
   scorer (`SetScoreCallback(ObserveWakeScore)`); the stop head's crossings
   call `HandleStopCrossing`, which has no capture hook, so no `stop/` bucket
   can ever appear in Training. If stopword training captures are wanted, that
   is a new device-side feature (observe stop-head scores, key captures by the
   `stop` stem), not a fault in the wake-capture path.

#### 6.3 Capture Privacy Disable

1. With captures enabled, start a wake request and speak for several seconds.
2. While Study is still listening, turn `Save wake captures` off.
3. Finish the query, wait one minute, and check Settings > Training.
4. Make one more normal wake query while captures remain off.

Pass: no capture from the privacy-disable query or subsequent query appears;
Study remains connected and the next ordinary wake works.

Fail: a post-disable capture appears, Study remains disconnected/stuck, or wake
stops working.

#### 6.4 TLS Capture Check

1. Confirm Study Link shows `wss (TLS)`.
2. Enable captures, make one ordinary wake query, and confirm one activation
   capture appears.
3. Turn captures off again.

Pass: capture works over TLS, link stays TLS, and no duplicate capture appears.

### Cleanup

1. Restore normal wake threshold and Barge-in preference.
2. Leave `Save wake captures` in the preferred production state, normally off.
3. Delete test clips from Settings > Training unless intentionally retained.
4. Ensure the Home Assistant integration is enabled.
5. Confirm Study is unmuted, connected, on TLS if normal for the deployment, and
   answers one final ordinary wake query.

### Result Summary

| Test | Result | Notes |
|---|---|---|
| 1.1 Normal on-device wake | PASS | |
| 1.2 Missing-model fail-closed behavior | AUTOMATED ONLY | |
| 2.1 Wake starts only when expected | PASS | |
| 2.2 Button turn | PASS | |
| 2.3 Mute | PASS | |
| 3 Home Assistant admission failure | PASS / FAIL / BLOCKED | |
| 4 Busy turn, Barge-in off | PASS / FAIL / BLOCKED | |
| 5.1 Playback barge-in (wake phrase) | PASS | Took four fix-and-retest rounds; see the status note above for all four root causes. |
| 5.2 Thinking barge-in (wake phrase) | PASS | |
| 5.3 Stopword interrupt ("stop") | PASS | Took two fix-and-retest rounds: a controller-side `run_in_executor` kwargs TypeError that crashed the control handler on every accepted stop, and a device-side green LED pulse painted between the word and the silence. See the item's own notes for both root causes. |
| 6.1 Accepted capture | PASS | |
| 6.2 Near-miss capture | FAIL | The threshold was never raised (no config push; device stayed at 0.30 all day), so the quieter-phrase attempt scored at or below the 0.03 near-miss floor and was silently discarded. Stopword captures are not implemented device-side at all. See the item's notes for the redo procedure and the open feature decision. |
| 6.3 Privacy disable | PASS / FAIL / BLOCKED | |
| 6.4 TLS capture | PASS / FAIL / BLOCKED | |
| Multi-device arbitration | NOT HARDWARE TESTED | Automated tests only |

Overall single-device result: `PASS / FAIL / BLOCKED`
