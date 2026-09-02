# HACS-Owned Streaming STT via Gemini 3.5 Transcribe — Design Plan

**Status:** Proposed, not implemented. This is Option A from the
brainstorm that produced it — a custom `stt` platform inside the
`echo_voice_satellite` HACS integration, correlated to the active turn via an
in-process callback rather than `PipelineEvent` or `hass.bus`.

## Motivation

`assist_satellite.py` today forwards exactly one transcript per turn to the
controller, on `PipelineEventType.STT_END` (`assist_satellite.py:367-376`):

```python
await self.client.async_turn_action(turn_id, "transcript", {"text": text, "is_final": True})
```

That single call is a ceiling imposed by Home Assistant, not by this
integration or the controller: `assist_pipeline`'s STT phase only has
`STT_START`/`STT_VAD_START`/`STT_VAD_END`/`STT_END` in its
`PipelineEventType` enum, and only `STT_END` carries `stt_output.text` — this
is true for *every* STT provider plugged into the pipeline, Gemini or
otherwise, because `stt.SpeechToTextEntity.async_process_audio_stream()`
itself only ever returns one `SpeechResult` per call. There is no HA-core
mechanism for a provider to hand partials up through the pipeline event
stream. Waiting for HA to add one is not a plan; becoming the STT provider
ourselves is the only way to see partials at all today.

`em_turn_engine.py`'s `transcript` action already carries an `is_final` field
(`em_turn_engine.py:352-362`) — it's just always `True`, because there has
only ever been one caller. Getting from "field exists but is ignored" to
partials showing up on the controller requires two independent pieces of
work: **producing** partials on the HA side (this doc), and **consuming**
them correctly on the controller side (the "Controller-side changes"
section below). Both are needed; neither alone is useful.

## Model

[Gemini 3.5 Transcribe](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-5-transcribe/)
(announced 2026-08-26) ships as two separate API surfaces — real-time
streaming via the **Live API**, model id **`gemini-3.5-transcribe-live`**
(not `gemini-flash-3.5-transcribe-live`; correcting the id here since it's
the one that matters for every code reference below), and pre-recorded
processing via the Interactions API (`gemini-3.5-transcribe`, no relevance
here — we have no recorded files, only a live mic stream). The streaming
model is the one this plan uses.

Published numbers worth knowing before committing to it: sub-second latency,
4.0% WER in streaming mode vs. 2.6% non-streaming (both measured by
Artificial Analysis), and "70% faster time-to-final transcription" vs.
Google's previous model. The streaming/non-streaming WER gap is Google's own
disclosure, not ours — treat "how good are the partials specifically,
compared to the eventual final" as unmeasured until we've run it against
this fleet's actual rooms and mic capture, the same posture CLAUDE.md takes
toward every other vendor performance claim in this codebase.

## Decisions locked in

- **Gemini becomes the pipeline's STT provider, not a shadow copy running
  alongside whatever is already configured.** One Gemini Live session per
  turn does double duty: its interim results are the partials, and its last
  result is the exact string `STT_END` reports back to the pipeline. This
  avoids paying for two STT calls per turn and avoids two recognizers ever
  disagreeing about what the final text was.
- **HA's pipeline shape is unmodified.** `RUN_START → STT_START →
  STT_VAD_START/END → STT_END → INTENT_* → TTS_* → RUN_END` looks exactly as
  it does today; from the pipeline's perspective this is just another `stt`
  platform implementing the standard contract. HA still owns end-of-speech
  detection (its own VAD-gated audio segmenter decides when the async
  generator handed to `async_process_audio_stream` stops producing chunks) —
  this plan does not take that responsibility on, the same way no other STT
  provider does.
- **Partials are correlated to a turn via the audio-stream object itself,
  not a registry.** `assist_satellite.py` is already the thing that
  constructs the audio generator handed to the pipeline
  (`channel.mic_frames()`, passed into
  `super().async_accept_pipeline_from_satellite(...)` at
  `assist_satellite.py:256-260`). Wrapping that generator in our own type
  and having the new `stt.py` entity `isinstance()`-check the `stream`
  argument it's handed gives the STT entity a callback bound to the exact
  satellite/turn it's serving, with no shared dict to keep in sync and no
  risk of a partial from one device's turn reaching another's.
- **This replaces whichever STT engine the user's Assist pipeline currently
  uses, and is opt-in.** Selecting it is a manual step in HA's pipeline
  settings (STT engine dropdown), not something that happens automatically
  on integration update. A user who already has Google Cloud STT, Whisper,
  or anything else configured keeps it, and keeps getting only a final
  transcript, until they deliberately switch.
- **The controller stays exactly as unaware of Gemini/GCP as it is of every
  other STT provider today.** No controller code needs to know which
  provider is behind the pipeline; it only ever sees the same
  `transcript {text, is_final}` REST calls it does now, with `is_final`
  finally meaning something.
- **Partials terminate at the controller.** They are logged there
  (controller-side, see "Controller-side changes") and go no further — there
  is no device-bound message for either partial or final transcript text
  today, and this plan does not add one. The device has no use for it: it
  only ever renders audio and LED state, never text, and every existing
  wire message it understands (`internal/client/control.go`) is unchanged by
  this plan. Stated explicitly here because it's an easy thing to assume
  needs wiring and doesn't.

## Target architecture

```
Controller ──MIC_PCM (16 kHz mono S16LE, 80 ms frames)──▶ audio WS ──▶ assist_satellite.py
                                                                          │
                                                          CorrelatedMicStream(channel.mic_frames(),
                                                                              on_partial=self._on_stt_partial)
                                                                          │
                                                          async_accept_pipeline_from_satellite(...)
                                                                          │
                                                                 HA assist_pipeline (UNCHANGED)
                                                                          │  calls, like any stt provider:
                                                                          ▼
                                                          stt.py: GeminiTranscribeEntity
                                                            .async_process_audio_stream(stream, metadata)
                                                                          │
                                                    ┌─────────────────────┴─────────────────────┐
                                                    │ sender task: pump stream chunks in          │
                                                    │ receiver task: read interim/final results   │
                                                    └─────────────────────┬─────────────────────┘
                                                                          │
                                                         Gemini Live API, gemini-3.5-transcribe-live
                                                                          │
                        interim result ──▶ stream.on_partial(text) ──▶ assist_satellite._on_stt_partial
                                                    │                            │
                                                    │                            ▼
                                                    │              client.async_turn_action(turn_id,
                                                    │                "transcript", {text, is_final:false})
                                                    │
                        final result ──▶ returned as SpeechResult ──▶ HA fires STT_END (unchanged path)
                                                                                 │
                                                                                 ▼
                                                              client.async_turn_action(turn_id,
                                                                "transcript", {text, is_final:true})
                                                                                 │
                                                                                 ▼
                                                                     em_turn_engine.py "transcript" action
                                                                                 │
                                                                                 ▼
                                                                    log.debug(...) — TERMINAL.
                                                          Not forwarded to the device (no such
                                                          wire message exists); not pushed to
                                                          `/api/events` by this plan (Q3).
```

The mic audio is not forked or duplicated: `stream` in
`async_process_audio_stream` *is* `CorrelatedMicStream`, the same object
already carrying this turn's audio to the pipeline. The Gemini entity reads
it once, forwarding chunks into the Live session as they arrive.

Both partial and final transcript calls flow through the same
`em_turn_engine.py` `transcript` action and dead-end there today regardless
of `is_final` — the controller has never had a way to send text to a device,
so "don't forward to the device" isn't new work, it's the existing shape.
What's new is that this plan gives the controller something to do with a
partial *once it arrives* (log it, below) rather than only storing the
eventual final on the turn.

## New/changed files

### `stt.py` (new)

A single `GeminiTranscribeEntity(SpeechToTextEntity)`, registered once per
config entry (see Q1) and added to `const.PLATFORMS`. Implements the
standard `SpeechToTextEntity` surface — `supported_languages`,
`supported_formats`, `supported_codecs`, `supported_bit_rates`,
`supported_sample_rates`, `supported_channels` — matching what the pipeline
already hands us: 16 kHz mono S16LE, the same format `audio_frame.py`'s
`MIC_PCM` frames carry end to end (`MIC_FRAME_BYTES = 2_560` — 80 ms at that
rate). The exact property names should be checked against
`homeassistant.components.stt` for the pinned `MIN_HA_VERSION` (`const.py:10`,
currently `2026.8.0`) at implementation time rather than assumed from this
doc — unlike the wire-format facts above, this is HA-core API surface we
haven't read source for yet.

`async_process_audio_stream(stream, metadata) -> SpeechResult`:
1. Open (or reuse — see Q4) a Gemini Live session via the `google-genai` SDK,
   `model="gemini-3.5-transcribe-live"`, configured for transcription-only
   output.
2. Run two concurrent tasks: one pumps each chunk read from `stream` into the
   session as it arrives; the other reads interim/final transcription events
   back out.
3. On every interim event: if `isinstance(stream, CorrelatedMicStream)`, call
   `stream.on_partial(text)`. If the pipeline ever hands us a plain iterator
   instead (a direct unit-test call, or a future non-EchoMuse caller), the
   `isinstance` check is what makes that a no-op rather than an `AttributeError`.
4. When `stream` is exhausted (HA's own segmenter decided speech ended),
   signal end-of-input to the Live session, wait for its terminal result, and
   return it as the one `SpeechResult` — this is what `STT_END` reports,
   unchanged from any other provider's shape.

### `assist_satellite.py` (changed)

- `_run_wake_pipeline` (`assist_satellite.py:243-284`) wraps the audio
  generator before handing it to the base class:
  ```python
  await super().async_accept_pipeline_from_satellite(
      CorrelatedMicStream(channel.mic_frames(), on_partial=self._on_stt_partial)
  )
  ```
- New `_on_stt_partial(self, text: str) -> None`, following the exact
  ownership-token pattern every other async callback in this file already
  uses (`_owns_turn`, `assist_satellite.py:236-241`) — a partial arriving
  after a barge-in cancel or a new turn must not touch the wrong turn's
  state, same reasoning as `_async_pipeline_event`'s early-return guards:
  ```python
  def _on_stt_partial(self, text: str) -> None:
      turn_id, token, channel = self._active_turn_id, self._active_turn_token, self._active_channel
      if turn_id is None or token is None or not self._owns_turn(turn_id, token, channel):
          return
      self.hass.async_create_task(
          self.client.async_turn_action(turn_id, "transcript", {"text": text, "is_final": False})
      )
  ```
- `_async_pipeline_event`'s existing `STT_END` handling
  (`assist_satellite.py:367-376`) is untouched — it still sends the one
  `is_final: True` call. `_transcript_sent` (used there to guard against a
  double send) needs no change; it only ever gated the final.

### `const.py` / `manifest.json` (changed)

- `const.PLATFORMS` gains `"stt"`.
- `manifest.json`'s `requirements` (currently just `["aiohttp>=3.9.0"]`)
  gains a pinned `google-genai` version — the first non-`aiohttp` dependency
  this integration has ever needed, worth flagging in review since every
  other design decision in this package has deliberately kept its footprint
  to what `em_api.py`/`em_turn_engine.py` already expose.

### `config_flow.py` (changed)

A Gemini API key is a second credential, unrelated to the controller's own
`CONF_API_KEY` (`client.py:11-16` — that one authenticates *to the
controller*; this one authenticates *to Google*). Given this is opt-in
(see "Decisions locked in"), it belongs in an **options flow** rather than
the required `async_step_user` form (`config_flow.py:32-60`) — a user who
never turns this on shouldn't be asked for a Gemini credential to install
the integration at all.

## Controller-side changes

Everything below is independent of the HA-side work — it's what makes
`is_final` (already on the wire, already accepted by
`em_turn_engine.py:352-362`) actually mean something once it starts
arriving as `False` some of the time.

- **Gate `transcript_mono` on `is_final`.** Line 360 today is
  `turn.transcript_mono = turn.transcript_mono or time.monotonic()` — first
  write wins, which was harmless when there was only ever one write. Once
  partials arrive first, this stat (`stt_latency_ms` in `_turn_record`,
  meant to measure time-to-*final*-transcript) silently starts measuring
  time-to-first-partial instead, with no error and no test failure. Needs:
  ```python
  if body.get("is_final", True):
      turn.transcript_mono = turn.transcript_mono or time.monotonic()
  ```
- **Decide `on_transcript`'s semantics for partials.** Its one live consumer
  is `_run_timer_speech_turn` in `em_controller.py:2036-2054` — dismissing a
  timer alarm as soon as recognizable speech arrives. Firing it on the first
  partial rather than waiting for the final is plausibly strictly better
  (faster dismiss, and the check is just "did any text arrive," not
  content-sensitive), but it should be a deliberate choice recorded here
  rather than an accidental side effect of partials existing.
  `test_transcript_callback_only_runs_for_recognized_speech`
  (`tests/test_phase1_turn_engine.py:193`) currently doesn't distinguish
  `is_final` at all and needs a case added either way.
- **`turn.stt_text` persistence is unaffected.** It's whatever the last
  write was, and HA still always sends one final `is_final: True` call at
  the end of every turn — partials preceding it don't change what lands in
  the `turns` table.
- **Log each partial, at DEBUG, on arrival.** This is the "logged on the
  controller side" requirement — implemented in the `transcript` action
  itself (`em_turn_engine.py:352-362`), right where `text` is already in
  scope:
  ```python
  elif action == "transcript":
      ...
      if isinstance(text, str) and text:
          is_final = body.get("is_final", True)
          if is_final:
              turn.transcript_mono = turn.transcript_mono or time.monotonic()
          else:
              log.debug("[%s] partial transcript text=%r", turn.device.device_id, text)
          turn.stt_text = text
          ...
  ```
  Two choices here are load-bearing, not decoration:
  - **`log.debug`, not `log.info`.** `DEBUG` defaults off (`em_controller.py:98`,
    `logging.INFO` unless the `DEBUG` env var/add-on option is set) — so under
    normal operation this line is never emitted at all, never reaches
    `LogRing`, and never counts against the ~38 lines/min baseline
    `LogRing`'s capacity was sized against (`em_support.py:195-201`). A voice
    turn now producing 2-3+ log lines instead of 0 is exactly the kind of
    volume change that rate was measured against, and INFO would silently
    shrink the multi-hour window a support bundle's log tail is supposed to
    cover for *every* other kind of event too. DEBUG also happens to be the
    right audience: someone who has turned it on is already looking for
    exactly this kind of per-event detail.
  - **The message literally contains `text=`.** That's already one of
    `em_support.py`'s `_LOG_DROP` markers (`"STT result"`, `"text="`,
    `"Utterance saved"`, `"stt_text"`) — so `sanitise_log` drops this line
    whole at bundle-export time with no new code, the same "drop entirely,
    never partially redact" rule CLAUDE.md states for transcript-bearing
    lines. This needs pinning by test (`test_transcript_bearing_log_lines_are_dropped_whole`
    in `tests/test_support.py:194` is the existing coverage for the pattern;
    it should gain a case for this exact line shape) rather than trusted by
    construction, since the whole reason `_LOG_DROP` matches on substrings
    instead of call sites is that a differently-worded log line is exactly
    how this kind of thing has leaked before.

  Worth flagging while in this code: `_run_timer_speech_turn`'s existing
  `on_transcript` callback (`em_controller.py:2036-2054`) already logs
  transcript text today, at **INFO**, unconditionally —
  `log.info("[%s] Timer alarm dismissed by STT transcript: %r", device.device_id, text)`
  — and that line matches **none** of `_LOG_DROP`'s markers ("STT
  transcript:" is not "STT result", not "text=", not "stt_text"). That
  appears to be a live, pre-existing gap in the same invariant this plan is
  trying to uphold for partials, found while grounding this section rather
  than something this plan introduces — worth a fix (drop to DEBUG, or add
  a matching `_LOG_DROP` marker) in the same change, since after this plan
  ships, `em_controller.py` and `em_turn_engine.py` will sit right next to
  each other doing the *opposite* thing with the same category of data.
- **A dashboard live-caption view is out of scope for this plan.** Nothing
  here proposes a new `/api/events` push (e.g. `turn.transcript_partial`) —
  that's a genuinely separate decision (see Q3) with its own privacy bar to
  clear, not a natural consequence of partials merely existing on the
  controller.

## Privacy

Partial transcripts are speech from inside someone's home, same as the
final — CLAUDE.md's support-bundle rule ("No speech. Ever.") and
`em_support.py`'s `_LOG_DROP` list (`"STT result"`, `"text="`, `"Utterance
saved"`, `"stt_text"`) exist because of exactly this content. That coverage
is controller-side, and it's *why* the controller is where this plan allows
logging partials at all (the DEBUG-level, `text=`-tagged line above) — a
line dropped whole at bundle-export time isn't the same as a line never
written, but it's the same trust level `turns.stt_text` already sits at
today (persisted in plaintext, admin-only-gated, excluded from every bundle
allowlist).

None of that coverage reaches the new HA-side code — `stt.py` runs inside
Home Assistant, logging to HA's own log, which the controller's
support-bundle machinery has no visibility into or control over at all. The
rule for that file has to be stated independently, and it's stricter:
**partial and final text must never be logged at any level in `stt.py` or
`assist_satellite.py`'s new code**, not even at DEBUG — there is no
redaction layer on the HA side to catch it if it leaks there, so the only
safe rule is "never," full stop.

## Sequencing

1. Ship `stt.py` behind the options-flow opt-in, `const.PLATFORMS`,
   `manifest.json` dependency — no controller change required for this step
   to be inert (the field it would populate, `is_final: False`, is silently
   accepted and currently only affects `turn.stt_text`, which last-write-wins
   makes harmless either way).
2. Ship the `em_turn_engine.py` `is_final` gate and the `on_transcript`
   decision, independently, since they're correct regardless of whether any
   device has Gemini enabled yet (a fleet with zero partials still exercises
   `is_final: True` through the same gated code path).
3. Anything dashboard-facing (Q3) is a separate, later decision.

## Testing

- `hacs/tests` fakes Home Assistant at the module level rather than
  installing it (`conftest.py`) — `homeassistant.components.stt` isn't
  stubbed there yet and will need a minimal `SpeechToTextEntity`/
  `SpeechResult`/`SpeechResultState` fake added, the same pattern as the
  existing `pipeline.PipelineEventType`/`PipelineEvent` stub
  (`conftest.py:105-109`).
- A fake Gemini Live client (a canned interim/interim/final sequence, mirroring
  the GCP walkthrough this plan grew out of) stands in for the real SDK,
  the same role `test_stop_cancellation.py`/`test_assist_satellite.py`'s
  fake `PipelineEvent` objects play for the pipeline today.
- `test_assist_satellite.py` gains a case asserting `_on_stt_partial` is a
  no-op once `_owns_turn` fails (barge-in mid-partial), mirroring the
  existing `test_late_pipeline_event_cannot_touch_a_replaced_turn` coverage
  for `_async_pipeline_event`.
- `tests/test_phase1_turn_engine.py` gains an `is_final: False` case
  asserting `transcript_mono` is *not* set by it, plus the existing
  `is_final: True` case still setting it.

## Open questions

- **Q1 — one STT entity per config entry, or one per device?** This plan
  assumes a single shared entity, correlated per-turn via
  `CorrelatedMicStream` rather than per-device registration, to avoid one
  idle Gemini Live connection per device in a multi-device fleet. Revisit if
  Live API sessions turn out to be cheap enough to hold per-device
  permanently (see Q4).
- **Q2 — options-flow toggle only, or also a per-device switch entity** (the
  same shape as `switch.py`'s `EchoMuteSwitch`) so a fleet could run Gemini
  on some devices and the previously-configured provider on others? HA's
  pipeline STT engine is a pipeline-level setting, not per-satellite, so this
  may not be expressible without multiple HA pipelines — needs checking
  against how `AssistSatelliteConfiguration`/pipeline assignment actually
  works before promising it.
- **Q3 — is a dashboard live-caption view actually the goal, or is lower
  reaction latency for something else (e.g. a future stopword/barge-in
  content check, per `em_runbarrier.py`'s "kept because it may be needed
  again") the real motivation?** This plan deliberately doesn't assume an
  answer — the earlier options discussion speculated the caption use case,
  but nothing here has confirmed it, and the two have different privacy
  postures (a live display is a new admin-only surface; a purely internal
  latency signal is not).
- **Q4 — session lifecycle.** The Live API is WebSocket-based per Google's
  docs; a fresh connection per turn pays a handshake `write to first partial`
  is time nobody in this fleet has, versus keeping one warm and resetting
  its transcription context between turns. Needs measuring once built, the
  same way `DefaultOptions`' thread count was measured rather than assumed
  for the on-device wake-word inferer.
- **Q5 — exact request/response shape against the `google-genai` SDK.** This
  doc describes the architecture, not the literal SDK calls; the precise
  config for transcription-only Live sessions (as opposed to full
  multimodal chat) needs reading from `ai.google.dev/gemini-api/docs/live-api/live-transcribe`
  at implementation time.
