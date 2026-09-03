# EchoMuse

[![Unit Tests](https://github.com/amitra93/echo-voice-satellite/actions/workflows/unit-test.yaml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/unit-test.yaml)
[![Controller Build](https://github.com/amitra93/echo-voice-satellite/actions/workflows/controller-build.yml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/controller-build.yml)
[![Forge Build](https://github.com/amitra93/echo-voice-satellite/actions/workflows/forge-build.yml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/forge-build.yml)
[![Device Build](https://github.com/amitra93/echo-voice-satellite/actions/workflows/device-build.yml/badge.svg)](https://github.com/amitra93/echo-voice-satellite/actions/workflows/device-build.yml)

A fork of https://github.com/wilbowes/EchoMuse that has the following differences (as of August 2026):

*  **Streaming STT via Gemini 3.5 Speech to Text models.**
*  **Streaming TTS integration via Google Cloud TTS.**
*  ***Custom HACS integration**: Devices appear through a custom HACS integration rather than ESPHome. This allows for more control and will be used to add speech-to-speech models in the near future.
*  **Amazon audio framework** Audio is routed through Amazon Echo Android APIs, leading to richer and deeper sound.
*  **Better trainer**: Changes to `oww_forge` to make wakeword training easier.
*  **More on-device work**: Wake word and stopword detection run entirely on the devices — the controller never receives idle microphone audio and never scores a wake or stop model.

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
