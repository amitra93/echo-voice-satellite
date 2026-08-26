from __future__ import annotations

import asyncio
import sys
import types

import em_sendspin


def test_create_sdk_client_builds_mono_pcm_player(monkeypatch):
    class FakeEnum:
        def __init__(self, value):
            self.value = value

    class FakeModel:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    package = types.ModuleType("aiosendspin")
    client_module = types.ModuleType("aiosendspin.client")
    client_module.SendspinClient = FakeClient
    models = types.ModuleType("aiosendspin.models")
    player = types.ModuleType("aiosendspin.models.player")
    player.ClientHelloPlayerSupport = FakeModel
    player.SupportedAudioFormat = FakeModel
    types_module = types.ModuleType("aiosendspin.models.types")
    types_module.AudioCodec = types.SimpleNamespace(PCM=FakeEnum("pcm"))
    types_module.PlayerCommand = types.SimpleNamespace(
        VOLUME=FakeEnum("volume"), MUTE=FakeEnum("mute")
    )
    types_module.Roles = types.SimpleNamespace(
        PLAYER=FakeEnum("player@v1"),
        CONTROLLER=FakeEnum("controller@v1"),
        METADATA=FakeEnum("metadata@v1"),
    )
    monkeypatch.setitem(sys.modules, "aiosendspin", package)
    monkeypatch.setitem(sys.modules, "aiosendspin.client", client_module)
    monkeypatch.setitem(sys.modules, "aiosendspin.models", models)
    monkeypatch.setitem(sys.modules, "aiosendspin.models.player", player)
    monkeypatch.setitem(sys.modules, "aiosendspin.models.types", types_module)

    client = asyncio.run(em_sendspin.create_sdk_client(
        "echomuse-study", "Study", buffer_capacity=1234,
    ))

    assert client.args == (
        "echomuse-study",
        "Study",
        [types_module.Roles.PLAYER, types_module.Roles.CONTROLLER, types_module.Roles.METADATA],
    )
    support = client.kwargs["player_support"]
    assert support.buffer_capacity == 1234
    assert support.supported_formats[0].codec.value == "pcm"
    assert support.supported_formats[0].sample_rate == 48000
    assert support.supported_formats[0].channels == 1
    assert support.supported_formats[0].bit_depth == 16
    assert client.kwargs["required_lead_time_ms"] == em_sendspin.REQUIRED_LEAD_MS
    assert client.kwargs["min_buffer_ms"] == em_sendspin.MIN_BUFFER_MS
