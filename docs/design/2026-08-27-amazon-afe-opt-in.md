# Amazon AFE Production Audio Architecture

**Status:** Production architecture. Amazon AFE through paired OpenSL ES capture
and playback is EchoMuse's only production audio path on Echo Dot Gen 2.

## Decision

EchoMuse uses Android's supported OpenSL ES API, running the audio helper as
Android's `system` UID, to reach Amazon's existing Audio Front End (AFE). The
helper opens both ends as one transaction:

```text
OpenSL ES VOICE_RECOGNITION recorder -> Amazon AFE -> mono capture -> EchoMuse
EchoMuse renderer -> OpenSL ES player -> AudioFlinger/Amazon HAL -> speaker
```

Capture and playback are inseparable. The HAL observes playback on its own
route and can use it as the far-end reference for its audio processing. A
production process must not substitute a raw PCM route for either side.

## Ownership

| Concern | Owner |
|---|---|
| Microphone processing, beam selection, echo cancellation, gain | Amazon AFE/HAL |
| OpenSL recorder/player lifecycle | `afe_helper` running as UID 1000 |
| PCM framing, wake streaming, turn routing, media queues and ducking | EchoMuse device runtime |
| Wake detection, endpointing, Assist turn lifecycle, EQ | Controller and Home Assistant |
| Physical privacy mute and LEDs | Device runtime |

The AFE is opaque. EchoMuse does not expose local beam angle, ADC gain, fixed
mic gain, local AGC, local echo-cancellation, or Speex controls. Dashboard and
configuration documentation must not present those as production settings.

## Helper Boundary

`device/tools/afe_helper` is the system-UID audio endpoint. The root daemon
starts it with `su system -c /data/local/bin/afe_helper`; the helper rejects
any other UID. Its stdin/stdout are exclusively the `internal/afeipc` channel:
a fixed header (`EMAF`, version, operation, request ID, bounded payload length)
followed by PCM or a JSON acknowledgement/error.

`Open` creates recorder and player together. If either fails, it closes both
and reports no usable audio path. Runtime failure is terminal for that audio
process: report the error, stop the device audio runtime cleanly, and let the
supervisor restart it. There is no direct-PCM recovery backend.

## Runtime Contract

- The recorder uses `SL_ANDROID_KEY_RECORDING_PRESET` = `VOICE_RECOGNITION`.
- The player and recorder are both opened through OpenSL ES.
- The helper supplies processed mono capture to the normal 16 kHz wake/turn
  framing path.
- The renderer accepts the existing 48 kHz mono voice and music planes and
  writes them through the OpenSL player.
- Ducking, saturation, flush/discard-until-EOS, playback completion reporting,
  and the native Sendspin renderer remain device-runtime responsibilities.
- Scheduled playback timing must use an OpenSL presentation/completion clock
  measured on the active player. Do not infer it from raw-device diagnostics.

## Capability And Observability

The device starts only after the complete helper pair is open. Registration and
low-rate statistics report normal device health; they do not report a selected
audio backend because there is no alternate path. Capability negotiation still
governs independent features; do not use firmware versions.

Hardware validation covers complete turns: capture level and latency, residual
echo during playback, mute effectiveness, barge-in, output level/clipping,
ducking, Sendspin timing, jack transitions, CPU, memory, and thermals. A
successful recorder open alone is not evidence of a usable production path.

## Raw ALSA Diagnostics

The raw ALSA capture/output tools under `device/tools/` are **diagnostic tools
only**. They are retained for hardware inspection, fixture capture, and
comparison measurements. They do not define a supported runtime backend and
must not be wired into production capture or playback.
