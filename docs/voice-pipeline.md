# The Voice Pipeline, Explained

EchoMuse uses Amazon's Audio Front End (AFE) as its production device audio
path. The Dot captures and plays audio through paired OpenSL ES endpoints;
Amazon's HAL processes the microphone signal, while the device itself owns
wake word and stopword detection and the controller and Home Assistant own
conversation handling.

```text
YOUR VOICE
    |
OpenSL ES VOICE_RECOGNITION capture (system UID)
    |
Amazon AFE / HAL -> continuous mono audio -> device-side wake word detector
    |
wake request/grant -> EchoMuse turn engine -> Home Assistant Assist
    |
TTS PCM -> controller output shaping -> OpenSL ES playback -> speaker
```

## On The Dot

The system-UID AFE helper opens the OpenSL recorder and player together. This
keeps capture and playback on the Android audio route, allowing Amazon's HAL to
apply its own microphone processing and playback reference handling. EchoMuse
does not use a local beamformer, raw ALSA capture/playback, SpeexDSP echo
cancellation, fixed mic gain, or local AGC in the production path.

The device scores its own mic stream continuously with openWakeWord models
running locally: the wake phrase, the stopword during active turns, and
near-miss capture selection for opted-in training. No idle microphone audio
leaves the Dot. When the wake word crosses the threshold, the device asks the
controller for admission, which arbitrates between nearby Dots — first
eligible request wins — before any audio is sent.

Privacy mute remains hardware-authoritative: a muted Dot does not send useful
capture audio, regardless of controller state.

## On The Controller

The controller never receives idle PCM and never loads or runs a wake or stop
model. It admits or denies device wake requests, creates a turn before Home
Assistant is contacted, and only then grants the device permission to stream
audio. The HACS integration opens one authenticated audio WebSocket for
that turn, receives microphone PCM, drives Assist, and streams TTS PCM back as
it is generated.

The controller upsamples 24 kHz TTS to the device's 48 kHz mono playback
format, applies EQ/output shaping, and streams it immediately. It does not
wait for a complete response before playback begins.

## Speech-to-text via Gemini (HACS)

STT is *owned* by the HACS integration, not by an external Whisper/Cloud service. `hacs/custom_components/echo_voice_satellite/stt.py` registers one `stt.gemini_transcribe` entity per config entry (`GeminiTranscribeEntity`). Per turn it opens one `google-genai` `Live` session (`gemini-3.5-transcribe-live`, `LiveConnectConfig` with `AudioTranscriptionConfig` carrying `language_codes`/`mode`/`custom_vocabulary` from the options flow, `response_modalities=["TEXT"]`, `automatic_activity_detection` default = Gemini server VAD). The same session yields both interim `interim_input_transcription.text` (forwarded immediately as `POST /api/turns/{id}/transcript {is_final:false}`) and final `input_transcription.text` (returned as `SpeechResult` → `STT_END` → `is_final:true`). This is the only HA-level way to get partials — `assist_pipeline` has no partial plumbing.

Interims are correlated without a registry: `assist_satellite.py` wraps `channel.mic_frames()` as `CorrelatedMicStream(..., on_partial=_bound_partial_callback(turn_id,token,channel))` and publishes the callback via a `ContextVar` (`stt._partial_callback_var`) so it survives `assist_pipeline`’s `process_enhance_audio`/`_speech_to_text_stream` wrapping (which would otherwise turn the stream into a plain `async_generator` and make `isinstance` always `False`). The bound callback re-checks `_owns_turn` with the *captured* `(turn_id,token,channel)`, so a barge-in that replaces the turn drops stale interims. The controller’s `em_turn_engine.py` `transcript` action gates `transcript_mono`/`stt_latency_ms` on `is_final` and logs partials at `DEBUG text=` (whole-line dropped by `em_support._LOG_DROP` in support bundles); HA-side never logs transcript text at any level.

Configure the four Gemini knobs in HA → Settings → Devices → EchoMuse → Configure: `Gemini API key` (blank = off, `models.list()` validated → `invalid_gemini_key`), `Transcription mode` `VERBATIM`/`SMART`, `Custom vocabulary` (≤1000, `TextSelector(multiple)`), `Language codes` (BCP-47 `, ` list, `[]` → auto-detect). All are `vol.Optional` and read fresh per `async_process_audio_stream` call — no reload. Pick the provider per pipeline (STT engine dropdown → `Gemini Transcribe`, opt-in, global). Requires `google-genai>=2.22.0`; older `1.59.0` installs fall back to empty `AudioTranscriptionConfig` so turns still work (vocab ignored until upgraded). See `docs/design/hacs-stt-plan.md` (Implemented) for the full model, VAD, interim/final mapping, and privacy.

## Playback And Music

The device renderer has separate voice and music planes. A voice turn ducks
music rather than pausing it, preserving buffered non-seekable streams. The
renderer owns priming, saturation, gain ramps, flush/discard-until-EOS, and
measured playback completion. The dashboard clears speaking indicators only
after the device reports that playback has actually drained.

## Diagnostics

Raw ALSA tools under `device/tools/` remain useful for bench investigation of
the mic array, codec, and hardware routes. They are diagnostics only. Their
measurements must not be treated as production-path behaviour or used to add a
raw PCM fallback.
