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

## Full duplex, in this design

"Full duplex" means the audio transport is bidirectional and turn-agnostic:
the mic streams up continuously while a turn is open, and TTS/announcements
can be pushed down at any time — including mid-turn for barge-in. Home
Assistant's own Assist pipeline stays turn-based (STT → intent → TTS); that
is the realistic ceiling when Assist is the consumer.
