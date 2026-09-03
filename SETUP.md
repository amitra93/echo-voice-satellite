# EchoMuse Architecture Reference

> **Looking for installation instructions?** Start with
> [Quickstart](docs/quickstart.md). This page documents the active device
> architecture; [JOURNAL.md](JOURNAL.md) retains the chronological record.

## Production Audio Path

Amazon AFE through paired OpenSL ES capture and playback is the sole production
audio path for Echo Dot Gen 2.

```text
7-microphone array
        |
OpenSL ES VOICE_RECOGNITION recorder (system UID)
        |
Amazon AFE / AudioFlinger / hardware HAL
        |
processed mono capture -> device-side wake word / stopword detection

controller voice/music PCM -> device renderer -> OpenSL ES player -> HAL -> speaker
```

The AFE owns microphone processing, including its proprietary beam selection,
echo handling, and gain behaviour. EchoMuse does not apply a local beamformer,
SpeexDSP echo canceller, fixed mic gain, AGC, or ADC tuning in production.
Those controls are not part of the active configuration surface.

The system-UID `afe_helper` opens the OpenSL recorder and player as one
transaction. Both are required: the HAL needs the paired playback route for
its own processing. A partial open is an audio startup failure, not a reason
to use an alternate raw PCM runtime.

All wake word and stopword processing happens on the device: the Dot scores
its own mic stream and sends no idle audio to the controller. When the wake
word crosses the threshold, the device requests admission; the controller
arbitrates multi-device wakes, grants the turn, and routes voice audio to
the HACS integration. Home Assistant returns TTS over the per-turn audio
socket; the controller shapes it and streams 48 kHz mono PCM to the device
renderer. Voice and music retain separate device buffers so a voice turn can
duck music without discarding prebuffered audio.

## Audio Diagnostics

The microphone array remains physically seven microphones plus two unconnected
capture channels. `device/tools/capture_mics`, `bf_capture`, `analyse_capture.py`,
hardware mapping, capture fixtures, codec state, or AFE comparisons. They are
not a production capture or playback path and their raw format, period timing,

## Voice And Transport

Each device opens three outbound WebSocket connections: `/control` for JSON
control/configuration, `/data` for PCM frames, and `/shell/{device_id}` for
demand-opened root-shell proxying. The controller is discovered through mDNS.
Device features are negotiated by capability, never version string.

Wake and turn audio use continuous 16 kHz mono PCM frames. The HACS integration
opens one authenticated per-turn audio WebSocket, receives microphone audio,
runs Home Assistant Assist **with its own `stt.gemini_transcribe` provider** (`hacs/custom_components/echo_voice_satellite/stt.py` `gemini-3.5-transcribe-live` via `google-genai` Live, one session per turn, `interim_input_transcription` → `transcript {is_final:false}` via `CorrelatedMicStream`+`ContextVar`, `input_transcription` → `SpeechResult`), and returns 24 kHz TTS PCM. The controller
upsamples that response to 48 kHz mono, applies its output chain, and streams
it to the device.

Playback completion comes from the device, not a controller duration estimate.
The device renderer owns stream priming, flush/discard-until-EOS, saturation,
duck ramps, and its measured OpenSL presentation timing.

## Hardware Notes

Mute is device-sovereign: it physically blocks capture and keeps the red ring
authoritative. The action button, LED ring, ambient-light sensor, jack state,
for the user-facing pipeline and
[docs/design/2026-08-27-amazon-afe-opt-in.md](docs/design/2026-08-27-amazon-afe-opt-in.md)
for the AFE runtime contract.
