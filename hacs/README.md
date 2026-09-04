# EchoMuse — Home Assistant integration

A HACS custom integration that adds your EchoMuse devices to Home Assistant
directly — as a native `assist_satellite` plus supporting entities — instead
of through HA's built-in ESPHome integration. See
[`docs/design/full-duplex-plan.md`](../docs/design/full-duplex-plan.md) for
the full design and why this replaced the ESPHome impersonation.

This is a standalone HACS custom repository. The integration is intentionally
not installed into the controller's own Python environment — it talks to the
controller purely over HTTP/WebSocket, the same way any other Home Assistant
integration talks to the device it manages.

## Install

1. In the EchoMuse dashboard, go to **Settings → Home Assistant Integration**
   and click **Generate Key**.
2. In Home Assistant, add this repository to HACS (or copy
   `custom_components/echo_voice_satellite` into your `custom_components/`
   directory) and install **EchoMuse**.
3. Add the integration, entering the controller's base URL
   (e.g. `http://192.168.1.50:8768`) and the API key from step 1.

## What you get, per device

- `assist_satellite.<label>_voice_assistant` — the voice pipeline; supports
  announcements.
- `switch` — privacy mute. It reflects device state and uses the device's
  toggle command only when the requested state differs, so the hardware
  button remains the underlying mute authority.
- Music Assistant owns the Sendspin media player and its volume/mute controls.
- `sensor` — firmware version, wake-word model, and (capability-gated)
  ambient light.
- `event` — the action button's single/double/triple/long gestures
  (capability-gated on `button_hold`).
- `select` — which Assist pipeline this device uses.
- A passive Bluetooth remote scanner, if the device's `bleProxyEnabled`
  config is on.
- The satellite registers itself as a native Home Assistant timer device
  (`HassStartTimer`/`HassPauseTimer`/etc. and Assist API LLM tools all work
  unmodified), so `set a 10 minute timer` just works. A finished timer rings
  a continuous chime on the originating Echo with no spoken confirmation in
  either direction — say `stop`, or tap the action button, to dismiss it. See
  [`docs/design/timers-design.md`](../docs/design/timers-design.md).

## Timers dashboard card

Add `echo-voice-timers-card` to any Lovelace dashboard to see and manage
every EchoMuse timer across the fleet — active, paused, and (unlike Home
Assistant's own timer entities) still visible and dismissable while
ringing. Backed by `timer_card.py`'s
`echo_voice_satellite/timers/*` WebSocket commands, not a second `timer.*`
entity per device. See
[`docs/design/timers-design.md`](../docs/design/timers-design.md#home-assistant-timer-dashboard)
for the data contract and
[`docs/design/timers-implementation-update.md`](../docs/design/timers-implementation-update.md)
for what's built.

## Speech-to-text via Gemini 3.5 Transcribe (Live)

The integration *is* the STT provider — no separate Whisper/Google Cloud STT needed. A single `stt.gemini_transcribe` entity (`stt.py` `GeminiTranscribeEntity`) is registered per config entry (`const.PLATFORMS` + `stt`) and does double duty for every turn:

- **One Live session per turn** (`genai.Client(api_key=options.get(CONF_GEMINI_API_KEY,""))` → `client.aio.live.connect(model="gemini-3.5-transcribe-live", config=LiveConnectConfig(...))`) streams the same 16 kHz mono `MIC_PCM` (80 ms, 1280 samples, `audio/pcm;rate=16000`) that already flows to HA — no resampling/rechunking, no shadow copy, no second STT call. `interim_input_transcription.text` → `CorrelatedMicStream.on_partial` → `client.async_turn_action(turn_id, "transcript", {is_final:false})`; `input_transcription.text` → `SpeechResult` → `STT_END` → `is_final:true`. This is the only way to get partials today: `assist_pipeline` only has `STT_START`/`VAD`/`STT_END` and `SpeechToTextEntity` only ever returns one `SpeechResult` — HA has no partial plumbing.

- **Correlated, not registered.** `assist_satellite.py` wraps `channel.mic_frames()` as `CorrelatedMicStream(channel.mic_frames(), on_partial=self._bound_partial_callback(turn_id,token,channel))` and publishes the callback via a `ContextVar` (`stt._partial_callback_var`) so it survives `assist_pipeline`’s `process_enhance_audio`/`_speech_to_text_stream` wrapping (which yields a new `async_generator`, so `isinstance(stream, CorrelatedMicStream)` would always be `False` after the enhancer). `_bound_partial_callback` captures `(turn_id,token,channel)` at wrap time and re-checks `_owns_turn` on every interim, so a barge-in that replaces the turn drops stale interims rather than misattributing them.

- **HA VAD is not used.** `GeminiTranscribeEntity.audio_processing` returns `SpeechAudioProcessing(requires_external_vad=False, prefers_auto_gain_enabled=False, prefers_noise_reduction_enabled=False)` — with `MIN_HA_VERSION 2026.8.0` this keeps `VoiceCommandSegmenter` out of the path and makes Gemini’s `Automatic` server VAD (unconfigured `automatic_activity_detection`) the sole start/end detector, with `audio_stream_end` only as a transport courtesy.

- **Configure in HA → Settings → Devices → EchoMuse → Configure** (options flow, not install flow): `Gemini API key` (blank = off, validated via `models.list()` → `invalid_gemini_key`), `Transcription mode` `VERBATIM` (default, literal) / `SMART` (disfluencies removed), `Custom vocabulary` up to 1000 terms (`TextSelector(multiple=True)`, hard `1000`, soft `100`), `Language codes` comma-separated BCP-47 (`_parse_language_codes`, `[]` → auto-detect 85+). All four are `vol.Optional` and read fresh per `async_process_audio_stream` call — a mid-flight save takes effect on the *next* turn, no `async_reload`/`add_update_listener` needed. Requires `google-genai>=2.22.0` (`AudioTranscriptionConfig` with `language_codes`/`mode`/`custom_vocabulary`); older `1.59.0` installs fall back to an empty `AudioTranscriptionConfig` (auto-detect, `VERBATIM`, no vocab) so turns still work.

- **Pick it:** HA → Settings → Voice Assistants → your pipeline → STT engine → `Gemini Transcribe` (opt-in, global per pipeline; no per-device switch). The controller stays provider-agnostic — it only ever sees `POST /api/turns/{id}/transcript {text,is_final}` and gates `stt_latency_ms` on `is_final` (final-only `transcript_mono`).

- **Privacy:** partials terminate at the controller as `DEBUG partial transcript text=` (dropped whole by `em_support._LOG_DROP` `text=` in bundles, never forwarded to the device or `/api/events`). **HA-side never logs transcript text at any level** (`stt.py`/`assist_satellite.py` new code) — there is no HA-side redaction layer to catch it.

## Full duplex, in this design

"Full duplex" means the audio transport is bidirectional and turn-agnostic:
the mic streams up continuously while a turn is open, and TTS/announcements
can be pushed down at any time — including mid-turn for barge-in. Home
Assistant's own Assist pipeline stays turn-based (STT → intent → TTS); that
is the realistic ceiling when Assist is the consumer.
