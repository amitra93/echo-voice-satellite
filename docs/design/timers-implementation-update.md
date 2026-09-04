# Timers Implementation Update

Status audit of `docs/design/timers-design.md`'s six implementation phases
against the codebase, plus the plan for what remained and a record of what
has since been built. This document is a companion to that design doc, not
a replacement for it — the decisions and data contract there still stand;
this records where the build has landed relative to them.

## Phase status summary

| Phase | Status |
|---|---|
| 1 — HA Timer Registration and Event Transport | Done — the delayed-command forwarding bug (P0 below) is fixed |
| 2 — Controller Timer State and Alarm Queue | Done |
| 3 — Speaker Ownership and Alarm Playback | Done |
| 4 — Wake and Button Dismissal | Done (via a stricter mechanism than the doc describes — see below) |
| 5 — Home Assistant Timer Dashboard Card | Done — rebuilt to the documented data contract and command set (P1 below) |
| 6 — Integration, Regression, Hardware Validation | Automated portion done, including the DST regression (P2); hardware acceptance is pending by nature |

## Phase 1 & 2 — implemented

`em_timers.AlarmSession` (dedup by fingerprint, FIFO queue, dismiss-current/
dismiss-all/timeout/disconnect/delivery-failure transitions), the idempotent
`POST /api/devices/{id}/timer-events` endpoint, and `timer.alarm`
presentation events pushed over `/api/events` all match the design.

The one gap here — HACS forwarding every `TimerInfo` event regardless of
`timer.conversation_command`, so a delayed-command timer like `in five
minutes turn off the lights` rang the EchoMuse alarm for something the user
never asked to be alerted about — is fixed; see P0 below.

## Phase 3 — implemented

`device.speaker_lock` wraps buffered/streaming playback and the alarm's
`_play` callback identically (`em_controller.py`); alarm cancellation uses
its own `asyncio.Event` per device (`em_timer_alarm.py`), never
`Device.cancel_event`; the sound is decoded once and cached; the worker
interrupts/resumes music, repeats with `BURST_GAP_S`, honors
`MAX_RING_S=120s`, and advances the queue. LED animation (`timer_anim`) is
wired to alarm start/end.

## Phase 4 — implemented, via a stronger mechanism than specified

Wake/speech dismissal is gated through an STT-only mini-turn
(`_run_timer_speech_turn`, `stt_only=True`,
`end_stage=PipelineStage.STT`) rather than raw RMS or a full Assist turn — no
intent/TTS ever runs, so it satisfies "do not create an Assist turn or
generate a confirmation response" more strictly than the literal
wake/speech-flush behavior the doc describes. Button-tap dismissal, hold
preservation, and muted-expiry discard are all in place and are not gated on
`bargeInEnabled`.

## Phase 5 — rebuilt to the documented data contract

See P1 below for what changed and why.

## Phase 6 — automated portion done, including DST (P2)

Automated coverage is broad and green across the controller and HACS
suites, including the DST transition regression (P2 below). Hardware
acceptance is inherently manual and still pending.

## P0 — delayed-command timers no longer create a spurious alarm (done)

`hacs/custom_components/echo_voice_satellite/assist_satellite.py::_timer_event`
now returns immediately, before scheduling anything, when
`timer.conversation_command` is set — a delayed-command timer never reaches
the controller and can no longer ring the EchoMuse alarm.

Tests: `hacs/tests/test_assist_satellite.py::
test_delayed_command_timer_events_are_never_forwarded_to_the_controller` and
`test_ordinary_timer_events_are_still_forwarded_via_timer_event` pin the gate
both ways. `docs/design/timers-design.md`'s Controller tests section now
notes this is enforced at the HACS layer specifically — the controller's
timer-event payload never carries `conversation_command`, so it has no way
to enforce this itself.

## P1 — Phase 5 rebuilt to match the documented data contract (done)

**Backend.** New module
`hacs/custom_components/echo_voice_satellite/timer_card.py`, pulled out of
`__init__.py` per the design's "keep it in a dedicated HACS module with
version-gated tests":

- **Eight WebSocket commands**, one per documented action:
  `echo_voice_satellite/timers/{list,subscribe,start,pause,resume,change,
  cancel,dismiss}`.
- **The documented row shape.** `manager_rows()` builds `id`, `device_id`,
  `device_name` (resolved via `homeassistant.helpers.device_registry`, not
  the coordinator's own device records — a HA device id and an EchoMuse
  device id are different id spaces, and the registry lookup works even for
  a device the coordinator hasn't polled yet), `name`, `state`
  (`"active"`/`"paused"`), `duration_seconds` (`TimerInfo.created_seconds`),
  `remaining_seconds` (`TimerInfo.seconds_left`, monotonic-clock-derived),
  and `finishes_at` — computed fresh from `remaining_seconds` on every
  snapshot rather than stored, so nothing carries a wall-clock value across
  time and a DST transition between two snapshots cannot skew it (the same
  property Phase 6's exit criteria call for). Absent while paused.
- **The presentation bridge (the piece Phase 5 was missing entirely).**
  `AlarmPresence` holds the latest `timer.alarm` payload per EchoMuse
  device id (each `TimerRecord` in that payload already carries the
  originating HA device id as `ha_device_id`, and the payload's top-level
  `device_id` is the EchoMuse device id — exactly the mapping `dismiss`
  needs). `build_snapshot()` merges `AlarmPresence`'s ringing/queued rows
  in alongside TimerManager's active/paused rows, filtered against
  `manager.timers.keys()` so nothing is ever double-listed. A ringing timer
  therefore stays visible, and dismissable, for the entire time it is
  ringing — not just until HA's `FINISHED` event removes it from
  `TimerManager`.
- **`dismiss` reaches the controller.** New endpoint
  `POST /api/devices/{id}/timer-alarm/dismiss` (`em_api.py`, wrapping the
  already-existing `dismiss_timer_alarm()` — the same function the wake/
  speech/button dismissal paths already call) and
  `ControllerClient.async_dismiss_timer_alarm(device_id)`. `TimerCardHub`
  looks up the owning EchoMuse device id via
  `AlarmPresence.echomuse_device_for_timer()` before calling it.
- **`change` uses `TimerManager.add_time()`/`remove_time()`** — found by
  reading the real `TimerManager` source
  (`docker exec homeassistant python -c "from homeassistant.components.intent
  import TimerManager; import inspect; print(inspect.getsource(TimerManager))"`),
  not guessed: a `dir()` probe alone had missed them. `remove_time` is just
  `add_time` with the sign flipped, so the card's `change` command takes a
  signed `seconds` delta and covers both add and remove.
- **`TimerManager`'s not-found error isn't part of its public export
  surface** (`homeassistant.components.intent` exports the manager and the
  handler type, not `TimerNotFoundError` — it lives at
  `homeassistant.components.intent.timers.TimerNotFoundError`, a private
  submodule path). `apply_timer_action()`/`start_timer()` therefore catch a
  broad `Exception` around every `TimerManager` call rather than importing
  that private path, deliberately: an unrecognised action and a
  timer-not-found both fail closed the same way, and nothing here should
  break if HA renames or moves that exception.
- **`subscribe` has no `TimerManager` hook to attach to**, so `TimerCardHub`
  drives pushes itself: `EchoAssistSatellite._timer_event` (the one handler
  TimerManager allows to be registered per device — it does not support
  multiple handlers for the same device, which is why the card can't
  register its own second listener) calls `hub.notify_manager_change()`
  synchronously, since TimerManager has already mutated its own state by
  the time it invokes that handler; the coordinator's generic event fan-out
  (`async_add_event_listener`, the same mechanism `wake.offer`/`ble.adverts`
  already use) delivers `timer.alarm` events to `hub.notify_alarm_event()`.
  Both funnel into one `_push()` that snapshots and calls every subscribed
  connection's `send_event`. `_push()` skips the snapshot entirely when
  there are no subscribers — the common case between card opens, and it
  means a lifecycle event arriving before `TimerCardHub`'s own imports are
  even resolvable (early in HA startup) is harmless as long as nothing has
  subscribed yet.
- **Every hass/registry touch is an injected callable** on `TimerCardHub`
  (`manager_getter`, `device_name_resolver`, `known_devices_getter`), so the
  orchestration logic is fully unit-tested with plain fakes —
  `hacs/tests/test_timer_card_hub.py` — with no Home Assistant install
  needed. `async_setup_timer_card()` is the only place the real accessors
  are wired in, and their `homeassistant` imports are deferred into each
  accessor's own body (not done once at hub-construction time), matching
  this package's established "defer into function bodies" convention —
  building the hub must not itself require those modules importable, only
  actually taking a snapshot does.
- **Registration wiring has real test coverage, not just shape assertions.**
  `hacs/tests/test_timer_card_hub.py` locally stubs
  `homeassistant.components.websocket_api` (the same per-test
  `sys.modules` stubbing pattern `test_assist_satellite.py` already uses for
  `homeassistant.components.intent`), built to match the real module's
  shape as introspected via `docker exec homeassistant python -c ...`
  (`websocket_command`/`async_response`/`callback` decorators setting
  `_ws_command`, `async_register_command` storing by that name,
  `ActiveConnection.send_result`/`send_error`/`send_event`/`subscriptions`).
  That proves all eight command handlers actually call the hub correctly,
  not just that the source contains the right strings.

**Frontend** (`hacs/www/echo-voice-timers-card.js`, rewritten):

- `hass.connection.subscribeMessage(cb, {type: "echo_voice_satellite/
  timers/subscribe"})` replaces the 1s poll; a 1s `setInterval` still runs,
  but only to redraw the countdown text from the already-known
  `finishes_at`, never to refetch.
- Four distinct presentation states (`active`/`paused`/`ringing`/`queued`),
  each with its own controls: a **Dismiss** button only appears for
  `ringing`; `queued` rows offer nothing (they start ringing on their own);
  `active`/`paused` get Pause-or-Resume, `+1m`/`-1m` (→ `change`), and
  Cancel.
- Empty-state duration + optional-name creation form, with a device picker
  populated from the snapshot's `devices` list — needed because
  `TimerManager.start_timer()` requires an explicit device id, and an
  unfiltered card has no other way to know which Echo a new timer is for.
- Optional `device_id` card-config filter, restoring the per-Echo scoping
  the earlier TimerManager rewrite had dropped.

**Tests:** `hacs/tests/test_timer_manager.py` (pure `manager_rows`/
`apply_timer_action`/`start_timer`/`AlarmPresence`/`build_snapshot` logic),
`hacs/tests/test_timer_card_hub.py` (hub orchestration + WS wiring, above),
`hacs/tests/test_init.py` (the hub is stored on `hass.data`, a `timer.alarm`
coordinator event reaches it and an unrelated event type doesn't, and one
end-to-end test resolves the real deferred accessors against faithful
`TimerManager`/device-registry stand-ins), `hacs/tests/test_assist_satellite.py`
(`_timer_event` notifies the hub, and does not for a delayed-command timer),
`hacs/tests/test_timer_card.py` (rewritten source-shape assertions for the
new command set, states, controls, and creation form), `controller/tests/
test_api_controller_branches.py` + `hacs/tests/test_client.py` (the new
dismiss endpoint and client method).

Full suites after this work: controller `988 passed, 3 skipped`; HACS
`291 passed`.

## P2 — Phase 6 DST gap closed (done)

`controller/tests/test_timer_alarm.py` gained two tests:

- `test_em_timer_alarm_never_imports_wall_clock_modules` — a structural
  guard pinning the property that makes ring timing DST-safe in the first
  place: `em_timer_alarm.py` has no `time`/`datetime` import to be tempted
  to read `time.time()`/`datetime.now()` from at all. Its only clock is
  `asyncio.get_running_loop().time()` (`CLOCK_MONOTONIC`), which a system
  wall-clock adjustment — DST or an NTP correction — never moves.
- `test_alarm_ring_duration_is_immune_to_a_wall_clock_dst_jump`
  (parametrized both directions, `+3600`/`-3600` seconds) — with
  `max_ring_s=0` the safety timeout is already met by the time the first
  burst returns, so the alarm dismisses after exactly one burst regardless
  of jump direction or size. If the deadline were ever computed against
  `time.time()` instead of the loop's monotonic clock, springing forward
  would fire the timeout early and falling back would delay it — either
  would change the observed burst count, which is what the test actually
  asserts stays fixed at 1.

`docs/timer-validation.md`'s Hardware Acceptance list no longer asks anyone
to manually re-verify the EchoMuse-side alarm timeout across a DST
boundary — that's automated now. The remaining manual DST line is scoped to
what EchoMuse genuinely doesn't control: Home Assistant's own `TimerManager`
expiry, which the design has always deferred to HA's monotonic-clock-backed
`_wait_for_timer`.
