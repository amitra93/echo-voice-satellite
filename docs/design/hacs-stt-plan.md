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
- **HA's pipeline shape is unmodified for the events this integration
  consumes — but HA's own VAD is not depended on for anything, and that
  needs an explicit property override, not just an assumption.**
  `RUN_START → STT_START → STT_END → INTENT_* → TTS_* → RUN_END` looks
  exactly as it does today; from the pipeline's perspective this is just
  another `stt` platform implementing the standard contract. Concretely:
  `GeminiTranscribeEntity.audio_processing` must return
  `requires_external_vad=False` (see `stt.py` below) — that's the actual
  mechanism `assist_pipeline/pipeline.py` uses to decide whether to wrap our
  audio in its own local `VoiceCommandSegmenter` before we ever see it, and
  its default is `True`. Left unoverridden, this plan's entire "Gemini's own
  Automatic VAD, not HA's" design would be true of the config sent to
  Gemini and false of the audio Gemini actually received. One honest
  consequence of setting it correctly: `STT_VAD_START`/`STT_VAD_END` won't
  fire for turns using this provider (they're emitted from inside the code
  path that override disables) — harmless, since nothing here consumes
  them, but a real, narrow asterisk on "unmodified," stated precisely rather
  than left as the stronger claim.
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
- **One shared `GeminiTranscribeEntity` per config entry, not one per
  device.** Registered once, correlated per-turn via `CorrelatedMicStream`
  rather than a per-device HA pipeline. Avoids one idle Gemini Live
  connection per device on a multi-device fleet, and avoids asking a fleet
  owner to stand up a separate HA pipeline per satellite just to pick an STT
  engine.
- **Provider selection is global, via the options-flow credential alone —
  no per-device switch entity.** Whichever HA pipeline(s) select our entity
  as their STT engine get Gemini; this integration exposes no additional
  per-device toggle for it. Simpler, and sidesteps needing to verify whether
  `AssistSatelliteConfiguration`/pipeline assignment even supports
  per-satellite pipelines — a real per-device need can reopen this later,
  but nothing here is built assuming one exists.
- **A fresh Gemini Live session opens per turn and closes at turn end —
  permanently, not as an interim simplification to revisit.** Turns run for
  seconds; Google's own published limit is a **10-minute cap on continuous
  streaming per session**
  (`ai.google.dev/gemini-api/docs/live-api/live-transcribe`, "Limitations"),
  so a session reused indefinitely across turns would eventually need its
  own reset/rotation logic to avoid hitting that ceiling — complexity a
  fresh-session-per-turn design never has to build. The per-turn connection
  handshake cost is accepted as the price of that simplicity, not something
  planned to be measured and reconsidered later.
- **VAD strategy: Automatic, not Hybrid or Manual/push-to-talk — correcting
  an earlier draft of this plan.** `automatic_activity_detection` is left at
  its default (enabled, unconfigured) — Gemini's own server-side VAD owns
  BOTH speech start and speech end detection entirely on its own, from the
  raw audio alone. This plan does not send any client-side signal to mark
  where speech ends, and does not rely on HA's pipeline VAD, the controller,
  or the device to have already bracketed the audio to a single utterance —
  `async_process_audio_stream` treats Gemini's own `input_transcription`
  (final) event as the sole signal that a turn's speech is complete,
  independent of anything upstream. This is a correction, not a refinement:
  an earlier draft assumed Hybrid VAD (server-side start detection plus a
  client-sent `audio_stream_end` timed off HA's segmenter finishing) was the
  right fit, which depended on an assumption about HA's pipeline VAD that
  doesn't hold for how audio actually reaches this integration — a
  continuous stream for the life of the turn, with no upstream component
  this plan can or should treat as having already found the speech boundary.
  Manual/push-to-talk (`automatic_activity_detection.disabled=True` plus
  explicit `activity_start`/`activity_end`) remains rejected for the reason
  it always was — it would require *us* to decide and signal exactly when
  speech starts, a job Automatic VAD already does correctly (with prefix
  padding against front-word truncation) without needing a signal from
  anyone. `audio_stream_end` is still sent, but only as a transport-teardown
  courtesy if the underlying mic stream itself ends before Gemini has
  already found a final — see the `stt.py` implementation below — never as
  a speech-end signal in its own right.

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
                                                            .async_process_audio_stream(metadata, stream)
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
                                                          `/api/events` by this plan.
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
config entry and added to `const.PLATFORMS`. Implements the standard
`SpeechToTextEntity` surface — `supported_languages`, `supported_formats`,
`supported_codecs`, `supported_bit_rates`, `supported_sample_rates`,
`supported_channels`, plus one more property that turns out to be
load-bearing (`audio_processing`, below). Sourced against HA core directly
(`developers.home-assistant.io/docs/core/entity/stt/` and
`homeassistant/components/stt/{__init__,models}.py`, pinned `MIN_HA_VERSION`
`2026.8.0` — `const.py:10`), not assumed. What those format properties need
to *declare* is confirmed against Google's docs too: Live transcription input
is "raw 16-bit PCM audio" at "16kHz (mono, little-endian)", sent as
`audio/pcm;rate=16000` — exactly the format `audio_frame.py`'s `MIC_PCM`
frames already carry end to end (`MIC_FRAME_BYTES = 2_560` — 80 ms at that
rate, 1,280 samples). Google's recommended chunk size is 100 ms
(1,024–2,048 samples); our native 80 ms/1,280 sample cadence sits inside
that range, so no rechunking or resampling is needed anywhere in this path.

**`supported_languages` needs to be broad, not narrow — a gate fires before
Gemini is ever contacted.** `SpeechToTextEntity.check_metadata()` (HA core)
rejects the whole call with `stt-provider-unsupported-metadata` if
`metadata.language not in self.supported_languages`, and this runs *before*
`async_process_audio_stream` — a narrow list here would spuriously break the
integration for any pipeline configured with a language outside it, entirely
independent of anything in our own options flow. `supported_languages`
should therefore return the full BCP-47 code list from Google's "Supported
languages" table (`ai.google.dev/gemini-api/docs/live-api/live-transcribe`,
~100 entries), not just whatever's in `CONF_LANGUAGE_CODES` — those are two
different lists doing two different jobs: this one is "what HA is allowed to
ask for," `CONF_LANGUAGE_CODES` is "what we actually tell Gemini to expect."

**`audio_processing` is the property that makes "HA's VAD is not depended
on" true, and it needs an explicit override — this plan's earlier drafts
never set it.** Tracing `assist_pipeline/pipeline.py` directly: `stt_vad`
(a `VoiceCommandSegmenter` — HA's own local VAD) is only constructed, and
only wraps the audio stream we receive, when `self.stt_provider.audio_processing.requires_external_vad`
is `True`:
```python
stt_vad: VoiceCommandSegmenter | None = None
if (
    self.audio_settings.is_vad_enabled
    and self.stt_provider.audio_processing.requires_external_vad
):
    stt_vad = VoiceCommandSegmenter(silence_seconds=self.audio_settings.silence_seconds)
result = await self.stt_provider.async_process_audio_stream(
    metadata, self._speech_to_text_stream(audio_stream=stream, stt_vad=stt_vad),
)
```
The base class's default, `SpeechToTextEntity.audio_processing`, returns
`DEFAULT_AUDIO_PROCESSING`, which has **`requires_external_vad=True`**. Left
unoverridden, `GeminiTranscribeEntity` would silently get exactly the
`VoiceCommandSegmenter`-cut stream this plan says it doesn't depend on — the
"Automatic VAD" decision would be true of the Gemini config sent, and false
of what audio Gemini actually receives, which is precisely the "reports one
thing, does another" shape this codebase treats as a bug class rather than a
detail (the ambient-light 0-vs-nil case, the media-player IDLE-vs-truth
case). So `stt.py` must declare:
```python
from homeassistant.components.stt import SpeechAudioProcessing

@property
def audio_processing(self) -> SpeechAudioProcessing:
    return SpeechAudioProcessing(
        requires_external_vad=False,
        prefers_auto_gain_enabled=False,
        prefers_noise_reduction_enabled=False,
    )
```
`requires_external_vad=False` is the one that matters; the other two fields
are preferences about input signal conditioning that don't apply here — the
audio arriving from the device has already been through Amazon's AFE (see
CLAUDE.md's "Device audio pipeline"), so there's nothing this integration
should ask HA to do to it. With this set, `stt_vad` stays `None` regardless
of `self.audio_settings.is_vad_enabled`, `_speech_to_text_stream` yields
every chunk with no early cutoff, and `stream` really is the continuous,
undifferentiated audio the rest of this section already assumed it was.

**One honest consequence: `STT_VAD_START`/`STT_VAD_END` simply won't fire
for turns using this provider.** Those events are emitted from inside the
`if stt_vad is not None:` branch of `_speech_to_text_stream`, which never
runs once `requires_external_vad=False`. Harmless here — nothing in
`assist_satellite.py` consumes them (`_async_pipeline_event` only handles
`STT_END`/`INTENT_END`/`TTS_END`/`ERROR`) — but it's a real, if narrow,
asterisk on the earlier "HA's pipeline shape is unmodified" claim, worth
stating precisely rather than leaving the stronger version standing.

`async_process_audio_stream(metadata, stream) -> SpeechResult` — **metadata
first, stream second**, per the real abstract method signature
(`SpeechToTextEntity.async_process_audio_stream`); an earlier draft of this
plan had the arguments reversed throughout. Against the real `google-genai`
SDK surface (`ai.google.dev/gemini-api/docs/live-api/live-transcribe`):

```python
from google import genai
from google.genai import types

def _parse_language_codes(raw: str) -> list[str]:
    """Comma-separated BCP-47 codes as the user typed them, e.g.
    "en-US, es-ES" -> ["en-US", "es-ES"]. Empty/unset -> [] (auto-detect),
    same as leaving `language_codes` off entirely per Gemini's own default.
    No validation against Google's supported-language list — a typo'd code
    is Gemini's error to report at connect time, not ours to pre-guess.
    """
    return [code.strip() for code in raw.split(",") if code.strip()]


async def async_process_audio_stream(self, metadata, stream) -> SpeechResult:
    # Read fresh per call, not cached at __init__ — see "Reconfiguring after
    # setup" below for why this is the whole mechanism that makes the
    # options flow's changes take effect without a reload.
    options = self._entry.options
    config = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=_parse_language_codes(options.get(CONF_LANGUAGE_CODES, "")),
            mode=options.get(CONF_TRANSCRIPTION_MODE, "VERBATIM"),
            custom_vocabulary=options.get(CONF_CUSTOM_VOCABULARY, []),
        ),
        # automatic_activity_detection left unconfigured (Automatic VAD,
        # the default) — Gemini's own server-side detector owns both speech
        # start and speech end. See "Decisions locked in": HA's pipeline VAD
        # is not depended on for anything here.
    )
    # .get(), not options[...]: an unset key must fail at Gemini's own
    # auth step (a clear, Gemini-reported error) rather than as a bare
    # Python KeyError before a connection is even attempted — see
    # "config_flow.py (changed)" below for what "unset or wrong" means here.
    client = genai.Client(api_key=options.get(CONF_GEMINI_API_KEY, ""))
    final_text = ""

    async with client.aio.live.connect(
        model="gemini-3.5-transcribe-live", config=config,
    ) as session:
        async def _pump():
            async for chunk in stream:
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )
            # The underlying mic stream itself ended (turn teardown,
            # barge-in) before Gemini had already found a final — a
            # transport courtesy so the receive loop below can't wait
            # forever. This is NOT how speech-end is normally detected;
            # that's Automatic VAD's job, entirely server-side.
            await session.send_realtime_input(audio_stream_end=True)

        pump_task = asyncio.create_task(_pump())
        try:
            async for response in session.receive():
                content = response.server_content
                if not content:
                    continue
                if content.interim_input_transcription:
                    text = content.interim_input_transcription.text
                    if isinstance(stream, CorrelatedMicStream):
                        stream.on_partial(text)
                if content.input_transcription:
                    final_text = content.input_transcription.text
                    # Gemini's own Automatic VAD decided the turn's speech
                    # is complete — this call is done regardless of whether
                    # `stream` still has more audio to give.
                    break
        finally:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task

    return SpeechResult(final_text, SpeechResultState.SUCCESS)
```

Two structural details worth calling out, both consequences of not waiting
on `stream` to end:

- **The receive loop `break`s on the final, it doesn't wait for both tasks to
  finish.** An earlier draft used `asyncio.gather(_pump(), _receive())`,
  which only returns once *both* coroutines complete — but `_pump()` only
  completes when `stream` itself is exhausted, and under Automatic VAD this
  call is done as soon as Gemini reports `input_transcription`, which can
  happen well before that. Waiting on `stream` to end anyway would have
  quietly reintroduced a dependency on something upstream deciding when
  speech is over — precisely what this VAD strategy is supposed to avoid.
- **`pump_task` is explicitly cancelled once the receive loop exits**,
  whether that's via the `break` above or an exception. Once Gemini has
  given a final, there's no reason to keep forwarding whatever audio
  `stream` still has left into a session this method is about to close.

**`metadata.language` — what it actually is, and why this plan ignores it.**
Tracing `assist_pipeline/pipeline.py`: `metadata.language =
self.pipeline.stt_language or self.language` — the STT language configured
on *that specific HA Assist pipeline* (Settings → Voice Assistants → a
pipeline's own STT-language field), falling back to the pipeline's general
language if that's unset. It's a per-pipeline HA setting, checked by
`check_metadata` before our entity is ever called (see
`supported_languages`, above) and otherwise available to read inside
`async_process_audio_stream` if a provider wants it. This plan doesn't:
`language_codes` comes from `CONF_LANGUAGE_CODES` in the options flow
instead (see "const.py" below), a value the user types explicitly into
*this* integration's own form. Two independent "language" settings can
therefore disagree — HA's pipeline could say `"en"` while our options flow
says `"es-ES, fr-FR"` — and that's accepted rather than reconciled: making
them agree would mean either overriding what the user typed here with a
different setting from a different screen, or the reverse, and there's no
version of that which isn't surprising to whichever screen loses. `metadata`
is still passed through to `check_metadata`'s gate (hence the broad
`supported_languages` above), just not read inside this method.

The `isinstance(stream, CorrelatedMicStream)` check is what makes a plain
iterator (a direct unit-test call, or a future non-EchoMuse caller) a no-op
rather than an `AttributeError` — nothing here assumes every caller wraps its
stream.

**Two response fields, not one, doing exactly the job this plan needs:**
Google's Live API already distinguishes interim from final at the SDK level
— `server_content.interim_input_transcription.text` ("low-latency,
speculative partial hypotheses... emitted continuously while the speaker is
actively talking") versus `server_content.input_transcription.text` ("the
finalized transcript emitted when the speaker pauses, the turn completes, or
speech is finalized"). We don't need to invent our own interim/final
distinction on top of Gemini's output — `interim_input_transcription` maps
directly to `stream.on_partial`, `input_transcription` maps directly to the
one `SpeechResult` this method returns, and HA's own `STT_END`/`is_final`
plumbing (already built, see "Motivation") is what turns that into the
existing `transcript {is_final:true}` call.

**Auth: a standard long-lived API key, not ephemeral tokens.** Google's docs
describe ephemeral tokens as being for "client-to-server applications (such
as mobile or web apps streaming directly from a microphone)... to avoid
exposing your API key in client code." That describes a browser or phone
talking to Gemini directly — not this integration, which runs server-side
inside Home Assistant and never exposes its credential to anything less
trusted than HA's own config entry storage (the same trust level the
controller's own `CONF_API_KEY` already sits at). `genai.Client(api_key=...)`
with the config-entry-stored Gemini key is the correct, documented shape;
building the ephemeral-token flow here would be solving a problem this
integration doesn't have. `genai.Client(...)` construction itself does no
network I/O — the connection cost is entirely in `.aio.live.connect()` — so
constructing it fresh per call (above) costs nothing extra; it's what lets a
rotated key take effect on the very next turn with no separate cache to
invalidate.

**Three transcription knobs plus the API key, all reconfigurable after
setup — see "config_flow.py (changed)" below.** `mode` on
`AudioTranscriptionConfig` is `"VERBATIM"` (exact literal transcript, filler
words and all) or `"SMART"` (removes disfluencies, resolves
self-corrections, applies formatting); `"VERBATIM"` is the shipped default
because SMART changes *which words* HA's intent recognizer sees, and that's
a real tradeoff a user should opt into deliberately rather than inherit
silently. `custom_vocabulary` (up to 1,000 terms, "best results... with up
to 100" per Google's docs) defaults to an empty list — HA already knows
every area and entity name in the house, a natural future source list, but
seeding it automatically is a separate, later decision; for now the user
supplies it by hand. `language_codes` defaults to unset (Gemini's own
auto-detection across 85+ languages) and, when set, is exactly the codes
the user typed — see `_parse_language_codes` below. None of these three are
hardcoded — they, plus the Gemini API key itself, are exactly the four
fields the options flow below exists to hold and let a user revisit.

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
- Four new `CONF_*` keys, alongside the existing `CONF_URL`/`CONF_API_KEY`
  (`const.py:12-13`) — deliberately named distinctly from `CONF_API_KEY` so a
  diff or a log line can never confuse the controller credential with the
  Gemini one:

```python
CONF_GEMINI_API_KEY = "gemini_api_key"
CONF_CUSTOM_VOCABULARY = "gemini_custom_vocabulary"
CONF_TRANSCRIPTION_MODE = "gemini_transcription_mode"
CONF_LANGUAGE_CODES = "gemini_language_codes"
```

`CONF_TRANSCRIPTION_MODE`'s stored values are Gemini's own strings,
`"VERBATIM"`/`"SMART"`, not a separate EchoMuse vocabulary translated at the
boundary — one fewer place for the two to drift out of sync, and one fewer
lookup table to keep correct. `CONF_LANGUAGE_CODES` is stored as the raw
string the user types — a comma-separated list of BCP-47 codes (e.g.
`"en-US, es-ES"`) — not pre-split into a list; `_parse_language_codes` (in
`stt.py`, above) does that at read time, so the stored form is exactly what
the options-flow text field shows back to the user on the next visit.

### `config_flow.py` (changed)

A Gemini API key is a second credential, unrelated to the controller's own
`CONF_API_KEY` (`client.py:11-16` — that one authenticates *to the
controller*; this one authenticates *to Google*). Given this is opt-in
(see "Decisions locked in"), it — and the three other Gemini-specific knobs,
custom vocabulary, transcription mode, and language codes — belong in an
**options flow** rather than the required `async_step_user` form
(`config_flow.py:32-60`): a user who never turns this on shouldn't be asked
for a Gemini credential to install the integration at all, and options-flow
settings are exactly HA's existing mechanism for "revisit this later" (the
`Configure` button on an installed integration), which is what makes all
four actually reconfigurable rather than fixed at install time.

This is the **first** options flow this integration has ever needed —
`config_flow.py` today only has the required `async_step_user` step, no
`OptionsFlow` class at all. New class, registered on the existing
`EchoVoiceSatelliteConfigFlow`:

```python
class EchoVoiceSatelliteOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        current = self.config_entry.options
        if user_input is not None:
            api_key = user_input.get(CONF_GEMINI_API_KEY, "")
            if api_key:
                try:
                    await _async_validate_gemini_key(api_key)
                except GeminiAuthError:
                    errors["base"] = "invalid_gemini_key"
            if not errors:
                return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_GEMINI_API_KEY, default=current.get(CONF_GEMINI_API_KEY, ""),
                ): str,
                vol.Optional(
                    CONF_CUSTOM_VOCABULARY, default=current.get(CONF_CUSTOM_VOCABULARY, []),
                ): selector.TextSelector(selector.TextSelectorConfig(multiple=True)),
                vol.Optional(
                    CONF_TRANSCRIPTION_MODE,
                    default=current.get(CONF_TRANSCRIPTION_MODE, "VERBATIM"),
                ): vol.In(["VERBATIM", "SMART"]),
                vol.Optional(
                    CONF_LANGUAGE_CODES, default=current.get(CONF_LANGUAGE_CODES, ""),
                ): str,
            }),
            errors=errors,
        )


class EchoVoiceSatelliteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    ...
    @staticmethod
    def async_get_options_flow(config_entry):
        return EchoVoiceSatelliteOptionsFlow()
```

A few things worth being deliberate about:

- **Blank API key is a valid, supported state — it's how Gemini gets turned
  back off.** `CONF_GEMINI_API_KEY` is `vol.Optional`, not `vol.Required`:
  clearing the field and saving is the reversible off switch, no need to
  remove and re-add the whole integration to back out.
- **Nothing works if the API key is missing or wrong, and that's the whole
  of the story — no special handling, just the documented behavior.** A
  blank or incorrect key means every voice turn on a pipeline configured to
  use this STT entity fails, for as long as that's true; there's no partial
  functionality and no silent fallback to a different provider. The failure
  happens at Gemini's own auth step inside `async_process_audio_stream` (the
  `.get()` in the `stt.py` code above, not a bracket lookup, is what lets it
  get there rather than raising a bare `KeyError` first) and surfaces the
  same way any other STT provider's failure would: it propagates out of
  `async_process_audio_stream`, HA's pipeline reports
  `PipelineEventType.ERROR`, and the existing `_async_pipeline_event` ERROR
  handling (`assist_satellite.py:407-412`, already built, no change needed)
  turns that into `pipeline-event {event: error}` to the controller. A user
  who hasn't set up a key correctly finds out the first time they try to use
  it, loudly, the same way they would with any other misconfigured STT
  provider — not a new failure mode this plan has to invent a story for.
- **The key is validated at save time, not discovered broken on the next
  voice turn.** This mirrors `async_step_user`'s existing pattern exactly —
  it probes `client.async_get_devices()` before accepting the controller URL
  (`config_flow.py:41-44`); `_async_validate_gemini_key` should do the
  equivalent lightweight check against Gemini before accepting a new key.
  The cheapest correct call for that check is an implementation-time detail
  to confirm against the SDK (this doc's Q5 resolution covers the streaming
  transcription call shape, not a minimal auth-check call) — something like
  a models-list call is the shape to look for, not a full Live session.
- **Custom vocabulary as a multi-value text selector**, not a comma-separated
  string parsed by hand — HA's `TextSelector(multiple=True)` renders as
  repeatable chips in the frontend and already returns a `list[str]`, so
  there's no delimiter-escaping bug class to introduce (a vocabulary term
  containing a comma would corrupt a hand-parsed CSV field). Worth a soft
  warning, not a hard block, past Google's "best results... with up to 100"
  guidance — 1,000 is the hard API ceiling and should be enforced, 100 is a
  quality recommendation and shouldn't stop someone from saving 150.
- **No config-entry reload, no update listener, needed for any of this to
  take effect.** `stt.py`'s `async_process_audio_stream` reads
  `self._entry.options` fresh at the top of every call (above) rather than
  caching it at construction — so a change saved through this flow applies
  starting with the very next turn, and a turn already in flight when the
  save happens finishes with whatever config it started with rather than
  being disrupted mid-turn. This is the same shape as the device-link TLS
  credential ("re-read on every dial... so a push takes effect on the next
  reconnect, no restart," CLAUDE.md), applied to a config entry instead of a
  file on disk. It also means this integration's options flow doesn't need
  the `entry.add_update_listener(...)` + `hass.config_entries.async_reload(...)`
  pattern common to other HA integrations at all — deliberately: a full
  platform reload would tear down `assist_satellite`, `stt`, and every other
  entity in this config entry, which is a far bigger disruption than
  "the next turn uses different Gemini settings" needs to cause.

The Gemini API key is a credential, not speech — the "no speech in logs"
rules elsewhere in this plan don't apply to it *because* of what it is, but
ordinary secret hygiene still does: it must never be logged, and it sits in
HA's config-entry storage at the same trust level the controller's own
`CONF_API_KEY` already does today. Nothing about this plan changes that
baseline; it's the same place a second secret was always going to live once
one existed.

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
  that's a genuinely separate, deliberately deferred decision (see "Open
  questions, resolved" below) with its own privacy bar to clear, not a
  natural consequence of partials merely existing on the controller.

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
3. Anything dashboard-facing is a separate, later decision (deliberately not
   scoped into this plan — see "Open questions, resolved").

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
- `test_config_flow.py` gains coverage for `EchoVoiceSatelliteOptionsFlow`,
  built the same way the existing `async_step_user` tests are
  (`test_config_flow.py:51-80` — `object.__new__` the flow, stub
  `async_create_entry`/`self.config_entry`, no real HA install needed):
  saving valid `{api_key, custom_vocabulary, mode, language_codes}` creates
  an options entry; an empty `custom_vocabulary`/default `mode`/blank
  `language_codes` round-trip correctly; a rejected
  `_async_validate_gemini_key` shows the form again with an error and
  creates nothing, mirroring
  `test_unreachable_controller_shows_cannot_connect_and_still_closes_client`;
  and a blank API key is accepted (the documented off switch) without
  attempting validation at all.
- `_parse_language_codes` gets its own small, pure-function test —
  `"en-US, es-ES"` → `["en-US", "es-ES"]`, `""` → `[]`, and a case with
  stray whitespace/trailing commas — the same "split the pure logic out and
  test it directly" pattern this codebase uses throughout (`em_button`,
  `em_linkauth`, and the rest of the pure decision functions CLAUDE.md's
  testing section lists).
- `stt.py`'s tests assert `async_process_audio_stream` reads
  `CONF_TRANSCRIPTION_MODE`/`CONF_CUSTOM_VOCABULARY`/`CONF_GEMINI_API_KEY`/
  `CONF_LANGUAGE_CODES` from `self._entry.options` **fresh on each call**,
  not once at construction — a fake entry whose `.options` dict is mutated
  between two calls should produce two different `LiveConnectConfig`s. This
  is the test that actually pins "reconfiguring takes effect on the next
  turn," not just an assertion that the options flow saves correctly.
- A case asserting a missing/blank `CONF_GEMINI_API_KEY` reaches Gemini's own
  connect/auth failure (or an equivalent fake-client rejection) rather than
  raising a `KeyError` out of `stt.py` before any connection is attempted —
  pins the `.get()` fix above.
- `test_stt.py` (new) asserts `GeminiTranscribeEntity.audio_processing.requires_external_vad
  is False`, directly, with a comment tying it back to
  `assist_pipeline/pipeline.py`'s `stt_vad` construction. This is the
  highest-value single assertion in this plan's test suite despite being
  the smallest: get it wrong and every other test can still pass while the
  live behavior silently reintroduces a dependency on HA's own VAD — the
  exact failure mode this section exists to rule out, and nothing else here
  would catch it.

## Open questions, resolved

All five questions this plan originally raised have been decided; kept here
with their reasoning rather than deleted, the same provenance-preserving
habit CLAUDE.md uses throughout this codebase for rejected alternatives.

- **Q1 — one STT entity per config entry, or one per device? Resolved: one
  shared entity per config entry.** Folded into "Decisions locked in" above.
  Correlated per-turn via `CorrelatedMicStream`, not per-device
  registration — avoids one idle Gemini Live connection per device on a
  multi-device fleet, and avoids requiring a separate HA pipeline per
  satellite.
- **Q2 — options-flow toggle only, or also a per-device switch entity?
  Resolved: options-flow toggle only, global.** Folded into "Decisions
  locked in" above. No per-device switch is built; picking Gemini for some
  devices and something else for others isn't offered by this integration,
  and this plan doesn't spend time verifying whether HA's pipeline
  assignment would even allow it — a real need can reopen the question later
  with an actual use case driving it, rather than building the mechanism
  speculatively.
- **Q3 — is a dashboard live-caption view the goal, or is this really about
  latency for something else (e.g. a future stopword/barge-in content
  check)? Resolved: neither, for now — no consumer beyond the controller-side
  log line is built by this plan.** The controller-side logging in
  "Controller-side changes" is the entire scope; no new `/api/events` push,
  no dashboard UI, and no attempt to wire partials into
  `em_runbarrier.py`-adjacent barge-in logic. Whichever of those turns out to
  be the real motivation becomes its own design doc once there's an actual
  driving need, not speculative scope carried by this one.
- **Q4 — open a fresh session per turn, or keep one warm across turns?
  Resolved: fresh per turn, permanently.** Folded into "Decisions locked in"
  above, now backed by a fact this plan didn't have when the question was
  first raised: Google's docs state Live transcription sessions cap out at
  **10 minutes of continuous streaming**. Turns here run for seconds, nowhere
  near that ceiling, but a session reused indefinitely *across* turns would
  eventually need its own rotation logic to avoid hitting it — complexity a
  fresh-session-per-turn design never has to build. The per-turn handshake
  cost is accepted outright rather than measured first.
- **Q5 — exact request/response shape against the `google-genai` SDK.
  Resolved: researched and folded into the `stt.py` section above.** The
  concrete calls (`types.LiveConnectConfig`, `session.send_realtime_input`,
  `session.receive()`, the `interim_input_transcription`/
  `input_transcription` response fields, the Automatic VAD configuration
  shape, and the auth-token-vs-API-key distinction) are now sourced from
  `ai.google.dev/gemini-api/docs/live-api/live-transcribe` rather than
  guessed at. What's still genuinely open at implementation time is only the
  exact `SpeechToTextEntity` property names for the pinned HA version — HA
  core API surface, not Gemini API surface — noted where it comes up in the
  `stt.py` section.
