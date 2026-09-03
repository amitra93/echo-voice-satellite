# EchoMuse

[![Unit Tests](https://github.com/amitra93/echo-voice-satellite/actions/workflows/unit-test.yaml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/unit-test.yaml)
[![Controller Build](https://github.com/amitra93/echo-voice-satellite/actions/workflows/controller-build.yml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/controller-build.yml)
[![Forge Build](https://github.com/amitra93/echo-voice-satellite/actions/workflows/forge-build.yml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/forge-build.yml)
[![Device Build](https://github.com/amitra93/echo-voice-satellite/actions/workflows/device-build.yml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/device-build.yml)

A fork of https://github.com/wilbowes/EchoMuse that has the following differences (as of August 2026):

*  **Streaming STT via Gemini 3.5 Transcribe (HACS).** The `echo_voice_satellite` HACS integration now *is* the STT provider: a single `GeminiTranscribeEntity` (`hacs/custom_components/echo_voice_satellite/stt.py`) streams 16 kHz mono PCM to `gemini-3.5-transcribe-live` via the Live API (`google-genai` `LiveConnectConfig` + `AudioTranscriptionConfig`). One Live session per turn provides both interim `interim_input_transcription` (forwarded as `transcript {is_final:false}` via `CorrelatedMicStream` + `ContextVar` — survives `assist_pipeline`'s `process_enhance_audio` wrapping) and final `input_transcription` (returned as `SpeechResult` → `STT_END` → `transcript {is_final:true}`). The controller (`em_turn_engine.py` `transcript` action) gates `transcript_mono`/`stt_latency_ms` on `is_final` and logs partials at `DEBUG text=` (dropped by `em_support._LOG_DROP` in bundles). Selection is opt-in — pick “Gemini Transcribe” as the pipeline’s STT engine. Requires `google-genai>=2.22.0` (empty `AudioTranscriptionConfig` fallback for `1.59.0` installs, vocab ignored until upgraded). See `docs/design/hacs-stt-plan.md` (now **Implemented**) for the full model, VAD (`requires_external_vad=False` → Gemini `Automatic` server VAD), and privacy (HA-side never logs transcript, not even at `DEBUG`).
*  **Streaming TTS integration via Google Cloud TTS.**
*  ***Custom HACS integration**: Devices appear through a custom HACS integration rather than ESPHome. This allows for more control and is the transport for Gemini Live (audio + partials + `ConfigEntry` options for key/vocab/mode/language).
*  **Amazon audio framework** Audio is routed through Amazon Echo Android APIs, leading to richer and deeper sound.
*  **Better trainer**: Changes to `oww_forge` to make wakeword training easier.
*  Wake word and stopword detection run entirely on the devices — the controller never receives idle microphone audio and never scores a wake or stop model.

## Contributing

Bug reports, fixes and hardware findings are all welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). The most useful thing you can send is an
issue with a support bundle attached (Dashboard → Support → Download bundle);
it carries the logs, versions and metrics needed to diagnose something
remotely, with transcripts, recordings and network names excluded.

---

## License

MIT — see [LICENSE](LICENSE).

EchoMuse vendors and links several third-party components, each keeping its own
licence. They are inventoried in [NOTICE.md](NOTICE.md); note that the device
binary links two BSD-3-Clause components, whose copyright notices that file
carries on the binary's behalf.
