# Timer Validation

## Automated Coverage

Run from the repository root:

```bash
cd controller && python -m pytest tests/
cd ../hacs && python -m pytest tests/
```

The suites cover timer lifecycle forwarding (including that a delayed-command
timer is never forwarded at all), FIFO alarm queues, duplicate events,
speaker serialization, music interruption/resume, mute/disconnect handling,
LED state, action-button dismissal, capability-gated local stop-word
dismissal, the timer card's full WebSocket command set and ringing/queued
presentation bridge
(`hacs/tests/test_timer_manager.py`, `test_timer_card_hub.py`,
`test_timer_card.py`), and the alarm's ring/safety-timeout duration being
immune to a wall-clock (DST) jump (`controller/tests/test_timer_alarm.py`).
They also cover countdown-ring selection and interpolation, paused non-decay,
scene colour, lifecycle refresh, alarm replacement, animation supersession,
the existing-`led_anim` safe fallback on older firmware, and loss of the
transient presentation after a controller restart (`controller/tests/test_timer_ring.py`,
`test_controller_device.py`, and `test_api_controller_branches.py`).

## Hardware Acceptance

These checks require a real EchoMuse device and Home Assistant pipeline. Mark
each result with the controller and firmware version used.

- Create three named timers; verify FIFO alarms and final music resume.
- Start two timers at different times; while the device is otherwise idle,
  verify the first-started timer's scene-coloured partial ring shrinks. Pause
  it and verify the segment holds still; resume it and verify it continues.
- While that timer runs, exercise a voice turn, volume press, mute, and link
  loss. Each higher-priority state must take the ring, and the countdown must
  return when the device returns to its resting state.
- Let the displayed timer expire; verify its partial ring is immediately
  replaced by the distinct amber alarm pulse.
- Test firmware that supports `countdown` and firmware that only advertises
  the older `led_anim` set. The latter must leave the resting ring dark while
  preserving timer and alarm behaviour.
- Restart the controller with a timer still running. The countdown may remain
  dark until a later timer lifecycle event (for example pause, resume, or an
  adjustment); verify that event restores the correct presentation rather than
  creating or restoring a separate timer record.
- Create a timer while a voice reply is playing; verify the reply drains, then
  the timer chimes repeatedly.
- Create a timer while Music Assistant audio is playing; verify chime, timer
  LED, local speech/button dismissal, then resume.
- During an alarm on a device with a ready stop-word model, say `stop`; verify
  the device dismisses the alarm locally with no Assist intent or TTS response.
  Repeat on a device without that capability or model; it must keep ringing
  until action-button, card, or timeout dismissal.
- During an alarm, leave silence/noise after a chime; verify it continues to
  ring and is not self-dismissed.
- Press the action button; verify immediate local dismissal. Hold the button;
  verify its ordinary HA event still fires.
- Verify muted expiry produces neither chime nor alarm LED.
- Disconnect and reconnect during an alarm; verify the local alert is cleared
  and subsequent timers can ring.
- Test local Assist and the selected LLM pipeline for named timers, duplicate
  names, ordinal references, pause/resume, adjustment, and `cancel all`.
- Test the Lovelace card: creation form (with and without a `device_id`
  filter configured), pause/resume, +1m/-1m, cancel, and — while a timer is
  ringing — that it stays visible as "Ringing" and Dismiss actually stops
  the physical alarm.
- Check a DST boundary in the selected timezone, letting a real timer expire
  across it. This is Home Assistant's own `TimerManager` expiry, which
  EchoMuse does not control; the alarm's own ring/timeout duration on the
  EchoMuse side is covered by an automated wall-clock-jump test and does not
  need re-checking here (see Automated Coverage).

## Known Limitation

Home Assistant's native `TimerManager` is in-memory. A Home Assistant restart
does not preserve active Assist timers; the dashboard therefore cannot restore
them after restart.

The controller's timer-ring presentation is also in-memory, but for a
different reason: it is only a rendering cache, not a timer record. After a
controller restart, an otherwise quiet active timer has no countdown ring until
a later Home Assistant lifecycle event reaches the controller. EchoMuse does
not request a timer snapshot to fill that gap.
