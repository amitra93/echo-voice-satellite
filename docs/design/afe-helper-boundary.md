# System-UID OpenSL helper boundary

`device/tools/afe_helper` is the system-UID half of the Amazon AFE runtime. It
owns a paired OpenSL recorder/player while running as Android's `system` UID. A
root EchoMuse process launches it with:

```sh
su system -c /data/local/bin/afe_helper
```

The helper refuses any UID other than Android's well-known UID 1000, so a
future caller cannot accidentally bypass the identity boundary by executing it
directly as root.

The helper inherits stdin/stdout. Stdout is exclusively the `internal/afeipc`
transport; diagnostics go to stderr. The frame is a fixed 16-byte header
(`EMAF`, version, operation, request ID, bounded payload length) followed by
the payload. `WritePlayer` carries PCM, `ReadRecorder` returns PCM, and all
other operations return JSON acknowledgements or a structured error frame.

The `Open` operation creates the recorder and player as one transaction. If
either half fails, the other half is closed and no usable pair is reported.
The helper has no LED, mixer, raw-ALSA, network, or device-control privileges of
its own; root retains those in the daemon. The production daemon launches it
only when the persistent AFE marker is present.

This boundary is the production audio path. The renderer writes voice and music
through the OpenSL sink while retaining mixer, ducking, mute, output-chain,
flush/EOS, and statistics behaviour. Scheduled playback requires a measured
OpenSL presentation clock before synchronization is advertised.
