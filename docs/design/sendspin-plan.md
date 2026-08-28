# Sendspin Integration Plan

**Status:** Historical design, superseded by
[`2026-08-27-device-sendspin-migration.md`](2026-08-27-device-sendspin-migration.md).
Sendspin transport, device `music_sync`,
and the HACS media player shipped. The original limitation to group controls
and metadata is obsolete: the media player also supports media commands.

## Decisions

- Target **Music Assistant** first, while keeping the implementation aligned with the generic Sendspin protocol.
- EchoMuse's HACS integration owns the Home Assistant `media_player` proxy.
- Sendspin pairing is required; unpaired LAN access is not the first usable-version posture.
- Initial audio format is **48 kHz mono signed 16-bit PCM**.
- Complete simulation, protocol, and renderer tests before deploying firmware.
- Music Assistant remains the content, queue, and primary playback authority.
- The HACS media player initially supports Sendspin group controls and metadata, not arbitrary `play_media` URLs or media browsing.

## Architecture

`aiosendspin` runs in the Python controller. The FireOS device remains a timed audio renderer and local voice/music mixer.

```text
Music Assistant / Sendspin server
        | timestamped PCM
        v
aiosendspin client per Echo
        | Sendspin time -> controller monotonic -> device monotonic
        v
EchoMuse timed music protocol
        | stream ID + target timestamp + PCM
        v
Device scheduled renderer
        | voice ducking + music timeline
        v
OpenSL playback
```

The existing `0x04` music transport is not sufficient for synchronized playback. Sending ordinary PCM to multiple devices concurrently will not synchronize starts or prevent long-term clock drift.

## Reusable Components

- `aiosendspin` for Noise, pairing, clock synchronization, discovery semantics, stream lifecycle, commands, and server timestamp conversion.
- The existing device music plane and saturating voice/music mixer.
- Device-side ducking, which attenuates music without moving its timeline.
- Controller device registration, capability negotiation, configuration, authentication, and `/api/events`.
- Existing volume persistence and hardware control, with separate handling for Sendspin playback mute.
- Prototype ideas from `/home/amitra/echo-voice-satellite`:
  - Durable identity.
  - Dedicated music plane.
  - Local voice/music mixing.
  - mDNS advertisement.
  - Ramped gain changes.

The prototype's Sendspin protocol implementation is not reused unchanged. It has incorrect time fields, wall-clock timing, no complete stream lifecycle, exact-timestamp rendering, a stereo/mono mismatch, and an unaccounted speaker prime delay.

## Implementation Phases

### 1. SDK Boundary

- Pin `aiosendspin==6.0.5` and the matching protocol revision (the version actually deployed in Music Assistant's add-on and pinned in `requirements.txt`).
- Add `controller/em_sendspin.py` as the only controller-facing SDK adapter.
- Advertise only mono 48 kHz S16 PCM initially.
- Keep SDK callbacks non-blocking; callbacks enqueue work for a dedicated renderer path.

### 2. Identity, Pairing, and Discovery

- Generate one durable Sendspin identity and pairing store per Echo serial under persistent controller data, for example `data/sendspin/<device_id>/`.
- Keep identities stable across controller and device restarts.
- Run a dedicated Sendspin listener on port `8928`.
- Multiplex clients by path, such as `/sendspin/<client_id>`.
- Advertise one `_sendspin._tcp.local.` record per eligible Echo.
- Require pairing before normal playback.
- Advertise only devices with the new synchronized-playback capability.

### 3. Controller-to-Device Clock Synchronization

- Add a device monotonic timestamp exchange containing controller transmit/receive and device receive/transmit times.
- Reject high-RTT and retransmission-contaminated samples.
- Estimate offset, drift, and uncertainty.
- Never use device wall time.
- Convert timestamps through both clock domains:

```text
Sendspin server timestamp
  -> aiosendspin.compute_play_time()
  -> controller monotonic target
  -> device monotonic target
```

- Keep the Sendspin player unavailable until both clock filters converge.

### 4. Timestamped Device Music Protocol

- Add a separate `music_sync` capability. `audio_mix` alone does not imply synchronization support.
- Add data-plane messages for stream start, timestamped PCM, stream clear, and stream end.
- Include stream generation ID, sequence number, device-monotonic target time, and format information.
- Keep lifecycle messages ordered with PCM on the same data connection.
- Reject stale generations so delayed PCM or EOS from an old stream cannot affect a new one.
- Preserve `0x04`/`0x05` behavior for legacy URL playback and old firmware.

### 5. Device Renderer

- Add a scheduled music buffer integrated with the production renderer rather than the independently primed legacy music queue.
- Hold silence until the first target timestamp.
- Trim late prefixes instead of playing late audio.
- Clear immediately on stream clear, disconnect, or server replacement.
- Track predicted output time from the measured OpenSL presentation clock.
- Correct long-term clock drift with small, inaudible frame insertion/deletion or an equivalent bounded ASRC strategy.
- Keep steady-state correction within Sendspin's `+/-0.5%` speed limit.
- Retain local ducking without shifting the shared music timeline.
- Report music-specific buffer depth, late frames, underruns, start error, sync error, and correction count.

### 6. Transport Scheduling

- Add one controller-side outbound writer per device.
- Prioritize voice frames while preserving music timestamp order.
- Send music several seconds ahead, starting from the existing measured four-second resilience target.
- Advertise realistic `buffer_capacity`, `required_lead_time_ms`, and `min_buffer_ms`.
- Drop audio explicitly when it cannot meet its presentation deadline instead of playing it late.
- Surface degraded synchronization state.

### 7. Playback Ownership

- Make synchronized Sendspin playback and legacy URL playback mutually exclusive per device.
- Starting Sendspin playback stops and flushes a legacy `em_player.MediaSession`.
- Voice turns duck synchronized music locally rather than pausing or seeking the Sendspin group.
- A voice turn in one room must not pause the entire group.
- Sendspin disconnect, stream clear, and server replacement flush scheduled music immediately.

### 8. Volume and Mute

- Map Sendspin volume to the existing hardware master volume initially.
- Do not map Sendspin mute to EchoMuse privacy mute.
- Add a distinct music playback mute/gain in the device mixer.
- Report physical volume changes back to Sendspin.
- Ramp music mute and gain changes to avoid clicks.
- Preserve the raw hardware volume ceiling of `127`.

### 9. HACS Media Player

- Add `media_player` to the HACS integration and implement `media_player.py`.
- Gate the entity on `music_sync`, not `audio_mix`.
- Add the Sendspin controller role for group play, pause, stop, volume, mute, seek, and group switching.
- Populate state from Sendspin group updates and metadata callbacks.
- Add Sendspin state to controller snapshots and the HACS coordinator.
- Do not initially expose arbitrary `play_media` or media browsing.
- Keep privacy mute as the existing separate switch.
- Verify that Music Assistant does not create a duplicate entity for the same player.

### 10. Configuration and Operations

- Add `SENDSPIN_ENABLED` and `SENDSPIN_PORT` with standalone/add-on parity.
- Add dashboard status for identity, pairing, connected server, group, synchronization, and buffer health.
- Provide pairing-token generation and reset without logging PSKs.
- Include `aiosendspin` in both amd64 and arm64 controller images.

## Testing Before Hardware

- Unit-test timestamp frame codecs, stream generations, late-prefix trimming, drift correction, and clock conversion.
- Test the SDK adapter with fake callbacks and stream lifecycle messages.
- Simulate two devices with different clock offsets and drift.
- Simulate 5-10% packet loss and 1-2 second TCP stalls.
- Verify steady-state synchronization within `+/-1 ms`, targeting `+/-0.5 ms`.
- Verify no startup warble.
- Verify no stale audio after clear, end, reconnect, or server replacement.
- Verify voice ducking without moving the music timeline.
- Verify one group member entering a voice turn does not disrupt other members.
- Verify correct group behavior when one member disconnects.
- Verify synchronized state, metadata, volume, mute, and pairing behavior.

Only after these tests pass should the first hardware test use Study, followed by a second Echo for synchronization validation.

## Main Risks

- Scheduling in Python and then feeding the existing one-second-prime queue cannot synchronize output.
- TCP head-of-line blocking can deliver audio late despite no data loss.
- Independent audio clocks drift after a synchronized start.
- Per-device REST commands can diverge from group-owned Sendspin state.
- Existing master volume affects voice as well as music.
- The current EOS/flush protocol has no stream identity and is unsafe for rapid reconnects.
- `audio_mix` is not evidence that a device supports timestamped playback.
- Sendspin and `aiosendspin` are public-preview projects; SDK and spec versions must remain explicitly pinned and tested together.
