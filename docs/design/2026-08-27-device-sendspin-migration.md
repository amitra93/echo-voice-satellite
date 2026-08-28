# Device-side Sendspin migration

Moving the Sendspin client from the **controller** (`em_sendspin.py`, aiosendspin,
`0x06/0x07` forwarded frames) to the **device** (a native Go Sendspin client
talking to Music Assistant directly, MA → device). Music Assistant's own HA
integration provides the `media_player` entity, volume, and mute; the EchoMuse
controller/HACS stop providing those for Sendspin.

This supersedes `docs/design/music-assistant-sendspin-design.md` and
`docs/design/sendspin-plan.md` (the controller-side design). Inspiration:
`wilbowes/EchoMuse` branch `sendspin-design` (`docs/audio-states.md` §6/§8).

## Target architecture

```
today      MA ─(aiosendspin,PCM)→ controller ─0x06/0x07→ device scheduled_music
sendspin   MA ─(WS: hello, time-sync, FLAC chunks + timestamps)→ device Sendspin client
                                                                  → scheduled_music renderer
MA's HA integration → media_player.study (+ volume + mute + queue + metadata)
```

The controller keeps: voice turns (`0x02`), wake word, legacy `0x04`/`em_player`
(now ownership-coordinator + non-MA `play_media` only), device management, and
it **pushes MA's address** to the device. The controller stops being an audio
middleman for Sendspin.

## Locked-in decisions

| # | Decision | Choice |
|---|---|---|
| Output chain | Port EQ/limiter/bass-guard to device (option A) — device-side Sendspin otherwise plays silently unshaped | **A** (cherry-pick `device/internal/outchain` from `sendspin-design`, **regenerate fixtures from OUR Python chain**) |
| Codec | FLAC vs PCM | **FLAC** (advertise FLAC; ~3.68% of a core measured on hw vs 0.97% PCM, but half the bandwidth on the lossy link). PCM advertised as fallback. |
| MA discovery (1A) | how device learns MA address | **Controller-push** via `ConfigMessage.sendspinServer`; device caches last-known. (mDNS `_sendspin-server._tcp` fallback later.) |
| Mute (2A) | who owns mute | **MA owns music mute** (player entity → Sendspin mute → device music-plane gain only). **Keep EchoMuse privacy-mute switch** (mic/hardware/red-ring, device-sovereign). |
| Volume (3A) | authority | **Device is runtime authority**; controller persists (`startupVolume` from `volume_state`) + dashboard read-out; controller never pushes volume at runtime except its existing one-time boot seed. |
| Sync clock (4B) | renderer clock | **Measured OpenSL presentation clock**, not `CLOCK_MONOTONIC`. |
| Encryption | Noise | MA 6.0.5 implements none; build plaintext-first with Noise as a **switchable layer** (later). No pairing needed (server has no approval path). |
| Coverage | new code | **≥75%** (host tests). |

## Phase 0 gate — PASSED

`media_player.study` on the `music_assistant` HA platform exists (user installed
the Music Assistant HA integration 2026-08-27). This unblocks the HACS/controller
removal phases. Re-check: HA entity registry has a `music_assistant`-platform
`media_player` for the Echo.

## Done (this session) — `device/internal/sendspin/`

| Component | File | Coverage | Notes |
|---|---|---|---|
| 2-D Kalman time filter | `timefilter.go` | 100% | 1:1 port of aiosendspin `client/time_sync.py`; golden fixture `testdata/timefilter_fixture.json` generated from the exact Python. Maps server ts → device clock. |
| OpenSL playback clock (4B) | `playbackclock.go` | 98.6% | Uses the active OpenSL player's measured completion/presentation timing. |
| Wire protocol codec | `proto.go` | 94.1% | Envelope `{"payload":{…},"type":…}`; `client/hello`,`client/time`,`client/state`,`client/goodbye`; parse `server/hello`,`server/time`,`stream/start`(+`codec_header`),`stream/clear`/`end` role-targeting,`server/command`,`group/update`; binary audio frame `>Bq` (type 1B + int64 BE `timestamp_us`), AUDIO_CHUNK=4. |
| Native client core | `client.go`, `manager.go` | package total 81.7% | Direct WS handshake, periodic time-sync, reconnect/backoff, stream lifecycle, PCM scheduling, and MA volume/music-mute commands. Deliberately not wired into firmware until output-chain parity passes. |
| Decoder | `decoder.go` | package total 81.9% | Validates PCM or FLAC stream setup (including FLAC header presence) and decodes complete FLAC frames through `mewkiz/flac`; malformed or unsupported audio is rejected. |
| Configuration delivery | `config.go`, `em_controller.py`, `em_api.py` | n/a | Controller pushes `sendspinServer`; URL updates reach connected native devices immediately. |

## Reverse-engineered protocol reference (so the next session need not re-derive)

Captured live from `aiosendspin` 6.0.5 in the controller container.

- **Envelope:** every control msg is JSON `{"payload":{…},"type":"<type>"}`.
- **Roles:** `player@v1`, `controller@v1`, `metadata@v1`.
- **Codecs:** `flac`, `pcm`, `opus`. **Servers MUST support all three; client advertises what it wants.**
- **`client/hello` payload:** `client_id, name, version(=1), supported_roles[], player_support{supported_formats[{codec,channels,sample_rate,bit_depth}], buffer_capacity, supported_commands[]}`.
  - ⚠ The reference `to_json()` **omitted `player_support`** — likely a mashumaro quirk. We include it (the server needs our formats). **Must be confirmed against a live MA handshake** in the client phase.
- **`client/time` payload:** `{client_transmitted:t1}`. **`server/time`:** `{client_transmitted:t1, server_received:t2, server_transmitted:t3}`; client supplies t4 (its receive instant). Offset = ((t2−t1)+(t3−t4))/2, max_error = ((t4−t1)−(t3−t2))/2 → `TimeFilter.Update(offset, max_error, t4)` (see `_handle_server_time` in aiosendspin client.py).
- **`client/state` payload:** `{state, player{state,volume(0-100),muted,static_delay_ms,required_lead_time_ms,min_buffer_ms,supported_commands}}`. Initial state must include lead/min_buffer/static; increments omit unchanged.
- **`stream/start` payload:** `{player{codec,sample_rate,channels,bit_depth,codec_header(base64)}}` — `codec_header` is the FLAC init (STREAMINFO).
- **`stream/clear`/`stream/end`:** `{roles:[]}` — empty = all; entries may be `player@v1` or `_`-prefixed.
- **`server/command` payload:** `{player{command,volume,mute,static_delay_ms}}` — MA's controller-role commands (volume/mute/play/pause/next/…).
- **`group/update`:** `{playback_state, group_id, group_name}`.
- **Binary audio:** header `>Bq` = `message_type(1B)` + `timestamp_us(int64 BE, signed)`, then codec payload. `BINARY_HEADER_SIZE=9`, `AUDIO_CHUNK=4`.
- **No pairing / no Noise** in MA 6.0.5 (verified): a plaintext client that connects + sends `client/hello` registers as a player. Build Noise as a switchable layer for spec-compliance later.
- **Lead/buffer:** reuse the controller's tuned values — `required_lead_time_ms=4000`, `min_buffer_ms=1000`, `buffer_capacity≈480000` (5s of 48k mono S16). See `em_sendspin.py` constants.

## Remaining tasks

### Device (Go)

1. **FLAC decode** — implemented with `github.com/mewkiz/flac`; add a real MA-captured FLAC fixture and run the on-device CPU check (port `sendspin_bench`) before declaring it deployed.
2. **Client state machine** (`client.go`) — implemented for direct WS, handshake, periodic NTP sync, reconnect, stream lifecycle, PCM scheduling, and volume/music-mute commands. Remaining validation:
   - WS connect (reuse `gorilla/websocket`) to the controller-pushed MA URL; reconnect w/ backoff; `client/goodbye` on clean leave.
   - Handshake: send `client/hello`; receive `server/hello`.
   - **Time-sync loop:** periodic `client/time`; on `server/time` compute offset/max_error and `TimeFilter.Update`. Gate playback until `IsSynchronized()`.
   - Send initial `client/state` (volume/muted + lead/min_buffer/static_delay).
    - Stream lifecycle: `stream/start`(player role) → build FLAC decoder from `codec_header` → binary `AUDIO_CHUNK`s (decode → PCM, convert `timestamp_us` via `TimeFilter.ComputeClientTime`, then to the OpenSL-clock-scheduled frame position) → feed `scheduled_music`; `stream/clear`/`stream/end` (player-role-targeted) → clear/end the renderer.
   - **Volume/mute wiring (2A/3A):** `server/command` volume/mute → device music-plane gain / hardware volume; **must not touch privacy mute**. Report changes back via `client/state` and to the controller via `volume_state` (persistence).
    - Keep the clock behind an interface so the OpenSL presentation clock (4B) is the impl and tests can inject a fake.
3. **Renderer integration** — native chunks now feed the existing interpolated `scheduled_music` renderer, but its "now" still uses `deviceclock.NowUs()`. Integrate the measured OpenSL presentation clock before multi-room claims. Keep device-side ducking + mixing unchanged.
4. **Capability** — controller-side gate is ready, but firmware must not announce `sendspin_native` until output-chain parity and hardware verification pass. Controller forwarding remains active today.
5. **Config** — implemented: `sendspinServer` rides `ConfigMessage`; add durable last-known endpoint storage before relying on controller-offline reconnects.
6. **Output chain (option A)** — cherry-pick `device/internal/outchain/` + `bindings/speaker/outputchain.go` + `output_chain` capability from `wilbowes/sendspin-design`. **Regenerate `testdata/chain_fixture.bin` from OUR `em_eq`/`em_limiter`/`em_mbc`** (they differ from the branch's — incl. the ceiling work). Wire post-mix at `silenceLoop`. Controller stands down its chain only for `output_chain` devices, and only on announce (R2). No-click via dual-instance **linear** crossfade (their §8.3).

### Controller (Python)

7. **Push MA address (1A)** — implemented from `MUSIC_ASSISTANT_URL` / dashboard system config, including immediate update to connected native devices.
8. **`sendspin_native` gating** — implemented controller-side; inactive until firmware safely announces the capability.
9. **Volume authority (3A)** — controller persists `volume_state`; never push volume at runtime (keep boot seed).
10. **[GATE PASSED] Remove controller Sendspin** — delete `em_sendspin.py`, `em_music_sync.py`, `0x06-0x09` handling, `/api/system/sendspin`, `sendspin:true` routing in `_post_media_command`, `em_ha_sidechannels.sendspin_state`, the HA-wins yield/release, `aiosendspin` from `requirements.txt`. Keep `MUSIC_ASSISTANT_URL` only as the pushed address. Update tests (delete `test_sendspin*`, `test_music_sync`).

### HACS (Python)

11. **[GATE PASSED] Remove Sendspin media_player + music volume/mute** — delete `EchoSendspinMediaPlayer` (`media_player.py`), drop `media_player` from `PLATFORMS`, remove `sendspin_state` mapping in `coordinator.py`. **Keep** `switch.py` `EchoMuteSwitch` (privacy mute). Add stale-entity cleanup for the removed `*_sendspin_music` entity (mirror `_remove_stale_mute_entities`). Note MA now owns `play_media` for the Echo (arbitrary URL playback moves to MA's player; legacy `0x04`/`em_player` stays only as voice-turn ownership coordinator + non-MA HA media).

### Cross-cutting

12. **Rollout/coexistence** — capability-gated: controller + HACS handle both `sendspin_native` (device path) and legacy `music_sync`-only devices during migration. Single test device — fine.
13. **Verification** — `go test ./internal/... ./pkg/...` + `pytest`; ≥75% coverage on new code; build firmware (`compile.sh`), push to inactive slot, restart, verify on hardware; confirm MA drives volume/mute and the media_player; listen for regressions. Output-chain: measure against fixtures before ear (their §8.4 risk: port sounding different from Python).

## Key risks

- **`player_support` omission** in the reference `client/hello` — confirm live against MA before trusting the port (task 2).
- **Device CPU** — FLAC + ChaCha (later) + output chain, stacked on mic + shadow. Measure via `sendspin_bench` (task 1).
- **Output-chain port sounding different** from Python (§8.4) — fixtures regenerated from OUR chain, measured early.
- **Current hard blocker:** the `sendspin-design` output-chain port fails the fixture regenerated from this branch's controller chain (up to 39,941 LSB difference across EQ, limiter, and bass-guard cases). Do not enable `sendspin_native` or remove legacy forwarding until that port agrees with the current fixture.
- **hw_ptr integration** into the renderer is the subtle bit (mapping server ts → device clock → exact audible frame). The `TimeFilter` + `PlaybackClock` primitives are done and tested; the mapping glue lives in task 3.
- **Debuggability** — audio path moves entirely on-device. Keep the `[music] sched` telemetry (already added) and add client-side telemetry.

## Reuse pointers

- `wilbowes/EchoMuse` `sendspin-design`: `device/internal/outchain/*` (+ tests, `fixture/`, `testdata/gen_chain_fixture.py`), `device/internal/bindings/speaker/outputchain.go`, `output_chain` capability wiring in `control.go`/`config.go`, and `device/tools/sendspin_bench/` (CPU probe, `mewkiz/flac v1.0.12`). **Design only** — there is **no** device Sendspin *client* on that branch to reuse; the client (tasks 1-5) is greenfield.
