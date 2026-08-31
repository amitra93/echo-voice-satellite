# Timers Design

## Goal

Support multiple voice timers on EchoMuse devices. A user can create, name,
inspect, adjust, pause, resume, cancel, and dismiss timers through Home
Assistant Assist. Each finished timer notifies the EchoMuse device that created
it with a continuously repeating alarm sound. Finished timers are delivered in
order, including when several expire while an earlier alarm is still ringing.
Timers are also visible and manageable from a required EchoMuse Lovelace card
in Home Assistant.

## Decisions

- Home Assistant owns timer parsing, timer records, countdowns, and edits.
- The EchoMuse HACS integration registers each Echo as a Home Assistant timer
  device and forwards timer lifecycle events to the controller.
- The EchoMuse controller owns physical-device alarm playback, LEDs, queueing,
  and local dismissal.
- Timer creation records the originating Echo through Home Assistant's native
  device context. Selection, status, editing, and `cancel all` use Home
  Assistant's native timer-selection rules.
- Timer references are by name where possible. On an LLM-backed pipeline,
  ordinal requests such as `first` and `second` are best-effort selections from
  the timer-status result; duplicate timers can be ambiguous.
- A finished timer produces the Home Assistant Voice PE alarm sound continuously
  until the user says `stop`.
- The HACS integration ships an EchoMuse timer dashboard card. It displays and
  controls the complete dynamic timer list without creating fixed timer slots
  or duplicate `timer.*` entities.
- Finished timers form a FIFO queue. Dismissing one alarm advances to the next;
  it never silently discards another finished timer.
- An expiry while the originating Echo is privacy-muted is discarded locally.
- An expiry that cannot reach an offline Echo or controller is discarded
  locally and is not replayed after reconnect.
- An unanswered alarm rings for two minutes, then advances to the next queued
  finished timer.
- While an alarm rings, the microphone remains live without requiring a wake
  word. The spoken command `stop` dismisses the current alarm.
- No device firmware change or controller schema migration is required.

## Decision Log

- **Always listen for wake during an alarm.** A timer must be dismissable by
  voice even when ordinary barge-in is disabled. The alarm is known local
  speaker output, so this is a bounded exception rather than a change to normal
  speech-over-TTS behavior.
- **Discard missed offline expiries.** Replaying an old kitchen timer after an
  Echo reconnects is more confusing than useful. Timer alerts are best-effort;
  Home Assistant remains the source of truth for the timer lifecycle.
- **Keep delayed-command timers native to Home Assistant.** `In five minutes
  turn off the lights` is an automation-like command, not an audible reminder.
  HA already executes it at expiry, so EchoMuse must not add a redundant alarm.
- **Ship a native EchoMuse Lovelace card.** The Simple Timer Card Voice PE
  integration is a useful reference but mirrors a fixed number of ESPHome
  slots and writes a bespoke text-command protocol. EchoMuse needs all timers,
  stable IDs, accurate countdowns, and full HA-owned control, so its HACS
  integration supplies a dedicated dynamic card instead.
- **Use LLM-backed Assist pipelines only for EchoMuse timer commands.** The
  selected Home Assistant conversation agent must include the timer naming
  instruction below in its system prompt. EchoMuse does not mutate Home
  Assistant's `TimerInfo` to compensate for an omitted optional tool argument.

## Existing Home Assistant Support

Home Assistant 2026.8 already provides the timer model and built-in Assist
intents:

- `HassStartTimer`
- `HassCancelTimer`
- `HassCancelAllTimers`
- `HassIncreaseTimer`
- `HassDecreaseTimer`
- `HassPauseTimer`
- `HassUnpauseTimer`
- `HassTimerStatus`

`homeassistant.components.intent` exports `TimerInfo`, `TimerEventType`, and
`async_register_timer_handler`. This is the same timer integration point used
by Home Assistant's ESPHome integration, but it is independent of ESPHome.

The native `AssistSatelliteEntity` interface itself has no timer feature flag
or timer callback. The HACS integration must therefore register the handler
directly rather than attempting to recreate ESPHome's retired `TIMERS`
protocol feature.

When the selected pipeline uses an LLM conversation agent configured with HA's
Assist API, registering the timer handler makes these standard tools available
for the requesting Echo device:

- `intent__HassStartTimer`
- `intent__HassCancelTimer`
- `intent__HassIncreaseTimer`
- `intent__HassDecreaseTimer`
- `intent__HassPauseTimer`
- `intent__HassUnpauseTimer`
- `intent__HassTimerStatus`

The integration does not expose a parallel timer API to the LLM. Home
Assistant's timer intents remain the one command surface. The LLM configuration
must supply a non-empty timer name, deriving it from the duration when needed.

The required system-prompt instruction is:

```text
When calling HassStartTimer, always provide a concise, non-empty `name`.
If the user does not name the timer, derive the name from its duration, using
phrasing such as "1 minute timer", "10 second timer", or "2 hour timer".
Do not use a generic name such as "Timer".
```

This prompt belongs to the configured Home Assistant LLM conversation agent,
not to the EchoMuse integration: Home Assistant owns the Assist API tool
request and EchoMuse has no supported API for changing another conversation
agent's system prompt. The integration can verify that native timer tools are
available, but cannot guarantee compliance by an arbitrary LLM.

## Home Assistant Timer Dashboard

Timer visibility is a required part of the feature. Ship a frontend module in
the HACS integration that registers an `echo-voice-timers-card` Lovelace card.
Users add it to any Home Assistant dashboard; it is not a separate controller
dashboard and does not require an EchoMuse browser session.

The card displays every active, paused, finished, queued, or ringing timer
owned by an EchoMuse device. Each row shows:

- Timer name, with a duration fallback for unnamed timers.
- The originating Echo label.
- Active, paused, ringing, queued, or finished state.
- Remaining time and a live progress indicator.
- Pause/resume, add time, remove time, cancel, and dismiss controls when valid
  for the current state.

The empty state provides a duration and optional-name form for creating a
timer. The card may be configured to show all EchoMuse timers or a single
originating Echo device.

### Data Contract

The HACS backend registers authenticated Home Assistant WebSocket commands for
the card:

- `echo_voice_satellite/timers/list`
- `echo_voice_satellite/timers/subscribe`
- `echo_voice_satellite/timers/start`
- `echo_voice_satellite/timers/pause`
- `echo_voice_satellite/timers/resume`
- `echo_voice_satellite/timers/change`
- `echo_voice_satellite/timers/cancel`
- `echo_voice_satellite/timers/dismiss`

`list` returns all current EchoMuse voice timers. `subscribe` pushes a snapshot
followed by lifecycle deltas. The frontend calculates a smooth countdown from
an absolute `finishes_at` ISO timestamp rather than requiring a backend update
every second.

Each active or paused timer record includes:

```json
{
  "id": "01J...",
  "device_id": "ha-device-id",
  "device_name": "Kitchen Echo",
  "name": "pizza",
  "state": "active",
  "duration_seconds": 600,
  "remaining_seconds": 425,
  "finishes_at": "2026-08-29T12:34:56Z"
}
```

The stable HA timer ID is the card row key and every write target. This avoids
the identity churn and timer-count cap in Simple Timer Card's Voice PE slot
adapter. The card does not manufacture `timer.*` entities, template sensors,
or a second persistence store.

Home Assistant's `TimerManager` remains authoritative. The HACS WebSocket
handlers translate card actions to its start, pause, unpause, add/remove time,
and cancel operations. This is the one deliberate TimerManager-internal
boundary required for ID-addressed UI actions; keep it in a dedicated HACS
module with version-gated tests. It is distinct from overriding Assist intent
handlers, which this design does not do.

`FINISHED` removes the timer from Home Assistant's active timer manager. To
keep the card truthful while EchoMuse is alerting, the controller sends
`timer.alarm` state events through the existing authenticated `/api/events`
channel. HACS holds this short-lived presentation state only, including current
ringing timer and queue position; it never recreates a finished HA timer.

### Simple Timer Card Reference

`simple-timer-card` is inspiration for the user experience: a single compact
card, live progress, labels, sorting, empty-state creation, and row controls.
Its Voice PE implementation is not the transport to copy. It consumes
fixed-slot entities supplied by custom ESPHome YAML and writes commands such as
`pause:<timer_id>` to a `text.*` entity. That pattern is capped, reorders rows
when shorter timers are added, and does not natively control HA's TimerManager.

An optional read-only compatibility adapter for that card may be considered
later, but it is not part of the first release and must never be the source of
truth or primary control path.

## HACS Timer Registration

`EchoAssistSatellite.async_added_to_hass` will:

1. Obtain its Home Assistant device registry ID.
2. Register `async_register_timer_handler(hass, device_id, handler)`.
3. Store the unregister callback with `async_on_remove`.
4. Forward every timer event to the controller through the authenticated
   controller client.

The event payload contains the HA timer ID, name, original duration, remaining
duration, active state, and event type:

```json
{
  "event": "finished",
  "timer_id": "01J...",
  "name": "pizza",
  "total_seconds": 600,
  "seconds_left": 0,
  "is_active": false
}
```

Add `ControllerClient.async_timer_event()` and an integration-authenticated
controller endpoint such as:

```
POST /api/devices/{device_id}/timer-events
```

Timer events are state synchronization. Repeated delivery must be idempotent.

Timer alarms are best-effort notifications. If the controller or originating
Echo is unavailable at expiry, HACS logs the undeliverable event and does not
replay it later. Home Assistant remains the source of truth for timer state.

## Timer Command Scope

EchoMuse uses Home Assistant's native timer-selection behavior without
overriding handlers or accessing its internal `TimerManager`. Creating a timer
receives the Echo's HA device ID through the Assist pipeline context, so expiry
events route back to the Echo that created it. For status, named cancellation,
editing, pause, resume, and `cancel all`, Home Assistant applies its own
device/area matching and global rules.

This deliberately avoids a HACS timer-intent adapter. Replacing Home
Assistant's built-in handlers would couple EchoMuse to private timer internals,
could alter behavior for other Assist devices, and would duplicate the command
model Home Assistant already exposes to LLMs.

## Ordinal Timer Commands

Named timers cover the normal case:

- `cancel the pizza timer`
- `add two minutes to the pasta timer`
- `pause the laundry timer`

For LLM-backed Assist pipelines, the LLM may first call `HassTimerStatus` and
then issue a native timer command for the selected result. This is best effort:
the stock status tool returns a timer ID, but the stock cancel/edit tools accept
only a name or original duration, not that ID. If two timers cannot be uniquely
described by those fields, the operation may be ambiguous and the user should
use a name or duration. Sentence-only pipelines are outside the supported
EchoMuse timer configuration.

Do not add runtime sentence triggers or custom ordinal tools in the first
release. Both would require private Home Assistant APIs or a second timer
command surface.

## Controller Timer State

Add `controller/em_timers.py` as a pure decision/state module. It owns a
per-device alarm session with:

- Deduplication of timer lifecycle events by timer ID and state.
- One current finished timer.
- A FIFO queue of additional finished timers.
- Separate running timer state and finished-alarm acknowledgements.
- Transitions for finished, cancelled, dismiss-current, dismiss-all, safety
  timeout, disconnect, and alarm delivery failure.

Unlike the old ESPHome implementation, dismissing an alarm must not clear
still-running timers. A later expiry must still notify the user.

For example:

1. Pizza, pasta, and tea timers are started.
2. Pizza finishes and begins ringing.
3. Pasta and tea finish while pizza is ringing; both enter the queue.
4. Dismissing pizza starts the pasta alarm.
5. Dismissing pasta starts the tea alarm.

Repeated `finished` events for pizza do not produce extra queue entries.

## Speaker Ownership

The prior timer implementation used a `speaker_busy` counter. That detects
activity but is not mutual exclusion: a response can begin after an alarm
checks the counter and before it begins writing PCM. That permits two writers
on the device's `0x02` speaker plane.

Introduce a per-device speaker lock which spans all playback until the device
reports its buffer has drained through `playback_stats`:

- Buffered TTS playback.
- Streaming TTS playback.
- Home Assistant announcements.
  - Timer-alarm sound bursts.

The lock is not the existing `voice_lock`. `voice_lock` serializes whole voice
turns and cannot be held for an alarm, because a spoken dismissal needs to
start a new turn. The speaker lock serializes only output-plane ownership.

Timer alarms use a dedicated cancellation event. They must not use
`Device.cancel_event`, which belongs to voice turns. Dismissing an alarm must
not cancel or flush an unrelated response.

## Alarm Delivery

On a `finished` event, the controller starts an alarm worker only when no
alarm is currently active for the device. The worker:

1. Waits until an in-flight voice turn finishes before first playback.
2. Interrupts Music Assistant playback using its existing speaker-ownership
   path.
3. Plays the bundled Home Assistant Voice PE timer sound.
4. Repeats the sound with a short gap until dismissed or the safety timeout
   expires.
5. Keeps subsequent finished timers queued.
6. Restores interrupted music only after the current alarm is fully resolved.
7. Starts the next queued alarm immediately after dismissal or timeout.

The safety timeout is two minutes. It stops the current alarm and advances to
the next finished timer rather than silently discarding queued notifications.

If the device is privacy-muted when an alarm becomes current, the controller
discards that local notification rather than retaining or replaying audible
speech after unmute. The queue then advances to the next finished timer.

Home Assistant delayed-command timers, such as `in five minutes turn off the
lights`, retain their native behavior. HA executes the command at expiry; it
does not send an Echo timer event and does not enter the EchoMuse alarm queue.

Use the Voice PE timer sound already identified in PR #167. It is 48 kHz mono,
the device wire format, and must ship with its CC BY 4.0 attribution in
`controller/sounds/LICENSE.md` and `NOTICE.md`. Decode and cache PCM once. A
missing or undecodable file is a packaging failure: log the fault and do not
substitute an inconsistent synthesized alarm.

No timer-name announcement is generated. The alert is intentionally a local,
continuous sound so the user can hear it while speaking `stop` over it.

## LEDs

Use a distinct amber pulse while an alarm rings on firmware advertising
`led_anim`. The animation has a short TTL so it clears if the controller dies.
Do not allow alarm updates to repaint over listening, thinking, or speaking
states during an active voice turn.

Add a legacy streamed-LED fallback only if it can preserve existing
controller-driven LED behavior without animation churn.

## Dismissal

The microphone remains live while an alarm rings. The device AEC handles the
known speaker output, as it already does for barge-in.

While ringing:

- The microphone remains live without a wake word. Speech-level detection
  starts an Assist turn whose transcript is expected to be `stop`.
- A recognized `stop` dismisses the current alarm and flushes only buffered
  alarm audio.
- The next queued finished timer waits until the resulting voice turn has
  finished before beginning its own alert.

The alarm's speech-level detector starts a normal Assist turn, and the
transcript decides whether to dismiss. The chime is flushed as soon as the
turn starts so `stop` is not buried under the alarm. HACS must still always
send `tts/end`, including for an ordinary silent intent, because the turn
engine's TTS rendezvous otherwise remains unresolved and can hold `voice_lock`
indefinitely.

`cancel all timers` follows Home Assistant's native timer scope. A plain
`stop` dismisses only the current alarm and advances to the next queued
finished timer.

## Failure and Lifecycle Behavior

- A timer event arriving during a short creation response queues safely rather
  than interleaving its audio with that response.
- Alarm-task exceptions clear or advance logical alarm state, so a failed
  delivery cannot wedge later timer notifications.
- Device disconnect cancels its physical alarm task. Finished events that
  cannot be delivered while the Echo or controller is unavailable are logged
  and discarded rather than replayed on reconnect.
- Controller restart does not change Home Assistant's timer ownership. The
  controller begins handling subsequent events after HACS reconnects.
- Home Assistant's built-in timer manager is in-memory. Timers do not survive
  a Home Assistant restart; this design deliberately inherits that behavior
  rather than building a conflicting second timer system in EchoMuse.

## Tests

### HACS tests

- Timer handler registration and unregistration.
- Correct HA device registry ID association.
- Forwarding of started, updated, cancelled, and finished events.
- Built-in intent and Assist API tool availability after timer-handler
  registration.
- Native timer command context preserves the Echo's device ID at creation.
- Named and duration-based timer operations use Home Assistant's native
  selection behavior.
- LLM ordinal handling is documented and never claims exact selection where
  duplicate names or durations are ambiguous.
- Non-EchoMuse timer behavior remains unchanged.
- Timer-card WebSocket list snapshot, lifecycle subscriptions, and permissions.
- Card actions target the exact HA timer ID and preserve HA timer ownership.
- `finishes_at` is correct for active timers and absent for paused timers.
- Controller `timer.alarm` events render ringing and queued presentation state
  without recreating finished HA timers.

### Frontend card tests

- Dynamic timer rows retain stable keys while timers are created, updated,
  cancelled, and reordered.
- Countdown and progress derive from `finishes_at` without per-second backend
  messages.
- Paused, ringing, queued, and finished presentation states are distinct.
- Creation form and row controls send the expected authenticated WebSocket
  commands.
- A card filtered to one Echo excludes timers from other Echoes.
- Finished events while the controller or originating Echo is unavailable are
  logged and discarded.
- Ordinary silent intents still send `tts/end`.

### Controller tests

- FIFO queue order for multiple finished timers.
- Duplicate-event idempotency.
- Dismiss current versus dismiss all.
- Running timers remain after dismissing a finished alarm.
- Cancelled running timers never ring.
- Muted expiries are discarded locally.
- A two-minute timeout advances to the next queued finished timer.
- Delayed-command timers execute through Home Assistant without creating an
  EchoMuse alarm.
- An alarm-delivery failure does not wedge the queue.
- Disconnect and reconnect behavior.

### Concurrency tests

- A one-second timer finishing during its creation response.
- A timer finishing during an announcement.
- A timer finishing while music plays.
- Bare `stop` speech dismissal during an alarm without a wake word.
- Alarm speech detection while normal barge-in is disabled.
- Two timers finishing seconds apart.
- No concurrent writers on the `0x02` speaker plane.

### Hardware acceptance

1. Create three named timers consecutively.
2. Edit, pause, resume, and query each by name and duration.
3. Verify an LLM pipeline can resolve an unambiguous ordinal request through
   timer status, then verify duplicate timers are reported or handled as
   ambiguous rather than silently targeting the wrong one.
4. Verify `cancel all` follows Home Assistant's native scope.
5. Let remaining timers expire and verify a separate continuous alarm for each.
6. Verify bare-`stop` voice dismissal latency.
7. Verify compound commands preserve Home Assistant's spoken confirmation.
8. Verify Music Assistant resumes correctly after final dismissal.

## Implementation Phases

### Phase 1: HA Timer Registration and Event Transport

1. Add HACS timer-handler registration for each `EchoAssistSatellite` using
   its HA device registry ID.
2. Add controller-client support for timer lifecycle delivery.
3. Add the integration-authenticated controller timer-event endpoint.
4. Implement the event payload and idempotent handling for started, updated,
   cancelled, and finished events.
5. Verify that Home Assistant's selected Assist pipelines expose the native
   timer intents and Assist API LLM tools after registration.

Exit criteria:

- Voice-created named timers reach the controller as lifecycle events.
- Native HA voice and LLM timer creation, status, pause/resume, adjustment,
  and cancellation continue to work without replacing any HA intent handler.
- Timer creation records the originating Echo's HA device ID.

### Phase 2: Controller Timer State and Alarm Queue

1. Add the pure `em_timers.py` device alarm-session state machine.
2. Track current alarm, FIFO finished queue, deduplication, dismissal, timeout,
   muted expiry, disconnect, and delivery failure transitions.
3. Add controller-to-HACS `timer.alarm` event state for ringing and queue
   presentation.
4. Add unit tests for all state transitions before integrating device audio.

Exit criteria:

- Multiple finished timers retain FIFO order.
- Repeated lifecycle events do not create duplicate alarms.
- Dismissal, cancellation, timeout, and muted expiration cannot discard a
  still-running timer or wedge later alarms.

### Phase 3: Speaker Ownership and Alarm Playback

1. Introduce the per-device speaker lock around all voice-plane playback and
   device-confirmed buffer drain.
2. Keep alarm cancellation separate from `Device.cancel_event`.
3. Package the Voice PE timer sound, attribution, and decode/cache path.
4. Implement the alarm worker: music interruption, continuous sound, two-minute
   timeout, and queue advancement.
5. Add LED animation for capable devices and preserve voice-turn LED priority.

Exit criteria:

- A timer that expires during TTS, an announcement, or music never creates two
  writers on the speaker plane.
- The originating Echo receives the continuous alarm sound.
- Music resumes only after the final active alarm resolves.
- Missing sound assets and playback failures are explicit and cannot wedge the
  queue.

### Phase 4: Wake and Button Dismissal

1. Keep wake-word scoring active during timer alarms regardless of normal
   `bargeInEnabled` configuration.
2. On wake or detected speech, dismiss and flush only the current alarm audio;
   do not create an Assist turn or generate a confirmation response.
3. Consume an action-button tap as immediate current-alarm dismissal while
   preserving hold events.
4. Ensure a muted device discards new expiries locally and cannot leak audio.

Exit criteria:

- Wake-to-dismiss works with normal barge-in disabled.
- Alarm dismissal does not create an unsolicited HA response.
- Button dismissal is immediate and does not cancel an unrelated turn.

### Phase 5: Home Assistant Timer Dashboard Card

1. Add authenticated HACS WebSocket commands for timer list, subscription,
   start, pause, resume, change, cancel, and dismiss actions.
2. Isolate TimerManager access behind that backend boundary and add HA-version
   compatibility tests.
3. Ship the `echo-voice-timers-card` frontend module in the HACS integration.
4. Render dynamic rows from stable timer IDs, with `finishes_at` based local
   countdowns, source Echo labels, controls, ringing state, and queue state.
5. Add optional per-Echo filtering and an empty-state timer-creation form.

Exit criteria:

- A user can view and manage every EchoMuse timer from a normal HA dashboard.
- The card does not create timer entities, fixed slots, or a second persistence
  store.
- UI actions address the exact HA timer ID and update across open card clients.

### Phase 6: Integration, Regression, and Hardware Validation

1. Run controller and HACS unit suites, including timer state, lifecycle,
   speaker serialization, WebSocket contract, and frontend-card tests.
2. Exercise native local Assist and Assist-API LLM pipelines.
3. Test multiple named timers, unambiguous ordinal LLM requests, duplicate timer
   ambiguity, and HA-native `cancel all` scope.
4. Test timer expiry during TTS, announcements, music, mute, controller/device
   disconnect, and a DST transition simulation.
5. Validate on hardware: sound, spoken labels, LEDs, wake/button dismissal,
   queue ordering, and music restoration.

Exit criteria:

- All documented hardware acceptance checks pass.
- Timer duration remains correct across DST changes because expiry and alarm
  timeout use monotonic clocks, while the card only presents a UTC deadline in
  local time.
- Home Assistant restart behavior is documented and verified as the inherited
  Assist timer limitation.
