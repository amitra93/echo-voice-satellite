from __future__ import annotations

import asyncio
import types

import pytest

import em_sendspin


class FakeServerInfo:
    server_id = "server-id"


@pytest.mark.parametrize(("raw", "expected"), [
    ("", ""),
    ("  ", ""),
    ("music-assistant.local", "ws://music-assistant.local:8927/sendspin"),
    ("192.168.0.3:8927", "ws://192.168.0.3:8927/sendspin"),
    ("ws://ma:9000/custom", "ws://ma:9000/custom"),
    ("wss://ma.example/sendspin", "wss://ma.example:8927/sendspin"),
    ("ws://[2001:db8::1]:8927/sendspin", "ws://[2001:db8::1]:8927/sendspin"),
])
def test_normalize_server_url(raw, expected):
    assert em_sendspin.normalize_server_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "http://ma:8927", "ws://user:secret@ma:8927", "ws://ma:0",
    "ws://ma:70000", "ws://ma:8927/sendspin?token=x",
])
def test_normalize_server_url_rejects_unsafe_or_invalid_values(raw):
    with pytest.raises(ValueError):
        em_sendspin.normalize_server_url(raw)


def test_normalize_server_url_adds_path_and_rejects_bad_port_syntax():
    assert em_sendspin.normalize_server_url("ws://ma/custom") == "ws://ma:8927/custom"
    with pytest.raises(ValueError):
        em_sendspin.normalize_server_url("ws://ma:not-a-port")


class FakeClient:
    def __init__(self):
        self.connected = False
        self.server_info = None
        self.callbacks = {}
        self.connect_calls = []
        self.disconnect_calls = 0

    def _add(self, name, callback):
        self.callbacks[name] = callback
        return lambda: self.callbacks.pop(name, None)

    add_audio_chunk_listener = lambda self, callback: self._add("audio", callback)
    add_stream_start_listener = lambda self, callback: self._add("start", callback)
    add_stream_clear_listener = lambda self, callback: self._add("clear", callback)
    add_stream_end_listener = lambda self, callback: self._add("end", callback)
    add_disconnect_listener = lambda self, callback: self._add("disconnect", callback)

    def is_time_synchronized(self):
        return self.connected

    async def connect(self, url, *, expected_server_id=None):
        self.connect_calls.append((url, expected_server_id))
        self.connected = True
        self.server_info = FakeServerInfo()

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False
        self.callbacks["disconnect"]()

    def compute_play_time(self, timestamp):
        return timestamp + 10


def test_adapter_rejects_missing_identity_fields():
    with pytest.raises(ValueError):
        em_sendspin.SendspinPlayer("", "Study")
    with pytest.raises(ValueError):
        em_sendspin.SendspinPlayer("study", "")


def test_attach_registers_callbacks_once_and_exposes_state():
    client = FakeClient()
    player = em_sendspin.SendspinPlayer("study", "Study")

    player.attach(client)
    assert set(client.callbacks) == {"audio", "start", "clear", "end", "disconnect"}
    assert player.state.connected is False
    with pytest.raises(RuntimeError):
        player.attach(FakeClient())


def test_lifecycle_and_callbacks_are_forwarded():
    client = FakeClient()
    events = []
    player = em_sendspin.SendspinPlayer(
        "study",
        "Study",
        on_audio=lambda *args: events.append(("audio", args)),
        on_stream_start=lambda value: events.append(("start", value)),
        on_stream_clear=lambda value: events.append(("clear", value)),
        on_stream_end=lambda value: events.append(("end", value)),
        on_disconnect=lambda: events.append(("disconnect",)),
    )
    player.attach(client)

    async def run():
        await player.connect("ws://sendspin.example/sendspin", expected_server_id="server-id")
        assert player.state.connected is True
        assert player.state.synchronized is True
        assert player.state.available is True
        assert player.state.server_id == "server-id"
        await player.disconnect()

    asyncio.run(run())
    assert client.connect_calls == [("ws://sendspin.example/sendspin", None)]
    assert client.disconnect_calls == 1
    assert events == [("disconnect",)]
    assert player.state.available is False


def test_audio_and_stream_events_are_forwarded():
    client = FakeClient()
    events = []
    player = em_sendspin.SendspinPlayer(
        "study",
        "Study",
        on_audio=lambda *args: events.append(("audio", args)),
        on_stream_start=lambda value: events.append(("start", value)),
        on_stream_clear=lambda value: events.append(("clear", value)),
        on_stream_end=lambda value: events.append(("end", value)),
    )
    player.attach(client)
    client.callbacks["audio"](123, b"pcm", "format")
    client.callbacks["start"]("start-message")
    client.callbacks["clear"](["player"])
    client.callbacks["end"](None)

    assert events == [
        ("audio", (123, b"pcm", "format")),
        ("start", "start-message"),
        ("clear", ["player"]),
        ("end", None),
    ]
    assert player.compute_play_time(100) == 110


def test_player_updates_metadata_group_and_controller_state():
    player = em_sendspin.SendspinPlayer("study", "Study")
    states = []
    player.set_state_listener(lambda state: states.append((state.title, state.playback_state, state.volume)))

    player._metadata(types.SimpleNamespace(metadata=types.SimpleNamespace(title="Song", artist="Artist", album="Album")))
    player._group(types.SimpleNamespace(playback_state=types.SimpleNamespace(value="paused")))
    player._controller(types.SimpleNamespace(controller=types.SimpleNamespace(volume=35, muted=True)))
    player._stream_start({"media_title": "Live", "media_artist": "Band"})
    assert player.state.title == "Live"
    assert player.state.artist == "Band"
    assert player.state.album == "Album"
    assert player.state.playback_state == "playing"
    assert player.state.volume == 35 and player.state.muted is True
    assert states


def test_global_runtime_helpers_forward_and_noop():
    class Runtime:
        def __init__(self):
            self.calls = []

        async def command(self, *args, **kwargs):
            self.calls.append(("command", args, kwargs))

        async def register_device(self, *args):
            self.calls.append(("register", args))

        async def unregister_device(self, *args):
            self.calls.append(("unregister", args))

        async def yield_device(self, *args):
            self.calls.append(("yield", args))

        async def release_device(self, *args):
            self.calls.append(("release", args))

        async def configure(self, *args):
            self.calls.append(("configure", args))

        def status(self):
            return {"devices": ["study"]}

    runtime = Runtime()
    em_sendspin.set_runtime(runtime)

    async def run():
        await em_sendspin.command("study", "mute", mute=True)
        await em_sendspin.register_device("study", "Study", object())
        await em_sendspin.unregister_device("study")
        await em_sendspin.yield_device("study")
        await em_sendspin.release_device("study")
        await em_sendspin.configure("ma.local")

    try:
        asyncio.run(run())
        assert len(runtime.calls) == 6
        assert em_sendspin.runtime_status() == {"devices": ["study"]}
    finally:
        em_sendspin.set_runtime(None)

    assert em_sendspin.runtime_status()["devices"] == []
    asyncio.run(em_sendspin.unregister_device("missing"))


def test_detach_unsubscribes_all_callbacks_and_resets_state():
    client = FakeClient()
    player = em_sendspin.SendspinPlayer("study", "Study")
    player.attach(client)
    player.state.connected = True

    player.detach()

    assert player.client is None
    assert player.state == em_sendspin.PlayerState()
    assert client.callbacks == {}
    with pytest.raises(RuntimeError):
        player.compute_play_time(1)


def test_connect_and_disconnect_require_attachment():
    player = em_sendspin.SendspinPlayer("study", "Study")

    async def run():
        with pytest.raises(RuntimeError):
            await player.connect("ws://sendspin.example/sendspin")
        await player.disconnect()

    asyncio.run(run())
