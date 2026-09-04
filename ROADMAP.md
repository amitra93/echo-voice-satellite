# EchoMuse Roadmap

**Current state (2026-08-24):** the HACS turn engine, continued
conversation, Sendspin music playback, and device-side wake triggering are
already present. The items below describe the remaining reliability and
feature work, not a greenfield ESPHome implementation.

This roadmap focuses on making EchoMuse a polished, privacy-conscious voice satellite while preserving its strongest differentiator: deeply repurposing Echo Dot hardware with reliable, observable fleet management.

## Guiding Principles

- Keep device-local privacy controls sovereign, especially mute.
- Prefer capability negotiation over firmware-version checks.
- Degrade to a working, simpler behavior rather than silently doing nothing.
- Keep cloud AI explicitly opt-in and visible to the user.
- Measure audio, network, wake-word, and turn behavior before optimizing it.
- Preserve local Home Assistant Assist as the default path.

## Priority Roadmap

### 1. Complete Full-Duplex Interruption

**Goal:** Make barge-in cancel both device playback and the active Home Assistant pipeline.

- Add a controller-to-HACS turn-abort event.
- Cancel the active `async_accept_pipeline_from_satellite` task.
- Serialize old and replacement turns using the existing `em_runbarrier` design.
- Discard late TTS and pipeline events from cancelled turns.
- Record interruption, cancellation, and replacement-turn timings.

**Relevant code:** `controller/em_turn_engine.py`, `controller/em_controller.py`, `controller/em_runbarrier.py`, `hacs/custom_components/echo_voice_satellite/assist_satellite.py`

### 2. Add Remote Start And Richer Continued Conversations

**Goal:** Support Home Assistant's `START_CONVERSATION` satellite feature and
extend the existing HA-requested continuation flow.

- Advertise `AssistSatelliteEntityFeature.START_CONVERSATION` only after the controller supports it.
- Implement announcement-then-listen behavior.
- Support multiple follow-up turns without repeating the wake word.
- Add configurable follow-up timeout and cancellation behavior.
- Add distinct LED states for the follow-up window.

**Relevant code:** `hacs/custom_components/echo_voice_satellite/assist_satellite.py`, `hacs/custom_components/echo_voice_satellite/client.py`, `controller/em_turn_engine.py`

### 3. Add A Local Stop Word And Native Timers

**Goal:** Match a basic smart-speaker feature users expect without requiring a new wake word.

**Native timers are done.** See `docs/design/timers-design.md` and
`docs/design/timers-implementation-update.md` for the full design and
phase-by-phase build status; `docs/timer-validation.md` for test coverage
and the hardware acceptance checklist.

- ~~Add timer start, update, pause, resume, cancel, and finished events.~~
  Done — Home Assistant's native `TimerManager`/Assist timer intents are the
  only timer state; the controller forwards lifecycle events and owns only
  the physical alarm.
- ~~Add timer LED feedback and a configurable maximum ring duration.~~ Done —
  amber pulse while ringing, `MAX_RING_S` = 120s.
- ~~Make `stop` work while the device is muted without weakening mute
  privacy.~~ Resolved differently than proposed: a muted device discards a
  `finished` expiry entirely (no chime, no ring at all), so there is no
  ringing alarm to stop while muted in the first place.
- **Persisting timers through disconnects was decided against, not
  built.** An expiry that cannot reach an offline Echo or controller is
  discarded locally and never replayed on reconnect — Home Assistant remains
  the single source of truth for timer state, deliberately, rather than
  EchoMuse maintaining a second one that could disagree with it.
- **The generic local stop word remains open.** What shipped is narrower and
  timer-specific: an STT-only HA pipeline run gated on a recognized
  transcript, not a wake-free local classifier usable during TTS,
  announcements, or music generally. A configurable stop word covering all
  four (TTS, announcements, music, and timer ringing) via a local classifier
  is still a real gap. One considered direction specifically for timers: a
  small local streaming recognizer (e.g. Vosk/sherpa-onnx) running
  controller-side only while a timer alarm is ringing, so dismissal would
  not need a full HA STT round trip at all — not pursued, since the
  STT-gated approach already meets the "no unsolicited response" requirement
  and adds no new inference dependency. See item 7 ("Add Fast Local
  Commands") below for the wider version covering ordinary playback.

**Relevant code:** `controller/em_timers.py`, `controller/em_timer_alarm.py`,
`controller/em_turn_engine.py`, `controller/em_player.py`,
`hacs/custom_components/echo_voice_satellite/`

### 4. Support Multiple Wake Words With Backend Routing

**Goal:** Let each wake word select an explicit assistant or behavior.

Example routing:

```text
"Hey Home"   -> Home Assistant Assist
"Hey Gemini" -> Gemini Live
"Computer"   -> a selected HA pipeline or persona
"Stop"       -> local cancellation only
```

- Support multiple active classifier models per device.
- Expose available and active wake words through the HACS satellite configuration.
- Route wake events by model ID.
- Show the selected backend through LED and dashboard state.
- Keep model installation install-before-switch and capability-gated.

**Relevant code:** `controller/em_oww_models.py`, `controller/em_oww_assets.py`, `controller/em_controller.py`, `device/internal/wakeword/`, `hacs/custom_components/echo_voice_satellite/assist_satellite.py`

### 5. Add An Optional Gemini Live Audio Backend

**Goal:** Offer native full-duplex audio-to-audio conversation without replacing local Assist.

The existing audio boundary is a strong fit for Gemini Live:

- EchoMuse sends 16 kHz signed PCM into the HA-facing audio channel.
- Gemini Live accepts 16 kHz signed PCM input.
- Gemini Live produces 24 kHz signed PCM output.
- EchoMuse already receives 24 kHz TTS and resamples it for the device's 48 kHz playback path.

#### Proposed Architecture

```text
Echo -> EchoMuse controller -> HACS audio backend
                                  |- Home Assistant Assist
                                  `- Gemini Live session
```

- Keep the Gemini connection out of the Echo firmware.
- Add a backend abstraction beside the current Assist pipeline adapter.
- Select the backend per device, wake word, or explicit conversation mode.
- Support native audio input/output and model interruption.
- Forward input/output transcriptions into turn history when enabled.
- Implement reconnect and session-resumption handling.
- Add session duration, cost, latency, and fallback metrics.
- Fall back to normal Assist when Gemini is unavailable.
- Keep Gemini disabled by default.

#### Gemini Safety And Privacy Requirements

- Never expose Gemini credentials to the Echo or browser.
- Make cloud processing visible through dashboard and LED state.
- Require explicit configuration and per-device enablement.
- Do not enable proactive always-listening audio by default.
- Add configurable session duration, monthly usage, and data-retention controls.
- Require confirmation for locks, doors, alarms, purchases, and destructive actions.
- Expose only narrowly scoped Home Assistant tools, not unrestricted service calls.

**Relevant code:** `controller/em_turn_engine.py`, `controller/em_audio_frame.py`, `hacs/custom_components/echo_voice_satellite/client.py`, `hacs/custom_components/echo_voice_satellite/assist_satellite.py`, `hacs/custom_components/echo_voice_satellite/tts_stream.py`

### 6. Add Whole-Home Intercom And Synchronized Announcements

**Goal:** Turn the fleet into a coordinated room-to-room audio system.

- Add dashboard-defined speaker groups.
- Support synchronized announcements with a common start time.
- Add room-to-room intercom and action-button push-to-talk.
- Support short reply windows after an intercom message.
- Add quiet hours, do-not-disturb, and per-room volume policies.
- Normalize announcement volume across devices.

**Relevant code:** `controller/em_player.py`, `controller/em_turn_engine.py`, `controller/em_ha_sidechannels.py`, `device/internal/bindings/speaker/`

### 7. Add Fast Local Commands

**Goal:** Handle simple, latency-sensitive commands without a full STT or LLM round trip.

- Stop.
- Volume up/down.
- Pause/resume music.
- Cancel timer.
- Configured room lights on/off.

Use a small device-side classifier or focused speech recognizer — the same
pattern as the on-device stopword — with confidence thresholds and normal
Assist fallback. These commands should remain local where possible and must
not bypass device-sovereign mute behavior.

**Relevant code:** `controller/em_controller.py`, `controller/em_player.py`, `controller/em_turn_engine.py`, `controller/em_turnclock.py`

### 8. Add Room-Aware Context And Personas

**Goal:** Use the device's known room and policy context to improve responses without requiring voice identification.

- Scope ambiguous commands to the current room.
- Allow per-room voice, verbosity, and response style.
- Support restricted child-room or guest-room tool policies.
- Keep conversation context bounded and explicitly configurable.
- Make room context visible in the dashboard and HA configuration.

Do not use speaker recognition as an authentication mechanism. Voice identity may personalize responses, but must never authorize sensitive actions.

**Relevant code:** `controller/em_db.py`, `controller/em_config_sections.py`, `controller/em_turn_engine.py`, `hacs/custom_components/echo_voice_satellite/`

### 9. Add Opt-In Acoustic Event Detection

**Goal:** Detect useful non-speech events without retaining continuous audio.

Potential events:

- Smoke or CO alarm patterns.
- Glass breaking.
- Baby crying.
- Dog barking.
- Water alarms.
- Door knocks.

Requirements:

- Explicit per-device opt-in.
- Prefer local/controller inference with no continuous recording.
- Clear dashboard indication while monitoring is active.
- Event confidence, cooldown, and false-positive controls.
- Privacy review and evaluation data before enabling any detector by default.

### 10. Evaluate Controller-Side Playback Limiting

**Goal:** Prevent controller-side EQ peaks from hard-clipping before PCM reaches
the device, without changing the current tonal profile prematurely.

- Keep the existing 85 Hz subsonic high-pass and device codec DRC.
- Prototype a controller-side float-domain look-ahead limiter after EQ and
  before S16 conversion.
- Apply the same policy to HACS TTS, direct media playback, and Sendspin.
- Handle the limiter's look-ahead tail correctly for scheduled Sendspin audio.
- Validate on real hardware with boosted TTS and music before shipping.
- Do not add or stack the upstream dynamic 115 Hz bass guard in the same
  change; evaluate it separately against the current high-pass.

**Relevant code:** `controller/em_eq.py`, `controller/em_controller.py`,
`controller/em_player.py`,
`device/internal/bindings/speaker/`

## Implementation Order

1. Finish HA pipeline abort and turn serialization.
2. Implement `START_CONVERSATION` and reliable follow-up turns.
3. Add stop-word detection and timer lifecycle.
4. Generalize wake-word handling to multiple routed models.
5. Prototype Gemini Live through the existing 16 kHz/24 kHz HACS audio boundary.
6. Add safe Home Assistant tool calls, usage limits, privacy indicators, and fallback behavior.
7. Build synchronized multi-room announcements and intercom.
8. Add fast local commands.
9. Explore room-aware personas and acoustic event detection.
10. Evaluate a controller-side look-ahead limiter after real-device playback
    measurements.

## Explicit Non-Goals

- Do not restore the deleted ESPHome-impersonation backend as the primary architecture.
- Do not add a direct cloud connection from the Echo firmware.
- Do not enable always-on Gemini Proactive Audio by default.
- Do not use voice recognition as authorization for sensitive home actions.
- Do not promise support for every Echo generation before hardware-specific bindings and recovery paths exist.
- Do not build a generic plugin marketplace before core turn reliability and feature parity are complete.

## External References

- [Home Assistant Assist Satellite](https://developers.home-assistant.io/docs/core/entity/assist-satellite/)
- [Home Assistant Voice Preview Edition](https://www.home-assistant.io/voice-pe/)
- [OHF Linux Voice Assistant](https://github.com/OHF-Voice/linux-voice-assistant)
- [Wyoming Satellite](https://github.com/rhasspy/wyoming-satellite)
- [Gemini Live API](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/live-api)
- [OpenVoiceOS](https://github.com/OpenVoiceOS/ovos-core)
- [Willow](https://github.com/HeyWillow/willow)
- [Onju Voice](https://github.com/justLV/onju-voice)
- [EchoGo](https://github.com/Binozo/EchoGo)
- [EchoCLI](https://github.com/Dragon863/EchoCLI)
