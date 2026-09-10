# Timer Alert Stop Contract

**Superseded.** The mechanism proposed below — a per-device timer-alert
*turn*, published over the normal `turn.cancel`/`turn.terminal` events so
HACS could cancel it the same way it cancels a response — was not what
shipped. The alarm remained controller-owned local PCM, as this note already
anticipated as the fallback in its last paragraph, but dismissal did not need
a correlated turn ID at all: the controller stops the ring itself
(`em_timer_alarm.TimerAlarmRunner.stop()`, a dedicated per-device
`asyncio.Event`, never `Device.cancel_event`) and the device's local stop-word
detector dismisses it only when that capability and model are ready. HACS has
no timer-alert pipeline or cancellation protocol. See
`../../docs/timer-validation.md` and `../../docs/audio-states.md` §5.3 for the shipped
behaviour. Left below for the reasoning it recorded at the time —
correlated ownership over an uncorrelated identifier was and remains the
right call for anything that *does* reuse the turn-cancellation path.

---

The current controller timer alarm runner plays local PCM and does not expose
a turn ID to HACS. HACS cannot cancel or acknowledge that audio safely: the
existing `turn.cancel` contract is deliberately turn-correlated.

To make timer alerts stoppable through the same path as responses and
announcements, the controller must create one per-device timer-alert turn and
publish its normal lifecycle events:

```json
{"type":"turn.cancel","device_id":"...","turn_id":123,"reason":"stop"}
```

The `turn_id` must identify only the firing device's alert. On accepting this
event, HACS cancels only tasks and the audio channel owned by that exact turn,
sends `POST /api/turns/123/tts/end` when it owned a TTS rendezvous, and rejects
all later pipeline/TTS callbacks retained from it. A `turn.terminal` for the
same turn has the same cleanup semantics without a second `tts/end`.

If timer alarm PCM remains controller-owned, the controller must instead stop
it locally and publish a terminal event with the correlated alert turn ID.
HACS must not infer ownership from `timer_id`, device ID, or a timer lifecycle
event: those identifiers are not sufficient to prevent a delayed stop from
cancelling a later response.
