# EchoMuse

[![Unit Tests](https://github.com/amitra93/echo-voice-satellite/actions/workflows/unit-test.yaml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/unit-test.yaml)
[![Controller Build](https://github.com/amitra93/echo-voice-satellite/actions/workflows/controller-build.yml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/controller-build.yml)
[![Forge Build](https://github.com/amitra93/echo-voice-satellite/actions/workflows/forge-build.yml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/forge-build.yml)
[![Device Build](https://github.com/amitra93/echo-voice-satellite/actions/workflows/device-build.yml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/device-build.yml)

A fork of https://github.com/wilbowes/EchoMuse that has the following differences (as of August 2026):

*  Streaming STT and TTS integrations.
*  Devices appear through a custom HACS integration rather than ESPHome. This allows for more control and I will be using this to support Gemini Live in the future.
*  Audio is routed through Amazon Echo Android APIs, leading to richer and deeper sound.
*  Changes to `oww_forge` to make wakewor training easier.

## Contributing

Bug reports, fixes and hardware findings are all welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md). The most useful thing you can send is an
issue with a support bundle attached (Dashboard → Support → Download bundle);
it carries the logs, versions and metrics needed to diagnose something
remotely, with transcripts, recordings and network names excluded.

---

## License

MIT — see [LICENSE](LICENSE).

EchoMuse vendors and links several third-party components, each keeping its own
licence. They are inventoried in [NOTICE.md](NOTICE.md); note that the device
binary links two BSD-3-Clause components, whose copyright notices that file
carries on the binary's behalf.
