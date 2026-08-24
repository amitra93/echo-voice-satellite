# Music Assistant / Sendspin Interface

## Status

Design for the direct interface between the EchoMuse controller and Music
Assistant. The timestamped controller-to-device protocol and scheduled device
renderer already exist. The outbound Music Assistant connection described here
is the next implementation step.

## Decisions

- Music Assistant is the Sendspin server and owns content, queue, groups and
  playback state.
- One EchoMuse Sendspin client represents one Echo device. Each keeps a durable
  identity and pairing store under `data/sendspin/<device_id>/`.
- The EchoMuse controller connects directly to Music Assistant. Home Assistant
  and the EchoMuse HACS integration are not audio proxies.
- Music Assistant's HA integration owns the primary music `media_player`
  entity. EchoMuse HACS exposes the voice satellite and Echo-specific status;
  it must not create a duplicate music entity once the Music Assistant player
  exists.
- Initial audio remains PCM S16 LE, 48 kHz, mono. Codec expansion is separate.
- Pairing remains required in normal operation. Unpaired access may be used
  only as an explicit diagnostic mode and is not part of this design.

## Topology

```text
Music Assistant Sendspin server (:8927)
        |  Sendspin WebSocket
        |  stream lifecycle, timestamped PCM, metadata, group commands
        v
EchoMuse controller: one Sendspin client per eligible Echo
        |  Sendspin server time -> controller monotonic -> device monotonic
        |  EchoMuse frames 0x06 start / 0x07 PCM / 0x08 clear / 0x09 end
        v
Echo device scheduled music renderer
        |  local voice ducking, timestamp-paced ALSA output
        v
Speaker

Home Assistant <-> Music Assistant integration <-> music media_player entity
Home Assistant <-> EchoMuse HACS integration <-> voice/device diagnostics
```

## Music Assistant Address

The controller has one setting, `music_assistant_url` (environment variable
`MUSIC_ASSISTANT_URL`). It applies fleet-wide because every per-Echo client
connects to the same Music Assistant server.

Accepted values:

```text
                            # empty: discover _sendspin-server._tcp.local.
192.168.1.10:8927           # normalized to ws://192.168.1.10:8927/sendspin
music-assistant.local       # normalized to ws://music-assistant.local:8927/sendspin
ws://192.168.1.10:8927/sendspin
wss://music.example:8927/sendspin
```

Queries, fragments and embedded credentials are rejected. The dashboard stores
the canonical URL in `system_config`; that value overrides the deployment
environment default. Changing it should reconnect all Sendspin sessions live
once outbound connection management is implemented.

## Discovery

When the setting is empty, browse `_sendspin-server._tcp.local.` and use the
service's address, port and TXT `path` (default `/sendspin`). Music Assistant
advertises the server service; EchoMuse must not mistake a player advertisement
for a server.

Discovery continues while no explicit server is configured. A lost mDNS record
does not tear down a healthy socket. After a socket fails, discovery resumes so
an address change can heal without restarting the controller.

## Connection Lifecycle

For every connected device announcing `music_sync`:

1. Wait until the controller/device monotonic clock filter has converged.
2. Load or create the durable Sendspin identity and pairing store.
3. Construct a client with `player@v1`, `metadata@v1` and `controller@v1`.
4. Connect outbound to the configured/discovered Music Assistant WebSocket.
5. Complete Noise and pairing before advertising the player as available.
6. Report player state only when the Echo data plane and clock are both ready.
7. On transient failure, reconnect with bounded exponential backoff. A network
   recovery or device reconnect resets the backoff.
8. On device disconnect, report unavailable and clear scheduled audio before
   releasing the session.

The SDK may retain an inbound client listener for server-initiated connections,
but outbound connection is authoritative. An inbound socket must run the SDK's
server-initiated handshake path and must not send a second `client/init` into a
Music Assistant connection that is already in its message loop.

## Pairing

The first connection uses the SDK pairing protocol. EchoMuse must expose:

- Pairing state: unpaired, waiting, paired, failed.
- An admin-only action to open the pairing window.
- A transient PIN display when dynamic PIN pairing is selected.
- An admin-only reset action that removes the stored Music Assistant record and
  disconnects that Echo's session.

PSKs and long-term credentials are never returned by an API, displayed in a
support bundle or logged. Pairing records persist independently per Echo, so
resetting one player does not remove the others.

## Audio and Time Conversion

For every Sendspin audio chunk:

```text
server timestamp
  -> aiosendspin compute_play_time()
  -> controller monotonic presentation time
  -> em_clock.ClockSync.controller_to_device()
  -> device monotonic presentation time
```

Callbacks enqueue work and never await the Echo socket. One bounded writer per
device serializes start, PCM, clear and end frames. A full queue drops audio
that can no longer meet its deadline rather than applying backpressure to the
Music Assistant session or playing it late.

Stream generations isolate reconnects and track changes. Clear, disconnect and
server replacement invalidate queued PCM immediately. Old generation frames
remain harmless if delayed in TCP.

## Playback Ownership

Sendspin and legacy URL playback are mutually exclusive per device, and the
**last direct request wins**. A Sendspin stream start stops and flushes
`em_player` before forwarding synchronized PCM. Symmetrically, a direct legacy
`play_media` request (a spoken "play jazz" routed through Home Assistant to
`0x04`) **wins over an active Sendspin group**: the controller makes that
device leave the Sendspin group cleanly — clear+end the scheduled stream so the
device's mixer lets `0x04` play, then disconnect the Sendspin socket so the
server stops streaming to a client nobody is listening to (leaving is not
ignoring). It stays out of the group until the legacy playback ends, at which
point the player becomes available again. That re-arm is **not a rejoin**: it
does not resume the old group's audio (the server only streams on a fresh group
start), so no music appears in the room that nobody asked for at that moment —
regrouping the device is a deliberate user action.

This resolves the failure mode where an active/paused Sendspin stream would
otherwise suppress the legacy plane on the device and silently swallow a direct
"play jazz".

Voice turns do not pause the Music Assistant group. The device attenuates its
local synchronized music plane while voice audio plays, preserving the shared
timeline and leaving every other group member untouched.

## Volume and Controls

Music Assistant group commands flow through the Sendspin controller role. The
initial control set is play, pause, stop, next, previous, seek, volume and music
mute. Music mute is not Echo privacy mute.

Physical volume changes are reported back as Sendspin player state. A future
device-side music gain should separate Music Assistant volume from voice
response volume; until then, the documented implementation may map it to the
existing hardware master volume, with that limitation visible in the UI.

## State and Observability

Per client, expose without secrets:

- Configured or discovered server name/address.
- Client ID and pairing state.
- Connection and Sendspin time-sync state.
- Music group and playback state.
- Selected format and buffer capacity.
- Writer depth/drops and rejected late frames.
- Device render late samples, underruns, start error, sync error and correction
  count.

The primary music state reaches Home Assistant through Music Assistant. The
EchoMuse dashboard and HACS diagnostics may show these health fields but should
not claim ownership of Music Assistant's queue state.

## Failure Behavior

- Music Assistant unavailable: Echo stays available for voice and legacy
  playback; Sendspin state is disconnected.
- Device unavailable: its Sendspin player reports unavailable; other group
  members continue.
- Clock unconverged or uncertain: player stays unavailable and no scheduled
  frames are sent.
- Audio arrives after its device deadline: drop/trim and count it, never play
  it late.
- Server identity changes at the configured address: require pairing again;
  never silently trust it because the hostname is unchanged.
- Controller restart: identities and pairing survive; connections and stream
  generations restart cleanly.

## Implementation Sequence

1. Consume `music_assistant_url` and implement `_sendspin-server._tcp.local.`
   discovery when blank.
2. Add outbound connection management to `SendspinRuntime` while retaining
   one identity/session per Echo.
3. Implement pairing state, open-window and reset APIs.
4. Validate one Echo: connect, pair, start/clear/end, metadata and controls.
5. Transport renderer telemetry back to the controller.
6. Validate two Echos for start alignment and long-term drift.
7. Remove the duplicate EchoMuse HACS music entity after confirming Music
   Assistant creates the expected player entity.

## Tests

- URL normalization, IPv4/IPv6, TLS, default port/path and rejection cases.
- Explicit URL overrides discovery; clearing it resumes discovery.
- One durable identity per Echo across controller restarts.
- Pairing credentials are never returned or logged.
- Reconnect/backoff and server identity changes.
- Clock-unready devices never register as available.
- Stream ordering, stale-generation rejection and deadline drops.
- Legacy playback exclusion and local-only voice ducking.
- Music Assistant creates one HA entity; EchoMuse creates no duplicate.
