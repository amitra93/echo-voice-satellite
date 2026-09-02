# Full-Duplex Turn Engine + HACS Integration — Implementation Plan

**Status:** Implemented with changes. The ESPHome voice path was removed and
the controller/HACS turn engine shipped. The remaining full-duplex gap is
controller-driven abort of an active Home Assistant pipeline during barge-in;
see [`ROADMAP.md`](../../ROADMAP.md). The HACS integration now also exposes a
media player, beyond this plan's original entity scope.

Replace the ESPHome impersonation as EchoMuse's voice path by extending the
existing controller API with a turn engine and a Home Assistant custom (HACS)
integration. Devices appear in HA through that integration instead of as
native ESPHome devices, over a bidirectional audio transport. No standalone
gateway server, no custom control-WS protocol — the controller's existing
`em_api.py` (REST + `/api/events` WS) is the control surface; only the audio
transport needs a custom WebSocket and frame codec.

## Decisions locked in

- **Full cutover.** Remove the ESPHome impersonation entirely as the voice
  path (not additive).
- **"Full duplex" = bidirectional transport.** Mic streamed up during a turn,
  TTS/announcements pushable down at any time, instant barge-in. HA's Assist
  pipeline remains turn-based (STT → intent → TTS) — that is the realistic
  ceiling when the consumer is HA Assist, and matches what the prior
  `echo-voice-satellite` fork built.
- **Reuse proven fork designs**, built cleanly in `echomuse-v2`. Do not import
  the fork's Sendspin, SPA rewrite, or profiles/rollouts.
- **No standalone gateway / no custom control-WS protocol.** The fork built a
  separate `em_gateway.py` aiohttp app with a UUID-envelope control WebSocket
  (hello/hello_ack/connection_id/ping, inventory sync, turn RPCs). `echomuse-v2`
  **already has** all of that infrastructure in `em_api.py`: Bearer/session
  auth, `GET /api/devices`, the `/api/events` broadcast WS with `_push_event`,
  config push. Rebuilding it as a custom WS would duplicate working, tested,
  auth-gated code. The turn engine is therefore a module wired into the
  existing `em_api.py` app — not a parallel server.
- **Turn engine module: `controller/em_turn_engine.py`** (not `em_gateway.py`).
  The name reflects that it is a turn state machine embedded in the existing
  API, not a standalone gateway server.
- **Device identity is `ro.serialno`, used directly.** No UUIDs, no converters,
  no mapping table. The serial is already the device id on `/control` and
  `/data`, the SQLite key, and the ESPHome-MAC seed. HA's device registry keys
  on the serial too.
- **Wake word stays controller-side** (openWakeWord), unchanged. Out of scope.
- **TTS is streamed from the HACS integration as 24 kHz PCM chunks (Model 3).**
  HA exposes a custom `assist_satellite` to a streaming `ResultStream`, not just
  a URL. The integration decodes/resamples to 24 kHz PCM **as HA's TTS engine
  produces it** and pushes chunks down the per-turn audio WebSocket; the
  controller upsamples 24→48, applies EQ, and plays. This removes the
  `tts_proxy` URL fetch + controller-side ffmpeg from the critical path — the
  delay you were worried about. See "TTS conversion" below. No firmware change.
- **The per-turn audio WebSocket is the only custom WS.** It is bidirectional:
  mic frames up (`MIC_PCM`), TTS PCM chunks down (`TTS_PCM`), concurrently —
  that is the full-duplex transport. All other control (turn lifecycle, state,
  BLE events) rides ordinary REST + the existing `/api/events` WS. No audio
  bytes cross `/api/events`.
- **BLE proxy moves into the HACS integration** via an HA remote scanner, which
  lets us delete the ESPHome protocol layer entirely.

## Target architecture

```
Echo Dot ──(existing /control + /data WS, UNCHANGED)──▶ Controller (audio hub)
   ▲ plays 48 kHz TTS                                     │  wake word,
   │ down existing 0x02 plane                             │  em_player, 24→48 upsample + EQ
   │                                                      │
   │                                       em_turn_engine.py  (NEW — turn state machine,
   │                                        wired into em_api.py, NOT a separate server)
   │                                                      │
   │                                       em_api.py (EXISTING — extended)
   │                                        ├─ REST: GET /api/devices, PATCH /api/devices/{id}, …
   │                                        ├─ REST: POST /api/devices/{id}/turn, POST /api/turns/{tid}/{accept|reject|endpoint|cancel|tts/start|tts/end|transcript|pipeline-event}  (NEW)
   │                                        ├─ WS  /api/events  (EXISTING — gains wake.offer, turn.state, turn.terminal, button.event)
   │                                        └─ WS  /api/v1/ws/ha/audio/{turn_id}  (NEW — BIDIRECTIONAL: MIC_PCM up / TTS_PCM down)
   │                                                      ▲          │
   │                            HACS integration          │ mic up    │ TTS PCM chunks down (24 kHz)
   │                            custom_components/echo_voice_satellite/
   │                            (assist_satellite, entities, BLE scanner)
   │                                                      ▲
   └── ResultStream.async_stream_result() ─────── Home Assistant (Assist pipeline + TTS engine)
```

The device↔controller side does **not** change: `/data` is already full-duplex
(mic `0x01` up; speaker/music `0x02`–`0x05` down) and the controller already
does all audio processing. Only the HA-facing layer changes:
`em_esphome.py` (ESPHome impersonation) → `em_turn_engine.py` (turn state
machine) + new REST/WS surfaces on `em_api.py` + a HACS integration.

**Control surface = existing `em_api.py` infrastructure (no custom protocol):**
- Inventory / device state: `GET /api/devices` + `/api/events` (already exist).
- Auth: existing Bearer/session (`POST /api/auth/login`).
- Device control (mute/volume/config): existing `PATCH /api/devices/{id}` +
  config push over the device's `/control` WS.
- Turn lifecycle: **new** REST endpoints (see below) — ordinary request/response,
  no envelope/hello/connection_id/ping layer.
- Turn state / wake offers / button events / BLE adverts: **new** event types
  pushed via the existing `/api/events` WS (`_push_event`).

**Audio surface = the one new custom WebSocket:**
- `WS /api/v1/ws/ha/audio/{turn_id}` — `>BBI`-framed, bidirectional.
  `MIC_PCM=0x01` (2560 B = 80 ms @ 16 kHz mono S16) up; `TTS_PCM=0x03`
  (≤ 24 000 B @ 24 kHz mono S16) down; `MIC_EOS`/`TTS_EOS` close each direction.

**Audio direction summary:**
- **Mic (up):** device `/data` `0x01` → controller → `MIC_PCM` on the per-turn
  audio WS → HACS `assist_satellite` → HA Assist STT.
- **TTS (down):** HA Assist produces a TTS `ResultStream` → HACS decodes +
  resamples to 24 kHz PCM **incrementally as HA generates it** → `TTS_PCM`
  chunks down the same per-turn audio WS → controller upsamples 24→48 →
  `em_eq.StreamingEQ` → `stream_speaker` → device `/data` `0x02`. No
  `tts_proxy` URL fetch; no controller-side ffmpeg for TTS.
- **`/api/events`** stays JSON-only (turn lifecycle, state, BLE events); no
  audio bytes cross it.

## New REST endpoints (added to `em_api.py`)

| Method | Path | Purpose (replaces fork control-WS message) |
|--------|------|---------------------------------------------|
| `POST` | `/api/devices/{id}/turn` | Create a turn (`{kind: announcement\|conversation}`) → `{turn_id}` (`turn.create`) |
| `POST` | `/api/turns/{tid}/accept` | Accept a wake offer (`wake.accept`) |
| `POST` | `/api/turns/{tid}/reject` | Reject a wake offer (`wake.reject`) |
| `POST` | `/api/turns/{tid}/endpoint` | User stopped speaking (`turn.endpoint`) |
| `POST` | `/api/turns/{tid}/cancel` | Barge-in / cancel (`turn.cancel`) |
| `POST` | `/api/turns/{tid}/tts/start` | TTS stream signalling (`tts.start`) |
| `POST` | `/api/turns/{tid}/tts/end` | TTS stream signalling (`tts.end`) |
| `POST` | `/api/turns/{tid}/transcript` | Record STT text (`transcript.record`) |
| `POST` | `/api/turns/{tid}/pipeline-event` | Relay a pipeline event (`pipeline.event`) |
| `POST` | `/api/devices/{id}/test_audio` | Upload a WAV (any format ffmpeg handles); **Q7:** controller auto-converts to 16 kHz mono S16 PCM via ffmpeg on upload (Phase 1b, admin-only) |
| `POST` | `/api/devices/{id}/test_turn` | Trigger a synthetic wake + feed uploaded PCM (Phase 1b, admin-only) |
| `DELETE` | `/api/devices/{id}/test_audio` | Clean up stored test PCM (Phase 1b, admin-only) |

All reuse the existing `em_api.py` auth middleware (Bearer/session). The
`turn_id` is a server-minted integer (as in the fork). No `connection_id` /
envelope / handshake — the session token is the auth, and `/api/events` is the
event channel.

## New `/api/events` event types (pushed via `_push_event`)

| Event type | Payload | Replaces |
|------------|---------|----------|
| `wake.offer` | `{device_id, turn_id, trigger}` | fork `wake.offer` |
| `turn.state` | `{turn_id, device_id, state}` | fork `turn.state` |
| `turn.terminal` | `{turn_id, device_id, outcome}` | fork `turn.terminal` |
| `button.event` | `{device_id, gesture, held_ms}` | ESPHome event entity |
| `ble.adverts` | `{device_id, adverts: [...]}` | ESPHome `BluetoothLERawAdvertisementsResponse` |

The audio-WS URL is **not** pushed as an event — it is deterministic
(`/api/v1/ws/ha/audio/{turn_id}`), so the HACS integration derives it from the
`turn_id` carried in `wake.offer` / `turn.state`. No `audio.open` handshake
needed.

## Reused from the fork (`amitra93/echo-voice-satellite`)

Lift with adaptation. The fork's **control-WS protocol (UUID envelopes,
hello/hello_ack, connection_id, ping, inventory sync) is NOT reused** —
`echomuse-v2`'s existing `em_api.py` (REST + `/api/events`) replaces all of it.
What IS reused:

- **Audio framing** (`audio.py`) — `>BBI` header (type, flags, seq) + payload;
  `MIC_PCM=0x01` (2560 B = 80 ms @ 16 kHz mono S16 — already the device's exact
  wake-frame size) with `MIC_EOS`, and `TTS_PCM=0x03` (≤ 24 000 B = up to
  500 ms @ 24 kHz mono S16) with `TTS_EOS`; direction-local `Sequence`. Both
  directions are kept — the audio WS is bidirectional under Model 3.
- **HACS package** — adapted from the fork, with the `GatewayClient` control-WS
  replaced by REST + `/api/events`:
  - `assist_satellite.py` — drives
    `async_accept_pipeline_from_satellite(channel.mic_frames())`; on `TTS_END`
    pulls the `tts_output.token`, calls `tts.async_get_stream(self.hass, token)`,
    and streams the `ResultStream` down the audio WS as `TTS_PCM` chunks.
  - `tts_stream.py` — incremental ffmpeg decode + resample to **24 kHz** mono
    S16 (`-ar 24000`), streamed to the audio WS. Ported from the fork.
  - A new thin **controller client** replacing the fork's `gateway.py` — calls
    the REST endpoints above and listens to `/api/events` for
    `wake.offer`/`turn.state`/`turn.terminal`/`button.event`/`ble.adverts`.
  - `coordinator.py`, `entities.py`, `config_flow.py`, platform files,
    `manifest.json`/`hacs.json` — adapted to serial-keyed device identity and
    the REST/event-WS control surface.

## TTS conversion (Model 3 — HACS streams 24 kHz PCM chunks; controller upsamples)

HA's `assist_satellite` gives a custom integration access to a streaming
`ResultStream` (via `tts.async_get_stream(self.hass, token)`), not just a
`tts_proxy` URL. The stream yields TTS audio **as HA's TTS engine produces
it** — before the full response is generated. Model 3 exploits this to cut the
URL fetch + re-decode out of the critical path:

```
HA TTS engine → ResultStream.async_stream_result() (incremental, in HA)
  → HACS tts_stream.py: ffmpeg decode + resample -ar 24000 -ac 1 (streaming, in HA)
  → TTS_PCM frames down the per-turn audio WS (24 kHz mono S16)
  → controller: 24→48 linear upsample → em_eq.StreamingEQ → stream_speaker
  → device /data 0x02 (48 kHz mono)
```

Why this over Model 2 (controller fetches a `tts_proxy` URL):
- **Lower latency.** No `tts_proxy` URL generation + HTTP GET + re-decode.
  First PCM chunk reaches the controller as soon as HA's TTS engine emits.
  `echomuse-v2`'s current path (HA generates → `tts_proxy` serves over HTTP →
  controller re-decodes with ffmpeg) inserts delay at every step; Model 3
  removes all of it from the critical path.
- **EQ stays controller-side and free.** Per-device parametric EQ runs at
  48 kHz after the upsample, so `em_eq.StreamingEQ` is untouched. The upsample
  is trivial (linear interpolation is fine for TTS — narrowband speech; the
  fork's v1 device runtime already uses a 24→48 linear interp).
- **Audio WS is the single full-duplex transport.** Mic up, TTS down, same
  socket, concurrently. `/api/events` stays JSON-only. Device-originated
  barge-in cancels playback through the ordinary turn admission path.
- **Decode runs where the stream already lives.** ffmpeg in the integration
  consumes an in-process `ResultStream`, not an HTTP fetch.

Costs (real but small):
- ffmpeg runs on the HA host instead of the controller (acceptable — it is a
  streaming transcode of narrowband speech, not heavy DSP).
- The controller gains a tiny 24→48 upsample step before `StreamingEQ`
  (cheap; no resampler library needed — linear interp suffices for TTS).
- `_stream_tts_audio` / `_fetch_tts_audio` are **not** ported into the gateway
  (they are the URL-fetch path Model 3 eliminates). `em_player`'s separate
  media/music ffmpeg path is unaffected.

Requirement: the HACS `assist_satellite` reads the `tts_output.token` from the
`TTS_END` pipeline event and calls `tts.async_get_stream(self.hass, token)` —
exactly the pattern the fork's `assist_satellite.py:251,273` already uses.

## Cutover surface in `echomuse-v2`

`em_controller.py` calls `esphome.*` in ~20 places; all repoint to
`em_turn_engine.*` with equivalent signatures:

- Lifecycle: `start/stop_esphome_servers` → `start/stop_turn_engine`;
  `device_connected`/`device_disconnected`; `set_device_capabilities`.
- Turn: `trigger_voice_turn`, `cancel_voice_turn`, `abort_ha_run`,
  `VOICE_PREROLL_DISCARD`, `VAD_SENTINEL_*`, `BUTTON_HOLD_MS`.
- State push: `push_media_state`, `update_device_volume`, `update_ambient_lux`,
  `update_oww_model`, `send_button_event`.
- The `_stream_mic_audio` / `run_esphome_voice_turn` logic (VAD sentinels,
  no-speech timeout via `em_turnclock`, NS via `em_ns`, utterance capture,
  `TurnTrace`, `_persist_turn`) **moves into the turn engine largely
  intact** — it is protocol-agnostic and worth preserving.

Keep as-is (already split out, no ESPHome import): `em_runbarrier`,
`em_announce`, `em_turnclock`, `em_recordings`, `em_volume`, `em_player`.

## Implementation decisions

Ten decisions resolved during planning. Each is locked in; the justification
records *why* so a future reader doesn't reopen a settled question.

### Q1: Audio WS — same port (8768) or separate?

**Decision: same port (8768).** The audio WS (`/api/v1/ws/ha/audio/{turn_id}`)
rides the existing `em_api.py` aiohttp app on `API_PORT=8768`, alongside REST
routes, `/api/events`, and `/api/devices/{id}/shell`.

**Why:** aiohttp handles concurrent WS on one port fine — `/api/events` and
`/api/devices/{id}/shell` already coexist with REST on 8768. The fork needed a
separate port (`GATEWAY_LAN_PORT=8769`) because it was a *separate server
process*; the turn engine is a module in the existing app. One port = one
process, one auth layer, no port-allocation DB columns.

### Q2: `turn_id` allocation — upfront or at completion?

**Decision: allocate upfront.** Add `db.create_turn(device_id, kind)` that
inserts a pending row at turn start and returns the auto-increment `turn_id`.
`_persist_turn` *updates* that row at completion (instead of inserting, as it
does today).

**Why:** the HACS integration needs `turn_id` in `wake.offer` *before* the turn
runs — it uses that to open the audio WS. An in-memory ID would diverge from the
DB row's `id`, breaking the `playback_stats` rendezvous (`device.last_turn_id`
is consumed by `_persist_turn`). The fork proved the upfront-insert model
(`store.create_turn` → `INSERT INTO turns(...)` at turn start).

### Q3: Does `_run_voice_locked` stay in `em_controller.py` or move?

**Decision: stay in `em_controller.py`.** It calls
`turn_engine.trigger_voice_turn()` exactly as it calls
`esphome.trigger_voice_turn()` today. The turn engine owns the HA round-trip
(mic streaming + TTS receive); the controller owns device orchestration (LEDs,
state pushes, barge-watcher, continuation loop).

**Why:** `_run_voice_locked` is 300 lines tightly coupled to `em_controller`'s
`Device` class, LED helpers (`leds_listening`, `_leds_turn_end`),
`_push_device_state`, and barge-watcher lifecycle. Moving it means either
passing 15 callbacks or moving `Device` too. The split is clean: the controller
orchestrates the device; the turn engine talks to HA.

### Q4: Audio WS — separate per-turn or persistent multiplexed?

**Decision: separate per-turn WS.** The HACS integration opens
`/api/v1/ws/ha/audio/{turn_id}` on `wake.offer` and closes it on `turn.terminal`.

**Why:** simpler lifecycle — no multiplexing logic, no "which turn does this
frame belong to," clean teardown. The fork proved per-turn WS works. One WS
open/close per turn is negligible at voice-turn rate.

### Q5: How does the controller map `turn_id` → audio WS connection?

**Decision: dict `turn_id → ws` in the turn engine.** The audio WS handler
extracts `turn_id` from the URL path and registers the WS on connect; the
mic-streaming coroutine writes `MIC_PCM` to it; TTS frames are read from it; the
entry is removed on WS close or turn end.

**Why:** the fork uses this exact model (`self._audio_sockets[turn_id]`). The
`/api/events` connection is separate (one per HA instance, shared across
devices) and carries JSON events only.

### Q6: Feed coroutine vs `_stream_mic_audio` race safety (Phase 1b)

**Decision: trust the queue (maxsize=256) + realtime pacing.** The feed paces
at 80 ms/frame; `_stream_mic_audio` drains at 20 ms chunks (4× faster). On
`QueueFull`, use the same drop-oldest strategy as `handle_data`.

**Why:** both `echomuse-v2` and the fork use this model. The feed is the
producer and shouldn't block on HA; the queue (256 frames = 20 s) absorbs any
HA-side latency.

### Q7: Uploaded WAV format — validate or auto-convert? (Phase 1b)

**Decision: auto-convert with ffmpeg on upload.** The `POST /test_audio`
endpoint decodes anything ffmpeg handles → 16 kHz mono S16 PCM, stores the PCM
(not the original).

**Why:** users will upload 44.1 kHz stereo MP3s, 22 kHz WAVs, etc. Rejecting
them is hostile; the controller already has ffmpeg everywhere. The fork's
`_resample_to_16k_mono` does exactly this.

### Q8: HACS integration auth to controller — token model + UX

**Decision: hybrid trusted-LAN + long-lived API key, managed in the controller
settings UI.** The controller gains an "API Keys" section in Settings (separate
from user session tokens). An API key is a long-lived, non-expiring bearer token
sent as `Authorization: Bearer <key>` on every request. The HACS config_flow
stores the key. Keys can be created, rotated, and revoked from the dashboard.

**Why:** `echomuse-v2` uses 30-day session tokens (`em_auth.create_session`,
`expiry_days=30`) — fine for a browser, wrong for a persistent integration that
must survive restarts without re-login. The fork used no auth at all
(`auth: {}`), which is too open. A dedicated API-key mechanism reuses the
existing Bearer-token infrastructure without the session-expiry problem.

**Settings UI UX — three options for the exact interaction:**

| | Option A: Single "Integration Key" card | Option B: Named keys list | Option C: Single key + "Regenerate" button |
|---|---|---|
| **Layout** | One card in Settings → "Home Assistant Integration". Shows the key (masked, with a "Show" toggle). One "Generate" button if none exists; "Rotate" + "Revoke" buttons if one does. | A list of named keys (e.g. "Home Assistant", "Test Script"), each with create/rotate/revoke. Like GitHub Personal Access Tokens. | One card showing the current key (masked). A single "Regenerate" button replaces it. No naming, no list. |
| **Complexity** | Lowest. One key, one purpose. | Higher — supports multiple integrations, named for audit. But overkill for a single-operator LAN appliance. | Lowest, but "Regenerate" is the only action — no explicit "create first" step. |
| **echomuse-v2 precedent** | The existing Settings tab already has single-purpose cards (Server, Microphones, etc.). | No precedent for multi-item lists in Settings. | The bootstrap token (`maybe_generate_bootstrap_token`) is a single shown-once token — closest precedent. |
| **Recommendation** | ✅ **Recommended.** Matches the existing dashboard's single-card-per-setting pattern, is purpose-built for the HACS integration, and "Rotate" is a one-click key change (as requested). | Over-engineered for a single-operator system. | Confusing UX — "Regenerate" before any key exists reads as broken. |

**Recommended UX (Option A):** Settings tab → "Home Assistant Integration" card.
If no key exists: shows "No API key configured" + a "Generate Key" button. If a
key exists: shows the key masked (`em_••••••••`) with a "Show" toggle, plus
"Rotate Key" (generates a new one, invalidates the old) and "Revoke Key"
(deletes it) buttons. The key is stored in the `system_config` table
(`ha_api_key`). The config_flow in the HACS integration has a field for this key.

### Q9: HACS discovers controller URL — manual or mDNS?

**Decision: manual URL in config_flow.** The user enters
`http://<controller-ip>:8768` during HACS setup. No mDNS.

**Why:** the fork tried zeroconf and hit a real HA limitation (zeroconf service
name ≤15 chars — `_emcontroller._tcp` is 18 chars; even `_echomuse._tcp` at 14
chars would need a separate mDNS service registration). Manual URL is what the
fork shipped and it works. mDNS can be a future enhancement.

### Q10: Does the `Device` class change?

**Decision: `Device` unchanged.** Only the consumer module swaps:
`em_esphome` → `em_turn_engine`. The `Device` class's attributes
(`voice_queue`, `mic_queue`, `oww_paused`, `voice_lock`, `beam_lock`,
`cancel_event`, `control_ws`, `data_ws`, `last_turn_id`,
`pending_playback_stats`) are device-state, not ESPHome-specific. The turn
engine reads the same attributes. `last_turn_id` / `pending_playback_stats`
continue to work because the turn engine calls `_persist_turn` (moved from
`em_esphome`), which uses them exactly as before.

**Why:** the fork replaced `Device` with a UUID-keyed gateway store (no
`voice_queue` — audio rides per-turn WS). That's a massive refactor of
`em_controller`'s wake listener, barge watcher, data handler, and
`_run_voice_locked`. Since the device↔controller side doesn't change, `Device`
stays; only the HA-facing consumer swaps.

### Phase 0 — Audio codec + REST/WS scaffolding (complete)

**Status: complete.** Implemented the audio codec, authenticated route
scaffolding, upfront turn-id schema support, API-key lifecycle, Settings UI,
Docker source copies, and focused tests. The turn routes intentionally return
`501 turn_engine_not_ready` until Phase 1 wires the live state machine.

- `controller/em_audio_frame.py` — the `>BBI` audio frame codec
  (`MIC_PCM`/`MIC_EOS`/`TTS_PCM`/`TTS_EOS`, `Sequence`), ported from the fork's
  `audio.py`. This is the **only** custom protocol element.
- `controller/em_turn_engine.py` skeleton — turn state machine (no aiohttp app;
  it is a module wired into `em_api.py`): turn lifecycle, `turn_id` allocation,
  state transitions (pending → listening → processing → responding → done).
  **Q2:** `turn_id` is allocated upfront via `db.create_turn` (inserts a pending
  row, returns the auto-increment id); `_persist_turn` updates that row at
  completion instead of inserting.
- New `em_api.py` routes (not yet wired to the engine): the turn REST endpoints
  table above + `WS /api/v1/ws/ha/audio/{turn_id}` (bidirectional, `>BBI`
  frames). Routes return 501/placeholder until Phase 1.
  **Q1:** all routes + the audio WS ride the existing `API_PORT=8768` — no
  separate gateway port.
  **Q5:** the audio WS handler registers `turn_id → ws` in a turn-engine dict
  on connect; the entry is removed on close/turn-end.
- **Q8: API-key infrastructure.** `em_db.py`: add `ha_api_key` to
  `system_config` (or a dedicated `api_keys` table). `em_auth.py`: add
  `generate_api_key()` (long-lived, non-expiring) and `validate_api_key()`
  (checked alongside the existing session-token Bearer auth). `em_api.py`:
  Settings routes `GET/PATCH /api/system/config` gain the API-key field;
  `POST /api/system/api_key/generate`, `POST /api/system/api_key/rotate`,
  `DELETE /api/system/api_key` for the Settings card actions. All new turn REST
  endpoints accept either a session token or an API key.
- Tests: audio-frame codec round-trip; REST route registration; WS upgrade;
  API-key generate/validate/rotate.

### Phase 1 — Turn engine (complete)

**Status: complete for controller-triggered wake turns.** Implemented the live
per-turn audio WS, controller mic forwarding, TTS PCM reception with 24→48
upsampling and playback callback, endpoint/cancel actions, turn state events,
upfront turn persistence, and switched `_run_voice_locked` to
`em_turn_engine.trigger_voice_turn()`. HACS-initiated conversation/announcement
turn creation remains Phase 2 work because it needs the entity-side commands
and announcement orchestration.

- Move `run_esphome_voice_turn` / `_stream_mic_audio` / `TurnTrace` /
  `_persist_turn` from `em_esphome.py` into `em_turn_engine.py`, driven by the
  REST endpoints + audio WS instead of protobuf events. **Do not port
  `_stream_tts_audio` / `_fetch_tts_audio`** — Model 3 replaces the URL-fetch
  TTS path. **Q3:** `_run_voice_locked` stays in `em_controller.py` and calls
  `turn_engine.trigger_voice_turn()` — the controller orchestrates the device
  (LEDs, state, barge-watcher); the turn engine talks to HA.
- Wire the Phase-0 REST routes to the engine:
  `POST /api/devices/{id}/turn` → engine creates a turn (via `db.create_turn`);
  `POST /api/turns/{tid}/
  {accept|reject|endpoint|cancel|tts/start|tts/end|transcript|pipeline-event}`
  → engine state transitions.
- Wire `/api/events` to push `wake.offer`, `turn.state`, `turn.terminal`,
  `button.event` via the existing `_push_event`.
- **Q4:** Per-turn bidirectional audio WS (`/api/v1/ws/ha/audio/{turn_id}`) —
  the HACS integration opens it on `wake.offer`, closes it on `turn.terminal`:
  - Mic (up): frames from `device.voice_queue` → `MIC_PCM` to HA.
  - TTS (down): `TTS_PCM` chunks from HA → controller → 24→48 linear upsample →
    `em_eq.StreamingEQ` → `stream_speaker` → device `0x02`.
- Barge-in: `turn.cancel` → `em_runbarrier` (unchanged).
- Shim `trigger_voice_turn`/`cancel_voice_turn`/`abort_ha_run` in
  `em_turn_engine.py` so `em_controller.py` call sites work unchanged.
  **Q10:** the `Device` class is unchanged — the turn engine reads the same
  `voice_queue`/`oww_paused`/`last_turn_id`/`pending_playback_stats` attributes
  that `em_esphome` read.

### Phase 1b — E2E test flow (complete; real HA required)

**Status: complete.** Implemented the admin-only WAV upload/normalization,
synthetic wake task, realtime `voice_queue` injection, cleanup endpoint, and
controller tests. The flow is ready for a real HA + HACS integration; no HACS
package has been installed yet.

A manual test path: upload a 16 kHz mono WAV query to the controller, trigger a
synthetic wake, and watch the full pipeline run end-to-end — LEDs light up on
the Echo, STT runs in HA, TTS streams back and **plays on the device speaker**.
This requires a real HA + the HACS integration running (Phase 3), so it is
exercised after Phase 3 lands; but the controller-side scaffolding is built
here because it is pure `em_api.py` + `em_turn_engine.py` work with no HACS
dependency.

**Design: inject at `device.voice_queue`, not at the device mic.** The device's
mic processing pipeline (beamformer, AEC, AGC, VAD) conditions a real-room
signal; injected WAV audio is already clean 16 kHz mono PCM, so running it
through mic selection and echo cancellation is pointless and could degrade it.
`voice_queue` is the natural injection point — it is exactly where real mic
frames go during a turn (`oww_paused` routes them there), and the turn engine's
`_stream_mic_audio` reads from it. The device still does its real job for
output: TTS flows back through `stream_speaker` → `/data` `0x02` → speaker, and
LEDs light via `led_anim` control messages. Only the mic input is synthetic.

**New REST endpoints (admin-only, added to `em_api.py`):**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/devices/{id}/test_audio` | Upload audio in any ffmpeg-supported format; controller normalizes to 16 kHz mono S16 PCM, stores keyed by device_id |
| `POST` | `/api/devices/{id}/test_turn` | Trigger a synthetic wake + feed the uploaded PCM into `voice_queue` at realtime pace, then let the turn engine run normally |
| `DELETE` | `/api/devices/{id}/test_audio` | Clean up stored PCM |

**Flow (controller-side):**
1. `POST /test_audio` — receive multipart `audio` (or a raw body), decode with
   ffmpeg to 16 kHz mono S16 PCM, enforce the 120-second limit, and store the
   normalized PCM in controller memory keyed by `device_id`.
2. `POST /test_turn` — admin auth; verify device is connected + approved + not
   muted; verify uploaded PCM exists for this device.
3. Set up the synthetic wake:
   - Drain `device.voice_queue` + `device.mic_queue` (same as `_run_voice_locked`).
   - Set `device.oww_paused` (routes to `voice_queue`, consistent with real wake).
   - Set `device.last_wake = {model, score: 0.9, threshold: 0.5, noise_floor}`
     (synthetic wake detail for the trace/Activity tab).
    - Record the synthetic wake details for the trace and Activity tab.
4. Call `_run_voice_locked(device, trigger_label="test_turn", is_wakeword=True)`:
   - This acquires `voice_lock`, sets `device.listening=True`, sends
     `led_anim` listening (LEDs light up green on the Echo), pushes device state.
   - This calls `em_turn_engine.trigger_voice_turn(...)`, which runs
     `_stream_mic_audio` — a loop reading from `voice_queue` and sending
     `MIC_PCM` frames to HA via the audio WS.
5. **Concurrently**, a feed coroutine chunks the stored PCM into 2560 B frames
   (80 ms @ 16 kHz mono S16) and `put_nowait`s them into `device.voice_queue`
   at ~realtime pace (80 ms per frame), then puts `VAD_SENTINEL_END`.
   - This is exactly the normal race between the device's real mic putting
     frames into `voice_queue` (via `handle_data`) and `_stream_mic_audio`
     consuming them — the queue is the decoupling buffer (maxsize=256).
   - **Q6:** the feed paces at 80 ms/frame; `_stream_mic_audio` drains at 20 ms
     chunks (4× faster). On `QueueFull`, use the same drop-oldest strategy as
     `handle_data` (keeps the tail contiguous, which is what STT cares about).
     No `await put` — the feed is the producer and shouldn't block on HA.
6. The turn engine streams `MIC_PCM` to HA → HACS `assist_satellite` →
   HA Assist STT → intent → TTS → `ResultStream` → `TTS_PCM` chunks back down
   the audio WS → controller upsamples 24→48 → `em_eq.StreamingEQ` →
   `stream_speaker` → device `/data` `0x02` → **speaker plays the response**.
7. LEDs transition: listening → thinking (spinner) → speaking (meter) →
   turn-end (outcome throb or silent). All driven by the existing
   `leds_listening` / `on_thinking` / `_leds_turn_end` path.
8. Turn completes; `_persist_turn` records it with `trigger="test_turn"`.

**What this exercises that unit tests can't:**
- The full HA round-trip (STT → intent → TTS) through the HACS integration.
- TTS playback on the real device speaker (the whole point — you hear it).
- LED state transitions on real hardware.
- The bidirectional audio WS under real frame flow (mic up + TTS down).
- The 24→48 upsample + EQ + `stream_speaker` chain with real TTS audio.

**What this does NOT exercise:**
- The device's mic capture / beamformer / AEC (intentionally bypassed).
- Wake-word detection (synthetic wake, not OWW scoring real audio).
- Barge-in (no real mic to hear a wake-over-TTS).

**Cleanup:** `DELETE /test_audio` removes the stored PCM. The feed coroutine
is fire-and-forget per turn; a second `test_turn` overwrites the stored audio.

**Tests:** controller-side unit tests for the REST endpoints (auth, WAV decode,
queue injection, state transitions); the real-HA E2E is manual (device +
HA container + HACS integration).

### Phase 2 — Entities & side-channels (complete)

**Status: complete.** Implemented the REST media command surface, JSON event
bridge for media state, button gestures, ambient light, volume, capabilities,
and wake-model changes, plus HACS-initiated announcement turn creation over
the existing bidirectional audio WS. Phase 2b remains separate for BLE remote
scanning.

- Media commands: `POST /api/devices/{id}/media` forwards play/pause/resume/
  stop/volume to `em_player` and the device control plane; media state is
  published on `/api/events`.
- `button.event`, ambient-light, volume, capabilities, and wake-model events
  are published on `/api/events`; native ESPHome forwarding is no longer used
  for these HACS-facing side channels.
- Announcements: `POST /api/devices/{id}/turn` creates an announcement turn;
  the HACS client opens the per-turn audio WS and sends TTS PCM chunks, which
  play through the existing device speaker path.

### Phase 2b — BLE proxy via HACS (complete)

**Status: complete.** The controller now publishes raw device BLE batches as
`ble.adverts` events, and the HACS package contains a passive
`BaseHaRemoteScanner` adapter using HA's `async_register_scanner` API. Raw AD
structures are parsed into `AdvertisementData` (manufacturer data, service
data, UUIDs, local name, RSSI); advertisements are explicitly non-connectable.

- Controller forwards each enabled device's `ble_adverts` `/control` batch to
  HA as a `ble.adverts` event on `/api/events`, keyed by serial.
- HACS scanner code lives under `hacs/custom_components/echo_voice_satellite/`:
  `ble.py` parses the controller payload and `ble_scanner.py` feeds
  `BluetoothServiceInfoBleak` into HA's central Bluetooth advertisement
  callback. It registers one scanner per Echo with `connection_slots=0`.
- Passive raw adverts only (no connectable GATT; EchoMuse does not do GATT).
  The existing `bleProxyEnabled` lifecycle gates controller forwarding. The
  advert counter remains available through the existing device status path.
- The implementation targets the scanner API present in the running
  Home Assistant container: `async_register_scanner`, `BaseHaRemoteScanner`,
  and `BluetoothServiceInfoBleak`.

### Phase 3 — HACS integration (complete)

**Status: complete.** The package lives at `echomuse-v2/hacs/`:

```
echomuse-v2/hacs/
  ├─ custom_components/echo_voice_satellite/
  │    ├─ __init__.py          setup/unload; HA imports deferred to call time
  │    ├─ const.py              domain, platforms, capability strings — no HA import
  │    ├─ audio_frame.py        MIC/TTS frame codec, mirrors em_audio_frame.py — no HA import
  │    ├─ client.py             REST + /api/events + per-turn AudioChannel — no HA import
  │    ├─ tts_stream.py         incremental ffmpeg decode/resample to 24 kHz — no HA import
  │    ├─ ble.py / ble_scanner.py   passive BLE (Phase 2b, unchanged)
  │    ├─ coordinator.py, entities.py, config_flow.py, assist_satellite.py
  │    └─ event.py, sensor.py, binary_sensor.py, number.py, select.py
  ├─ tests/          test_client.py, test_tts_stream.py, test_ble.py (run with only
  │                  aiohttp installed) + test_config_flow.py, test_entities.py,
  │                  test_assist_satellite.py (pytest.importorskip("homeassistant"))
  ├─ hacs.json, pyproject.toml, README.md
  └─ custom_components/echo_voice_satellite/{manifest.json, strings.json,
       translations/en.json}
```

Everything is keyed on the serial (`device_id`), never a UUID.

**No custom control-WS client was built** (per the "no standalone gateway"
decision): `client.ControllerClient` calls the Phase-0/1/2 REST endpoints and
listens on `/api/events`. It opens the bidirectional audio WS at
`/api/v1/ws/ha/audio/{turn_id}` (URL derived from the `turn_id` in
`wake.offer`).

**Q8, refined during implementation:** since this is a Python `aiohttp`
client and not a browser, it sends `Authorization: Bearer <api_key>` on
*every* request — REST, the `/api/events` WebSocket, and the per-turn audio
WebSocket alike. `em_auth._resolve_api_key` checks the header first regardless
of call site, so there was no need for the query-param fallback the plan text
originally described for the WS paths (that fallback exists for browser
clients, like the dashboard, which cannot set a header on a WS upgrade).

**Q9** — `config_flow.py` has a manual URL + API key form only; no mDNS.

**`__init__.py` defers every `homeassistant` import into its function
bodies.** The previous (Phase 2b) `__init__.py` was just a docstring; had
Phase 3 put `from homeassistant...` at module scope, importing *any*
submodule — including the pure ones (`client`, `audio_frame`, `tts_stream`,
`ble`) — would fail wherever `homeassistant` isn't installed, since Python
always runs a package's `__init__.py` first. That's also why `client.py`,
`audio_frame.py`, and `tts_stream.py` themselves import nothing from
`homeassistant`: they're fully unit-testable (`hacs/tests/test_client.py`,
`test_tts_stream.py`) without a Home Assistant install, mirroring
`em_audio_frame.py`'s testing story on the controller side. The HA-dependent
modules (`coordinator.py`, `entities.py`, `assist_satellite.py`,
`config_flow.py`, the platform files, `ble_scanner.py`) still import HA at
their own module scope — normal for code that only makes sense inside HA —
and their tests use `pytest.importorskip("homeassistant")`, the same pattern
the fork used (`SESSION_SUMMARY_2.md`'s "7 passed, 3 skipped").

**`assist_satellite.py` advertises `ANNOUNCE` only, not `START_CONVERSATION`.**
`em_turn_engine.create_turn` only implements the `"announcement"` turn kind —
conversation turns are always controller-triggered (wake word / button).
Advertising a feature the engine 400s on would be exactly the "control that
silently does nothing" CLAUDE.md's capability-negotiation rule forbids.

**Every turn must resolve the TTS side of the rendezvous, even with no TTS.**
`em_turn_engine._run_turn` always awaits `post_turn_play(_tts_chunks(turn))`
once `endpoint` is set and the audio socket is still open, and `_tts_chunks`
blocks until it reads `None` from `turn.tts_queue` — which only happens on a
received `TTS_EOS` frame or an explicit `tts/end`/`cancel` REST call. A silent
intent, a `no_speech` endpoint, or a pipeline `ERROR` therefore has to call
`POST /api/turns/{tid}/tts/end` directly, or the turn (and the device's
`voice_lock`) hangs forever. `_run_wake_pipeline`'s `finally` block is the one
place this is guaranteed: if no `_tts_task` was ever created for the turn, it
sends `tts/end` itself.

**Mute has no switch entity.** Mute is device-sovereign (CLAUDE.md "Volume /
mute persistence") — only the hardware button sets it, and the controller has
no config field or command to write it. A writable HA switch would be the
same "control that silently does nothing" problem in the other direction, so
`muted` is a read-only `binary_sensor` instead; there is no `switch.py` and
`"switch"` was dropped from `PLATFORMS`.

**Volume is a `number` entity (0.0–1.0), calling the existing
`POST /api/devices/{id}/media {"volume": …}`** (Phase 2's media command
endpoint) — not a 0–100 percent scale, matching the controller's own native
representation (`em_volume`).

**No `media_player` entity yet.** Phase 2's REST media-command endpoint
exists and `number.py` uses it for volume, but play/pause/stop/media-browse
are not yet exposed as HA entities — flagged as follow-up work, not silently
dropped.

**Controller-side addition needed for capability gating:** `_merge_device`
(`em_api.py`) gained two fields the entities read directly — `capabilities`
(the raw list, for `entities.capability_available()`, mirroring
`Device.capabilities`'s existing per-feature booleans) and
`ambient_light_lux` (surfaced from `live.stats["ambientLux"]` so a cold
`/api/devices` poll or a fresh `/api/events` snapshot shows the last reading
immediately, not just after the next live push). Pinned by
`test_phase2_sidechannels.py::test_merge_device_exposes_capabilities_and_ambient_light_for_hacs`.
The `/api/events` payload shapes these entities key off
(`ambient_light`/`volume_state`/`capabilities`/`wake_model`/`button.event`/
`ble.adverts`) are exactly `em_ha_sidechannels.py`'s, already pinned by
`test_phase2_sidechannels.py::test_sidechannel_event_helpers_publish_expected_payloads`.

**Test coverage expanded substantially after the initial Phase 3 pass**, per
`hacs/requirements-test.txt` and a real (if older — 2025.1.4, the newest this
environment's pip index carries; the project targets 2026.8.0) `homeassistant`
install: 144 HACS tests pass with it on `PYTHONPATH` (34 pass / 11 skip
without it — every pure module has full coverage either way), and the
controller suite grew to 540. Two real production bugs surfaced this way and
were fixed, not just documented:

- `EchoAssistSatellite` never implemented `async_get_configuration` /
  `async_set_configuration` — both `@abstractmethod` on `AssistSatelliteEntity`,
  so HA could not have constructed this entity at all (`TypeError` at platform
  setup, not a runtime surprise). Fixed by reporting the device's single
  controller-owned wake word (mirroring the ESPHome-mode satellite's
  `VoiceAssistantConfigurationResponse`) and rejecting `async_set_configuration`
  — wake word is still set from the EchoMuse dashboard, never from HA.
- Two `_attr_*` entity attributes (`EventEntity._attr_event_types`,
  `NumberEntity._attr_native_min_value`/`_attr_native_max_value`) are HA
  `cached_property` descriptors, not plain class attributes — reading them via
  the class itself (`SomeEntity._attr_x`) returns the descriptor object, not
  the value; only instance access works. This is ordinary Python property
  behaviour once known, but easy to trip over, and it caught a genuine test
  bug (not a production one) worth recording so it isn't rediscovered.

**One unverifiable-in-this-environment gap:** `homeassistant.components.tts`
has no `async_get_stream` in the installed 2025.1 — it's the streaming-TTS-
result API this package targets on 2026.8.0, and the fork validated the exact
same call end to end against a real 2026.8.2 container
(`SESSION_SUMMARY_2.md`). The tests exercise it via
`monkeypatch.setattr(..., raising=False)` so they still validate this
package's own control flow, but cannot themselves confirm the 2026.8 method
signature — re-verify against a real 2026.8 install before shipping if that
hasn't happened since this was written.

### Phase 4 — Cutover & cleanup (complete)

**Status: complete.** `em_controller.py` now imports `em_turn_engine as
turn_engine` (never `em_esphome`), and `em_esphome.py`, `em_ble_proxy.py`, and
the entire `esphome/` package (frame_protocol, satellite_server,
feature_flags, message_registry, vendored aioesphomeapi protobufs) are
deleted, along with their `Dockerfile` `COPY` lines. **Q10** held: `Device`
was not touched, only the consumer module swapped, and every
`esphome.*`/`em_ble_proxy.*` call site in `em_controller.py`/`em_api.py`
repoints to `turn_engine`/inline logic with an equivalent signature.

- **BLE proxy retirement was simpler than the plan implied.** It is not a
  second protocol surface to port — Phase 2b had already moved BLE forwarding
  onto `ble.adverts` events, so all that remained of `em_ble_proxy.py` was a
  `bleProxyEnabled` lifecycle flag. That is now a plain boolean cached on
  `Device` (`device.ble_proxy_enabled`, set at register and on config push,
  exactly like `ns_asr`/`save_utterances`), checked once per `ble_adverts`
  control message before forwarding. No per-device listener, no mDNS, no
  `reconcile()` — the entire class of lifecycle bug that pattern invites is
  gone with it.
- **`_esphomelib._tcp` mDNS is gone** — it went with `em_esphome.py`, since
  it was only ever advertised alongside the per-device ESPHome TCP listener.
  **Q9** holds as planned: the HACS `config_flow` takes a manual URL, no mDNS
  replacement was added.
- **DB:** `get_esphome_port`/`assign_esphome_port`/`free_esphome_port` and the
  BLE-proxy port-allocator equivalents are deleted from `em_db.py` along with
  their only callers. The `esphome_api_port`/`ble_proxy_port` **columns**
  are deliberately left in the schema, unread and unwritten — `MIGRATIONS` is
  append-only (see CLAUDE.md), so a column from a deployed migration cannot be
  dropped, only stop being used. The `turns` table already carries what the
  upfront-insert model needs from Phase 0/1; no further schema change was
  required for the cutover itself.
- **Docs:** `CLAUDE.md`'s "Voice backend" and "HA entities beyond the voice
  satellite" sections are rewritten to describe the turn-engine/HACS
  architecture rather than ESPHome impersonation, with the genuinely
  protocol-agnostic lessons (the TTS-side rendezvous must always resolve,
  barge-in's run-overlap problem, live-not-cached capability reads) carried
  forward and the ESPHome-specific mechanics that motivated them (`RUN_END`/
  `RUN_START` discrimination, `ListEntities` staleness, connection-bounce
  workarounds) marked as retired history rather than deleted outright, so the
  reasoning that led here stays legible. The Key Python Modules table,
  scattered current-state references elsewhere in the file (turn persistence,
  LED outcome rhythm, the mic-audio recording tap, the TTS fetch path two
  other modules had stale comments about), and `docs/voice-pipeline.md` were
  checked; the latter turned out to already be written at a protocol-agnostic
  level of abstraction (it never named ESPHome) and needed no changes.
- **Test suite:** `test_capabilities.py`'s device/controller capability
  cross-check is retargeted from `em_esphome.py`'s `_device_has()` idiom to
  `hacs/.../const.py`'s `CAP_*` constants (the equivalent single-source-of-
  truth on the new consumer side), and its three tests pinning the
  `_pending_caps` race are replaced with one test pinning the property that
  makes that race structurally impossible now (`_merge_device` reads
  `capabilities` live off `Device`, never a snapshot). `test_deploy.py`,
  `test_barge_serialisation.py`, and `test_announce.py` each lost the tests
  that pinned ESPHome-protocol-specific mechanisms with no equivalent in the
  new architecture (documented in-file at each removal site) and kept every
  test pinning a still-load-bearing pure-Python property. A new
  `hacs/tests/test_entity_naming.py` ports the retired
  `test_entity_names_do_not_repeat_the_device_label` against this
  integration's own source, using `ast` (not a literal-string regex) so a
  future switch to a dynamically-composed entity name can't silently stop
  being caught. Two small live bugs surfaced and were fixed in the same pass
  (not just documented): `preroll_discard` was computed and then discarded
  unused, silently disabling wake-word-tail trimming; and
  `device.turn_history` was never appended to, which meant playback-stats
  attachment and the Activity tab's post-restart view were both silently
  broken. Controller suite: 546 passed, 1 skipped. HACS suite: 37 passed, 11
  skipped (no `homeassistant` install in this environment; unchanged from
  Phase 3).

## Testing strategy

- Controller: new tests for `em_audio_frame` (codec round-trip), the REST turn
  endpoints (state transitions, auth), the audio WS (bidirectional frame flow,
  barge-in), and the `/api/events` event types; adapt the existing suite
  (currently pins ESPHome behaviors).
- HACS: the fork's `tests/` (config_flow, audio, tts_stream, entities,
  assist_satellite) — `test_tts_stream.py` **is** ported under Model 3; the
  fork's `test_gateway_*` (control-WS protocol) is **not** ported (no custom
  protocol); a new `test_controller_client` covers the REST + `/api/events`
  replacement.
- E2E: device simulator + real HA container (the fork's harness reached working
  STT + streaming TTS this way).
- **E2E test flow (Phase 1b):** manual full-pipeline test with a real Echo +
  real HA + HACS integration — upload a 16 kHz mono WAV, trigger a synthetic
  wake, verify LEDs light up, STT runs, TTS plays on the device speaker.
  Controller-side unit tests cover the REST endpoints (auth, WAV decode, queue
  injection, state transitions); the real-hardware E2E is manual.
- Device Go tests unchanged (device side does not change under 48 kHz).

## Risks

- **HA Assist is turn-based** — "full duplex" here is bidirectional transport +
  anytime TTS + barge-in, not simultaneous speech. Accepted.
- **BLE remote-scanner API drift** across HA versions — pin at implementation.
- **TTS provider format variance** — solved by `tts_stream.py`'s incremental
  ffmpeg transcode to 24 kHz in the integration, same pattern the fork reached
  working STT+TTS with. Controller upsamples to 48 kHz before EQ.
- **Test-suite churn** — the cutover touches many deploy/capability tests;
  budget time for it.
- **`/api/events` fan-out scaling** — the existing event WS is currently
  dashboard-only (a handful of clients). The HACS integration adds one more
  persistent listener per HA instance; fine, but worth noting that turn-state
  events now flow through it at voice-turn rate.
