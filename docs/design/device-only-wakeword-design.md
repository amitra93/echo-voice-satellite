# Device-Only Wake Word Design

Status: accepted design; steps 1-5 complete, step 6 in progress

## Goal

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
4. Only after a grant does the device light the listening ring and begin sending
   audio for STT.

Physical action-button turns use the same admission sequence. Wakeword-based
barge-in is also detected only on the device.

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
- Opted-in wake captures are uploaded for every detector activation, including
  requests denied because of arbitration, a busy device, or backend failure.
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
or capture upload.

The device reports detector readiness whenever configuration or assets change:

```json
{
  "type": "wake_status",
  "model": "hey_jarvis",
  "classifierMd5": "...",
  "ready": true,
  "error": null
}
```

The controller treats the device as unavailable for wake turns until the status
matches the selected model and reports `ready: true`.

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

Button releases carry the same correlated `requestId` on their existing
`button` message. The controller classifies the gesture before admitting a tap,
so a hold or timer-dismiss tap cannot race a separate request into a voice turn.
Once classified as a voice turn, it follows the same grant/deny/start boundary.

The request ID combines a per-process random nonce with a monotonic counter. A
16-bit audio sequence alone is not a safe request identifier because it wraps
and resets when a stream restarts.

### Admission

Before granting, the controller verifies that:

- The request belongs to the current control connection.
- The device is approved, connected, unmuted, and data-ready.
- The matching detector model is ready for wakeword requests.
- No ordinary turn or admission reservation already owns the device.
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

The device starts its listening animation only after receiving a matching,
non-expired grant and rechecking local mute and readiness state. It ignores
duplicate, stale, and mismatched decisions.

After lighting the ring and starting the bounded audio stream, the device sends:

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

The Amazon AFE capture remains open continuously. Its 16 kHz mono PCM is fed to
the local detector and a bounded RAM ring. Idle PCM is not sent to the
controller.

The ring preserves 80 ms frame boundaries and sequence numbers and retains up
to eight seconds: two seconds of preroll plus the four-second request lifetime
and margin. It is cleared on mute, detector reset, model replacement,
capture disable, stream discontinuity, sequence gap, or audio-format change.

Detector inference remains off the microphone goroutine. A full inference queue
drops frames and reports health counters rather than blocking capture.

### Granted Turn Stream

After a grant, the device starts a bounded turn stream over `/data`:

1. Send contiguous local preroll ending at the detector activation sequence.
2. Continue with live PCM frames.
3. Stop on endpoint, cancellation, mute, disconnect, or turn timeout.

The stream is the only ordinary microphone audio sent to the controller. The
controller forwards it to the already-accepted Home Assistant turn.

Local wake scoring remains active during all bounded streams, including button
turns, so barge-in never requires controller inference.

## Home Assistant Admission

The existing turn `accept` and `reject` routes become functional rather than
being no-ops. Turn startup is split into preparation and execution:

1. The controller reserves the device and creates a provisional turn.
2. The controller broadcasts `wake.offer`.
3. HACS attaches the turn audio WebSocket.
4. HACS posts `accept`, or posts `reject` when it cannot own the turn.
5. The controller sends the device grant only after acceptance.
6. The device starts its LED and turn audio.
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

For a fresh wake request during an active turn, the controller grants only when
barge-in is enabled and the current phase permits it. After admission it:

- Cancels the exact active Home Assistant turn.
- Flushes speaker playback when needed.
- Ends the old turn as barged.
- Accepts the replacement bounded audio stream and its local preroll.

The existing Home Assistant cancellation gap must be fixed so cancellation
reaches the running Assist pipeline rather than only the EchoMuse turn object.

## Wake Training Captures

Training capture remains opt-in and writes recognizable speech. Capture
selection moves to the device because the controller no longer receives idle
audio or scores it.

### Selection

For trusted local detector scores:

- A score at or above the effective threshold is an activation candidate.
- A score above `wakeNearMissFloor` and below the threshold is a near-miss
  candidate.
- Activation and near-miss candidates share a per-utterance debounce window.
- The device retains the peak-scoring near-miss within the debounced utterance
  and snapshots PCM ending at that frame.

Captures use the configured `wakeCaptureSec`, with a five-second hard maximum
and one complete 80 ms frame as the minimum. A sequence gap produces only the
safe contiguous suffix and marks the capture incomplete.

Activation captures are retained whether the controller grants or denies the
associated session.

### Device Queue

Capture PCM is held only in bounded RAM and is never written to device flash.
The queue holds a small fixed number of complete captures. Activations outrank
near misses; when full, an activation may evict the oldest queued near miss.

Disabling capture synchronously clears the PCM ring, queued captures, retained
retries, and any incomplete upload. A reboot or power loss also loses queued
captures by design.

### Upload Timing

- Near misses upload while the device is idle and the data connection is
  available.
- An accepted activation capture is snapshotted immediately but uploads after
  the STT microphone phase ends.
- A denied activation capture uploads immediately after the denial because no
  STT stream will start.
- Live STT audio always has priority over capture traffic.
- Accepted activation captures have priority over near misses.
- A disconnected device retains captures within its bounded RAM queue and
  retries after reconnect.
- The device deletes a queued capture only after the controller acknowledges
  durable storage.

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

## Controller Removal Work

The controller removes:

- OpenWakeWord model construction and prediction.
- Continuous idle microphone ingestion and wake queues.
- Controller wake warm-up handling.
- Controller near-miss scoring.
- Controller/device score comparison and agreement rollups.
- Controller barge-in inference.
- Speex wake denoise.
- The `off`, `shadow`, and `on` wake-location modes.
- Controller wake GPU build and deployment options.

Generic persisted `wake_model`, `wake_score`, and `wake_threshold` fields remain
and become explicitly device-reported. Historical comparison columns remain in
deployed schemas but are not populated for new turns or presented as current
functionality.

The controller retains device model upload, asset distribution, install-before-
switch behavior, classifier reconciliation, turn handling, training storage,
and Forge export. Device provisioning assets must be separated from the Python
OpenWakeWord runtime before that runtime dependency is removed from the image.

## UI and Configuration

The dashboard removes the `Controller`, `Both (compare)`, and `On device` mode
selector, controller Speex denoise, detector-comparison displays, fallback
language, and shadow terminology.

It retains and presents:

- Device wake model and threshold.
- Detector readiness and selected-model match.
- Device inference health, errors, dropped frames, and inference latency.
- Asset installation and repair status.
- Device-side barge-in settings.
- Device-side wake capture settings and privacy warning.
- An explicit unsupported-firmware or detector-unavailable state.

Obsolete controller startup and stored settings are removed from current UI and
configuration surfaces, including `owwOnDevice`, `owwSpeexNs`, controller
`OWW_MODEL`, and controller `OWW_THRESHOLD`. Model, threshold, capture, barge-in,
and arbitration settings remain device configuration.

## Compatibility and Migration

The database migration list remains append-only. Existing detector-comparison
columns and old trigger labels remain readable for historical rows, but new
turns do not populate controller score fields.

The deployment order is firmware-first:

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
7. Remove current controller-inference code, dependencies, UI, comments, and
   current-architecture documentation.

There is no final controller-side compatibility detector.

## Implementation Plan

Ownership below names the component responsible for enforcing each decision;
cross-component changes are verified by controller integration tests and the
hardware release checklist.

| Step | Status | Owner | Work and decisions owned |
|---|---|---|---|
| 1 | Complete | Device firmware | Implement `wake_request_v1`, explicit detector readiness, request expiry, and unavailable-on-model-failure behavior. Owns mandatory new firmware and no detector fallback. |
| 2 | Complete | Device firmware | Add the bounded local PCM ring and grant-gated turn stream. Start the LED and send preroll/live PCM only after a matching grant. Apply the same boundary to button turns. |
| 3 | Complete | Controller and HACS | Add provisional turn preparation plus real `accept`/`reject` handling. Send a grant only after HACS attaches and accepts; deny on timeout or failure. |
| 4 | Complete | Controller | Add admission reservations, immediate first-request-wins arbitration, explicit loser denial, stale-connection protection, and cleanup on cancellation or disconnect. |
| 5 | Complete | Device firmware and controller | Move barge detection fully to the device, route active-turn wake requests through admission, and make controller cancellation reach the exact running HA pipeline. |
| 6 | In progress | Device firmware and controller capture storage | Select peak-score near misses, queue all opted-in activation captures including denied requests, and upload them after STT or while idle over the configured plaintext or TLS link. |
| 7 | Pending | Controller, dashboard, and packaging | Remove controller inference, comparison modes, Speex/GPU options, fallback behavior, and runtime dependencies while retaining device model asset distribution. Surface unsupported firmware and detector-unavailable states. |
| 8 | Pending | Documentation and release | Remove controller-detection claims from current code comments, UI, and current documentation while preserving historical changelogs, journals, and append-only migrations. Release firmware first, verify readiness fleet-wide, then release controller and HACS together. |

Implementation is complete only when hardware tests prove that no idle PCM
leaves the device, no LED or turn audio starts before HA-backed admission, and
the controller image contains no executable wake detector.

## Verification

Tests must prove at minimum:

- A detector crossing sends a request without lighting the ring or sending PCM.
- Only a fresh matching grant starts the LED and bounded turn stream.
- Denial, timeout, mute, and disconnect cannot start a late turn.
- No controller OpenWakeWord model is imported or constructed.
- The controller handles wake requests without idle microphone frames.
- HA acceptance precedes the device grant.
- Busy requests are denied instead of queued.
- Arbitration grants one request and explicitly denies losers.
- Local scoring remains active during button turns and playback.
- Barge-in cancels the exact active HA turn.
- Turn preroll begins at the correct local activation sequence.
- Capture selection uses the peak near-miss score within the debounce window.
- Live STT audio preempts capture uploads.
- Capture disable clears every retained PCM copy.
- Partial, malformed, oversized, or digest-mismatched uploads produce no WAV or
  acknowledgement.
- Duplicate capture IDs produce one stored capture and a repeat acknowledgement.
- Current privacy configuration is checked before accepting uploaded speech.
- Support bundles continue to exclude training audio and metadata paths.

Hardware verification must cover ordinary wakes, button turns, mute races,
multi-device arbitration, missing models, controller and HA outages, reconnects,
accepted and denied captures, TTS playback, and device-only barge-in.
