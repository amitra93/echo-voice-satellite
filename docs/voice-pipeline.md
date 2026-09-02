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
