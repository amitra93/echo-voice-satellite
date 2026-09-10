# Timer Implementation Audit

**Status: all five implementation phases are implemented; hardware acceptance
remains.**

This audit reviews the implemented timer path from Home Assistant lifecycle
events through the controller's alarm worker and timer card to the device's
speaker and ring. It follows the speaking-state incident in which cancelling a
buffered timer chime skipped a playback cleanup path.

## Findings

### P0 - Alarm correctness

1. **The abandoned RMS/STT-only dismissal path is not connected to live
   microphone input.** `_run_timer_speech_turn()` and its
   `timer_alarm_audio_ready`, `timer_alarm_listen_after`, and
   `alarm_should_capture()` plumbing have no production caller. The live
   timer dismissal path is instead the device stop word: `_timer_state()` arms
   it and `stop_detected` calls `dismiss_timer_alarm()`. That only works for a
   device with the `stopword` capability and a ready model. The selected
   capability-only direction makes that the supported contract and retires the
   unused RMS/STT path. (`controller/em_controller.py`, `em_timers.py`.)

2. **A failed alarm delivery can leave `timer_firing` true.**
   `TimerAlarmRunner.start()` asynchronously signals `on_state(..., True)`
   before loading the sound. Its no-device, unreadable-sound, and empty-audio
   paths return before the `on_state(..., False)` cleanup in `_run()`'s
   `finally`. The alarm is discarded, but the device can retain its alarm LED
   and stop-arm presentation. (`controller/em_timer_alarm.py`.)

3. **Queued alarms share one timeout budget.**
   `_run()` creates `deadline` once before looping over `session.current`.
   Timing out the first alarm promotes the next one without resetting that
   deadline, so every later queued alarm receives only one burst before being
   timed out. Each alarm needs its own `MAX_RING_S` window.

4. **Idle-ring restoration can overwrite a live alarm pulse.**
   A `started`, `updated`, or `cancelled` event calls `leds_idle()` whenever
   the device is not in a voice state. `leds_idle()` does not check
   `timer_firing`, so it can replace the amber alarm pulse with a countdown or
   black while a different timer is still ringing. The delayed post-turn
   outcome cleanup has the same flaw. (`controller/em_api.py`,
   `em_controller.py`.)

5. **Offline expiry is neither discarded nor delivered.**
   The controller creates a session and publishes a ringing `timer.alarm`
   snapshot before checking whether the Echo is live. It then cannot start an
   alarm. The card can show a phantom ringing row, and a later expiry can cause
   the stale timer to ring first. This contradicts the established
   best-effort/offline-discard timer policy. (`controller/em_api.py`.)

### P1 - Lifecycle and recovery

6. **HACS forwards lifecycle events concurrently from mutable timer objects.**
   The synchronous TimerManager callback creates an independent task for each
   event. A fast `started` then `finished` sequence can reach the controller
   in reverse order; the late `started` removes the newly created alarm. The
   async task also reads the mutable `TimerInfo` after scheduling, so it can
   send fields belonging to a later event. (`hacs/.../assist_satellite.py`.)

7. **A HACS reload or control-stream reconnect loses ringing/queued rows.**
   `AlarmPresence` is in memory and only receives future `timer.alarm` events.
   The controller's initial event snapshot does not include alarm sessions.
   The physical alarm can continue after reload while the card no longer shows
   it or offers Dismiss. (`hacs/.../__init__.py`, `timer_card.py`.)

8. **Control disconnect can leave buffered chime audio audible.**
   The controller removes the device from `_devices` before asking the alarm
   runner to stop. `TimerAlarmRunner.stop()` can no longer find the device to
   send `speaker_flush`, so a chime already in the device buffer can continue
   after the controller considers the alert stopped. (`em_controller.py`,
   `em_timer_alarm.py`.)

9. **Timer forwarding tasks survive integration unload.**
   The timer event tasks are neither retained nor cancelled when the satellite
   is removed. A late task can create a fresh HTTP session after the integration
   client was closed and send stale state. (`hacs/.../assist_satellite.py`.)

### P2 - UI and rendering

10. **The Lovelace card does not resubscribe after DOM reattachment.**
    It unsubscribes in `disconnectedCallback`, but subscribes only on its first
    `hass` assignment. Returning to a dashboard can leave the same card element
    permanently stale. (`hacs/www/echo-voice-timers-card.js`.)

11. **Timer-card manager updates select an arbitrary hub in a multi-controller
    installation.** `_hub_for()` returns the first hub instead of the hub for
    the satellite's config entry. Events for another controller can update the
    wrong card. (`hacs/.../timer_card.py`.)

12. **A countdown frame may repaint once after a newer animation starts.**
    `runCountdown()` checks its generation before calling `SetLEDs`, leaving a
    small check-to-paint window. For the one-second countdown ticker, that stale
    frame can hold until the next tick. The same pattern exists in the faster
    animation loops, but it is more visible for countdown. (`device/internal/
    server/animator.go`.)

## Implementation Options

### A. Alarm correctness batch

1. **Narrow controller repair (recommended).**
   Keep the existing timer contract and fix P0 controller defects together:
   make alarm state cleanup unconditional, reset the deadline per promoted
   alarm, preserve the pulse while `timer_firing`, discard offline expiries,
   and flush before removing a disconnecting device. Add focused unit and
   branch tests for every transition.

2. **End-to-end reliability batch (selected).**
   Include the narrow controller repair plus the HACS event-ordering and alarm
   recovery work in one change. This avoids fixing controller state while HACS
   can still reorder or lose it, but is a larger review and test surface.

### B. Spoken alarm dismissal

1. **Controller RMS gate into STT-only confirmation (recommended).**
   During the quiet gap between chime bursts, use the existing
   `alarm_should_capture()` threshold and settle time to start
   `_run_timer_speech_turn()`. A non-empty transcript dismisses; false starts
   remain silent. This is the behaviour the timer design currently promises
   and works on devices without a local stop-word capability.

2. **On-device stop word only (selected).**
   Arm the existing stop-word detector for timer alarms and dismiss directly
   on its match. It is faster and avoids an HA STT round trip, but makes spoken
   dismissal capability-dependent and gives no fallback to older firmware.

3. **Hybrid.**
   Use the device stop word where available, with the controller RMS/STT path
   as the fallback. This offers the broadest compatibility but has two input
   paths to arbitrate and test against the same alarm.

### C. HACS lifecycle ordering

1. **Immutable per-satellite FIFO forwarder (selected).**
   Snapshot the payload synchronously in `_timer_event`, append it to a
   per-satellite queue, and have one worker post events in order. Cancel and
   drain that worker during unload. This requires no controller protocol
   change.

2. **Controller sequence enforcement.**
   Add a per-device sequence number to the HACS payload and have the controller
   reject stale events. This protects against network reordering even beyond
   the current integration, but expands the wire contract and requires careful
   reconnect semantics.

### D. Alarm-card recovery

1. **Authenticated alarm snapshot endpoint (recommended).**
   Add a controller read endpoint returning a device's current/queued alarm
   presentation. HACS fetches it after its control stream connects and feeds
   `AlarmPresence` before the card first renders.

2. **Extend the existing event-stream snapshot (selected).**
   Include alarm sessions in the controller's initial snapshot event. This
   avoids another request, but makes the general device snapshot carry
   timer-specific presentation data for every integration consumer.

### E. Card and renderer maintenance

1. **Scoped HACS/UI repair (selected).**
   Key timer-card hubs by config entry, pass the right hub to each satellite,
   and subscribe on every DOM connection. Add a deterministic countdown
   generation handoff test before deciding whether the device locking needs a
   change.

2. **Broader card coordinator.**
   Replace the per-entry hub lookup with one fleet-level timer coordinator that
   owns all card subscriptions. This can simplify a multi-controller UI but is
   more invasive and is not needed to correct the present routing defect.

## Test Requirements

- Alarm load/decode/device failure always retires `timer_firing`, ring state,
  stop arm, and synthetic timer-alert state.
- Two queued alarms each receive an independent `MAX_RING_S` window.
- Timer updates and delayed turn cleanup never replace a live alarm pulse.
- Offline expiry is discarded without a card-visible ringing row.
- A ready stop-word device dismisses an alarm locally with normal barge-in
  disabled and produces no intent or TTS response; an unsupported device never
  advertises a spoken-dismissal path it cannot honour.
- Rapid started/updated/finished delivery remains ordered across an artificial
  HTTP delay, and unloading cancels pending delivery.
- Reloading HACS while an alarm rings restores the card's ringing/queued rows
  and Dismiss action.
- Card reattachment resubscribes, and two controller entries update only their
  own hubs.

## Selected Implementation Plan

### Scope and invariants

This is the selected **end-to-end reliability batch**. It fixes every finding
in this audit without creating a second timer store, schema migration, or new
device capability. Home Assistant remains authoritative for durable timer
records; controller sessions and HACS `AlarmPresence` remain presentation and
physical-alarm state only.

The selected spoken-dismissal contract is **device stop word only**. A timer
alarm arms the existing `stopword` capability only when the device reports a
ready model. Devices without that capability or model retain action-button and
card dismissal, but do not claim to support spoken alarm dismissal. The
unwired RMS/STT-only experiment is not revived as a fallback.

### Phase 1 - Alarm-runner state machine

**Files:** `controller/em_timer_alarm.py`, `controller/em_api.py`,
`controller/em_controller.py`, `controller/tests/test_timer_alarm.py`,
`controller/tests/test_api_controller_branches.py`,
`controller/tests/test_controller_device.py`.

1. Move the firing-state transition from `TimerAlarmRunner.start()` into
   `_run()` after device and sound preflight succeeds. Await the transition
   rather than fire-and-forget it, then put its matching `on_state(..., False)`
   in the same `finally` that resumes interrupted music. Preflight failures
   discard the session's transient alarm queue and publish its empty snapshot,
   but never arm LEDs, stop-word state, or `timer_firing`.
2. Give each promoted `session.current` timer an independent deadline. Track
   the current timer id; when it changes after `timeout_current()` or an
   explicit dismissal, start a new `loop.time() + MAX_RING_S` window before
   playing its first burst.
3. Preserve the existing best-effort offline policy at the ingress boundary.
   When a `finished` event names no live device, immediately apply the
   delivery-failure transition and publish the resulting empty alarm snapshot;
   never leave an undeliverable item in `current` or the FIFO queue.
4. Pass the disconnecting `Device` object into alarm shutdown, or invoke timer
   shutdown before removing that object from `_devices`. Send `speaker_flush`
   while the control plane is still usable, then remove the registry entry.
   Preserve the existing stale-connection guard so an old socket cannot flush a
   replacement connection.
5. Keep the current stop-word timer phase. Arm it only when
   `stopword_capable && stop_model_ready`, and renew the arm when a queued
   alarm becomes current; on a matching `stop_detected`, dismiss the current
   alarm through the existing shared path. Remove the unused RMS/STT-only
   helper and timer-audio gate fields, then update the timer documentation and
   tests to describe this capability-gated behaviour.

**Tests:**

- Missing sound, ffmpeg failure, empty PCM, and disappeared device leave no
  active runner, no `timer_firing`, no timer stop arm, and an empty presentation
  snapshot.
- Two queued alarms that each reach `MAX_RING_S` both receive their full,
  independently measured window.
- Offline expiry emits no ringing row and cannot ring after reconnect.
- Control disconnect sends `speaker_flush` before registry removal; stale
  control disconnect does not affect the replacement device.
- A ready stop-word device dismisses a timer through `stop_detected`; a device
  without a ready stop-word model does not arm it and exposes no unsupported
  spoken-dismissal promise.

### Phase 2 - Ring priority and countdown handoff

**Files:** `controller/em_controller.py`, `controller/em_api.py`,
`device/internal/server/animator.go`, `controller/tests/test_controller_device.py`,
`controller/tests/test_api_controller_branches.py`,
`device/internal/server/animator_test.go`.

1. Make `leds_idle()` a no-op while `device.timer_firing` is true. The alarm
   state callback remains the sole owner of the amber pulse until it clears
   `timer_firing` and explicitly returns the ring to idle.
2. Retain delayed outcome-cue restoration, but route it through that same
   guard. A timer that fires during the one-second delay keeps its pulse; an
   alarm that ends before the delay expires receives the ordinary idle/countdown
   restoration.
3. Add a `paintIfCurrent` animator helper that checks a generation and paints
   while holding the animation generation lock. Use it for countdown frames and
   expiry clearing so a newer `StartAnim` cannot be overwritten by a stale
   one-second countdown frame. Keep `SetLEDs` as the single path through
   mute/volume suppression and `baseLEDs` recording.

**Tests:**

- Started, updated, cancelled, reconnect, and delayed turn-end cleanup do not
  replace a live alarm pulse.
- Alarm end restores the first eligible countdown or off state exactly once.
- A forced countdown-versus-listening interleaving cannot paint the countdown
  after the listening generation becomes current.

### Phase 3 - Ordered HACS lifecycle forwarding

**Files:** `hacs/custom_components/echo_voice_satellite/assist_satellite.py`,
`hacs/tests/test_assist_satellite.py`.

1. Build an immutable plain-dict payload synchronously inside `_timer_event()`
   before `TimerInfo` can mutate. It contains the event name, timer id, HA
   device id, name, original duration, remaining seconds, and active state.
2. Add one FIFO queue and one forwarding worker per `EchoAssistSatellite`.
   The worker sends exactly one payload at a time through `async_timer_event`;
   it keeps the current delivery ordering and logs a failed delivery using the
   existing no-replay posture rather than spawning a competing retry path.
3. Retain the worker task on the satellite. On unload, stop accepting new
   payloads, cancel the worker, and await it before closing the controller
   client. No task may recreate an HTTP session after unload.
4. Stop looking up an arbitrary timer-card hub. Inject or resolve the hub for
   this satellite's own config entry, so manager changes notify only that
   controller's subscribers.

**Tests:**

- Artificially delay the first HTTP post; `started`, `updated`, and `finished`
  still arrive in order with their original, immutable values.
- Unloading with a queued or in-flight payload cancels the worker and performs
  no post-unload request.
- Two config entries route manager updates to their own timer-card hubs only.

### Phase 4 - Event-snapshot alarm recovery

**Files:** `controller/em_api.py`, controller event-WebSocket tests,
`hacs/.../client.py`, `hacs/.../coordinator.py`, `hacs/.../__init__.py`,
`hacs/tests/test_client.py`, `hacs/tests/test_coordinator.py`,
`hacs/tests/test_init.py`, `hacs/tests/test_timer_card_hub.py`.

1. Extend the initial `/api/events` `snapshot` payload with an additive
   `timer_alarms` list. Each entry is `{device_id, current, queue}` from an
   `AlarmSession` with a current item or queued items. Do not include ordinary
   running timers: the timer card already obtains those from Home Assistant's
   `TimerManager`.
2. Register the HACS timer-alarm listener before opening the control stream.
   Teach it to hydrate `AlarmPresence` from `snapshot.timer_alarms` and to
   continue applying ordinary `timer.alarm` updates. This closes the gap where
   an alarm event arrives between snapshot receipt and listener registration.
3. Keep the field optional. Older controllers send a normal device-only
   snapshot; HACS starts with an empty `AlarmPresence` and continues receiving
   future events. Unknown snapshot fields remain ignored by existing clients.

**Tests:**

- A reconnect/reload while an alarm is ringing restores ringing and queued rows
  before the timer card subscribes; Dismiss targets the correct EchoMuse device.
- Empty `timer_alarms` retires an old presence row.
- A legacy snapshot without `timer_alarms` remains compatible.

### Phase 5 - Card lifecycle and release validation

**Files:** `hacs/www/echo-voice-timers-card.js`,
`hacs/tests/test_timer_card.py`, `hacs/tests/test_timer_card_hub.py`,
`docs/audio-states.md`, `docs/led-ring-states.md`, `docs/timer-validation.md`.

1. Factor card subscription into an idempotent method. Call it whenever both
   `hass` and a live element connection exist; `disconnectedCallback()` calls
   and clears the unsubscribe reference. Preserve the redraw interval's sole
   job of locally rendering known countdown text.
2. Add browser-level or lifecycle-shaped tests proving disconnect then reconnect
   creates exactly one new subscription and receives later snapshots.
3. Update user and design documentation: stop-word-only alarm dismissal is
   capability-gated; offline expiries are discarded; individual queued alarms
   each ring for up to `MAX_RING_S`; snapshots restore card alarm presentation.
4. Run controller, HACS, device, and integration-focused timer suites. Perform
   hardware acceptance on a stop-word-capable device and a device without that
   capability, including alarm queue timeout, HACS reload mid-alarm, controller
   reconnect, action-button dismissal, and stop-word dismissal.

## Delivery Order

Implement and merge Phase 1 before Phase 2: alarm state must be trustworthy
before the ring can safely defer to it. Phase 3 and Phase 4 can proceed in
parallel after their snapshot contract is agreed, then Phase 5 integrates the
card and documentation. Run the complete controller and HACS suites after all
phases, because timer state crosses both processes.
