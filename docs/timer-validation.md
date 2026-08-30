# Timer Validation

## Automated Coverage

Run from the repository root:

```bash
cd controller && python -m pytest tests/
cd ../hacs && python -m pytest tests/
```

The suites cover timer lifecycle forwarding, FIFO alarm queues, duplicate
events, speaker serialization, music interruption/resume, mute/disconnect
handling, LED state, action-button dismissal, STT-only timer dismissal, and
the HACS timer-card transport.

## Hardware Acceptance

These checks require a real EchoMuse device and Home Assistant pipeline. Mark
each result with the controller and firmware version used.

- Create three named timers; verify FIFO alarms and final music resume.
- Create a timer while a voice reply is playing; verify the reply drains, then
  the timer chimes repeatedly.
- Create a timer while Music Assistant audio is playing; verify chime, timer
  LED, local speech/button dismissal, then resume.
- During an alarm, speak `stop`; verify HA STT returns a non-empty transcript,
  no Assist intent/TTS response is created, and the alarm stops.
- During an alarm, leave silence/noise after a chime; verify it continues to
  ring and is not self-dismissed.
- Press the action button; verify immediate local dismissal. Hold the button;
  verify its ordinary HA event still fires.
- Verify muted expiry produces neither chime nor alarm LED.
- Disconnect and reconnect during an alarm; verify the local alert is cleared
  and subsequent timers can ring.
- Test local Assist and the selected LLM pipeline for named timers, duplicate
  names, ordinal references, pause/resume, adjustment, and `cancel all`.
- Check a DST boundary in the selected timezone. Home Assistant owns expiry;
  EchoMuse only uses monotonic time for alarm timeout.

## Known Limitation

Home Assistant's native `TimerManager` is in-memory. A Home Assistant restart
does not preserve active Assist timers; the dashboard therefore cannot restore
them after restart.
