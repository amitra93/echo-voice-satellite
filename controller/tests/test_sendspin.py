import asyncio
import sys
import types

import pytest

import em_sendspin as sendspin


def test_normalize_server_url_accepts_hosts_urls_and_ipv6():
    assert sendspin.normalize_server_url("") == ""
    assert sendspin.normalize_server_url(" ma.local ") == "ws://ma.local:8927/sendspin"
    assert sendspin.normalize_server_url("wss://ma.local:9999/custom") == "wss://ma.local:9999/custom"
    assert sendspin.normalize_server_url("[2001:db8::1]") == "ws://[2001:db8::1]:8927/sendspin"
    assert sendspin.normalize_server_url("ws://ma.local/path") == "ws://ma.local:8927/path"

    for value in ("http://ma.local", "ws://user:pass@ma.local", "ws://ma.local:0", "ws://ma.local:65536", "ws://ma.local/a?q=1", "ws://ma.local#a"):
        with pytest.raises(ValueError):
            sendspin.normalize_server_url(value)
    with pytest.raises(ValueError, match="invalid port"):
        sendspin.normalize_server_url("ws://ma.local:notaport")


def test_runtime_module_helpers_and_status(monkeypatch):
    class Runtime:
        def __init__(self):
            self.calls = []

        async def command(self, *args, **kwargs): self.calls.append(("command", args, kwargs))
        async def register_device(self, *args): self.calls.append(("register", args))
        async def unregister_device(self, *args): self.calls.append(("unregister", args))
        async def yield_device(self, *args): self.calls.append(("yield", args))
        async def release_device(self, *args): self.calls.append(("release", args))
        async def configure(self, *args): self.calls.append(("configure", args))
        def status(self): return {"devices": ["d"]}

    async def run():
        sendspin.set_runtime(None)
        assert sendspin.runtime_status()["discovery"] is False
        with pytest.raises(RuntimeError):
            await sendspin.command("d", "x")
        runtime = Runtime()
        sendspin.set_runtime(runtime)
        await sendspin.command("d", "x", n=1)
        await sendspin.register_device("d", "D", object())
        await sendspin.unregister_device("d")
        await sendspin.yield_device("d")
        await sendspin.release_device("d")
        await sendspin.configure("ma.local")
        assert sendspin.runtime_status() == {"devices": ["d"]}
        sendspin.set_runtime(None)
        await sendspin.unregister_device("d")
        await sendspin.yield_device("d")
        await sendspin.release_device("d")
        await sendspin.configure("")
        assert runtime.calls[0] == ("command", ("d", "x"), {"n": 1})

    asyncio.run(run())


class FakeSdkClient:
    def __init__(self, *, connected=False, synchronized=False):
        self.connected = connected
        self._synchronized = synchronized
        self.server_info = types.SimpleNamespace(server_id="server")
        self.callbacks = {}
        self.groups = []
        self.disconnected = False

    def _add(self, name, callback):
        self.callbacks[name] = callback
        return lambda: self.callbacks.pop(name, None)

    add_audio_chunk_listener = lambda self, cb: self._add("audio", cb)
    add_stream_start_listener = lambda self, cb: self._add("start", cb)
    add_stream_clear_listener = lambda self, cb: self._add("clear", cb)
    add_stream_end_listener = lambda self, cb: self._add("end", cb)
    add_disconnect_listener = lambda self, cb: self._add("disconnect", cb)
    add_metadata_listener = lambda self, cb: self._add("metadata", cb)
    add_group_update_listener = lambda self, cb: self._add("group", cb)
    add_controller_state_listener = lambda self, cb: self._add("controller", cb)

    def is_time_synchronized(self): return self._synchronized
    async def connect(self, url): self.url = url; self.connected = True
    def disconnect(self): self.disconnected = True; self.connected = False
    async def send_group_command(self, command, **kwargs): self.groups.append((command, kwargs))
    async def attach_websocket(self, websocket): self.websocket = websocket
    def compute_play_time(self, timestamp): return timestamp + 7


def test_sendspin_player_callbacks_and_lifecycle():
    audio, starts, clears, ends, disconnects, states = [], [], [], [], [], []
    player = sendspin.SendspinPlayer(
        "d", "Dot", on_audio=lambda *x: audio.append(x), on_stream_start=starts.append,
        on_stream_clear=clears.append, on_stream_end=ends.append,
        on_disconnect=lambda: disconnects.append(True),
    )
    with pytest.raises(ValueError): sendspin.SendspinPlayer("", "Dot")
    with pytest.raises(ValueError): sendspin.SendspinPlayer("d", "")
    client = FakeSdkClient(connected=True, synchronized=True)
    player.attach(client)
    player.set_state_listener(states.append)
    with pytest.raises(RuntimeError): player.attach(FakeSdkClient())
    client.callbacks["audio"](1, b"pcm", "fmt")
    client.callbacks["start"]({"title": "Song", "artist": "Band"})
    client.callbacks["group"](types.SimpleNamespace(playback_state=types.SimpleNamespace(value="paused")))
    client.callbacks["controller"](types.SimpleNamespace(controller=types.SimpleNamespace(volume=22, muted=True)))
    client.callbacks["metadata"](types.SimpleNamespace(metadata=types.SimpleNamespace(title="T", artist="A", album="L")))
    client.callbacks["clear"](["player"])
    client.callbacks["end"](["player"])
    client.callbacks["disconnect"]()
    assert audio == [(1, b"pcm", "fmt")]
    assert player.state.title == "T" and player.state.artist == "A" and player.state.album == "L"
    assert player.state.volume == 22 and player.state.muted and not player.state.connected
    assert starts and clears == [["player"]] and ends == [["player"]] and disconnects == [True]
    assert states

    async def run():
        media_types = types.ModuleType("aiosendspin.models.types")
        media_types.MediaCommand = lambda value: value
        sys.modules["aiosendspin"] = types.ModuleType("aiosendspin")
        sys.modules["aiosendspin.models"] = types.ModuleType("aiosendspin.models")
        sys.modules["aiosendspin.models.types"] = media_types
        await player.connect("ws://server", expected_server_id="ignored")
        assert player.state.available
        await player.send_group_command("volume", volume=4)
        await player.serve_websocket(object())
        await player.disconnect()

    asyncio.run(run())
    assert client.groups
    player.detach()
    assert player.client is None and not player.state.connected
    with pytest.raises(RuntimeError): player.compute_play_time(1)


def test_player_optional_callbacks_and_undefined_fields():
    player = sendspin.SendspinPlayer("d", "D")
    client = FakeSdkClient()
    client.add_metadata_listener = None
    client.add_group_update_listener = None
    client.add_controller_state_listener = None
    player.attach(client)
    undefined = type("UndefinedField", (), {})()
    player._metadata(types.SimpleNamespace(metadata=undefined))
    player._metadata(types.SimpleNamespace(metadata=None))
    player._controller(types.SimpleNamespace(controller=undefined))
    player._stream_clear(["controller"])
    player._stream_end(["metadata"])
    assert player.state.playback_state == "idle"


def test_player_support_and_sdk_client(monkeypatch):
    class Command:
        VOLUME = "volume"
        MUTE = "mute"

    class Roles:
        PLAYER = "player"
        CONTROLLER = "controller"
        METADATA = "metadata"

    class MediaCommand:
        def __new__(cls, value):
            if value == "bad": raise ValueError
            return value

    class Client:
        def __init__(self, *args, **kwargs): self.args, self.kwargs = args, kwargs
        async def send_group_command(self, *args, **kwargs): self.sent = (args, kwargs)

    monkeypatch.setitem(sys.modules, "aiosendspin", types.ModuleType("aiosendspin"))
    monkeypatch.setitem(sys.modules, "aiosendspin.models", types.ModuleType("aiosendspin.models"))
    models_player = types.ModuleType("aiosendspin.models.player")
    models_player.ClientHelloPlayerSupport = lambda **kwargs: kwargs
    models_player.SupportedAudioFormat = lambda **kwargs: kwargs
    models_types = types.ModuleType("aiosendspin.models.types")
    models_types.AudioCodec = types.SimpleNamespace(PCM="pcm")
    models_types.PlayerCommand = Command
    models_types.Roles = Roles
    models_types.MediaCommand = MediaCommand
    client_mod = types.ModuleType("aiosendspin.client")
    client_mod.SendspinClient = Client
    for name, module in (("aiosendspin.models.player", models_player), ("aiosendspin.models.types", models_types), ("aiosendspin.client", client_mod)):
        monkeypatch.setitem(sys.modules, name, module)
    support = sendspin.player_support(buffer_capacity=12)
    assert support["buffer_capacity"] == 12 and support["supported_formats"][0]["channels"] == 1

    async def run():
        client = await sendspin.create_sdk_client("id", "D", buffer_capacity=3, controller_role=False, metadata_role=False)
        assert client.args[2] == [Roles.PLAYER]

    asyncio.run(run())

    player = sendspin.SendspinPlayer("d", "D")
    player.client = Client()
    async def command():
        await player.send_group_command("play")
        with pytest.raises(ValueError, match="unsupported"):
            await player.send_group_command("bad")
    asyncio.run(command())


class FakeDevice:
    def __init__(self):
        self.clock_sync = types.SimpleNamespace(synchronized=True, controller_to_device=lambda x: x + 10)
        self.sent = []

    async def send_data(self, data): self.sent.append(data)


def install_sync_stubs(monkeypatch):
    sync = types.ModuleType("em_music_sync")
    sync.PcmFrame = lambda *args: args
    sync.encode_start = lambda generation: b"S" + bytes([generation])
    sync.encode_clear = lambda generation: b"C" + bytes([generation])
    sync.encode_end = lambda generation: b"E" + bytes([generation])
    sync.encode_pcm = lambda frame: b"P" + repr(frame).encode()
    monkeypatch.setitem(sys.modules, "em_music_sync", sync)


def test_session_queue_writer_callbacks_and_legacy_handoff(monkeypatch):
    install_sync_stubs(monkeypatch)
    monkeypatch.setitem(sys.modules, "em_eq", types.SimpleNamespace(StreamingEQ=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "em_limiter", types.SimpleNamespace(Limiter=object, DEFAULT_THRESHOLD_DB=-1))
    monkeypatch.setitem(sys.modules, "em_mbc", types.SimpleNamespace(BassGuard=object))
    stopped = []
    async def stop_legacy(device_id):
        stopped.append(device_id)
    monkeypatch.setitem(sys.modules, "em_player", types.SimpleNamespace(stop=stop_legacy))
    device = FakeDevice()
    session = sendspin.SendspinDeviceSession("d", "D", device, device.clock_sync)
    session.player.attach(FakeSdkClient())
    async def run():
        session.on_stream_start({"title": "x"})
        session.on_audio(5, b"raw", None)
        session.on_audio(6, b"raw2", None)
        assert session._generation == 1 and session._sequence == 2
        session.on_stream_clear(["controller"])
        session.on_stream_clear(["player"])
        session.on_stream_end(["player"])
        await session.start()
        await asyncio.sleep(0)
        assert device.sent[0].startswith(b"C")
        await session.yield_to_legacy()
        assert session._legacy_owns and session.player.client is not None
        await session.yield_to_legacy()
        await session.release_legacy()
        await session.release_legacy()
        session._legacy_owns = True
        session.on_audio(7, b"late", None)
        await session.close()

    asyncio.run(run())
    assert device.sent and device.sent[0].startswith(b"C")
    assert stopped == ["d"]
    assert session.rejected_frames >= 1


def test_session_rejects_bad_audio_and_writer_encode_errors(monkeypatch):
    install_sync_stubs(monkeypatch)
    device = FakeDevice()
    session = sendspin.SendspinDeviceSession("d", "D", device, device.clock_sync)
    session._generation = 1
    session.player.client = types.SimpleNamespace(compute_play_time=lambda _: (_ for _ in ()).throw(ValueError()))
    session.on_audio(1, b"x", None)
    session._legacy_owns = True
    session.on_audio(1, b"x", None)
    session._legacy_owns = False
    session._encode_pcm = lambda _: (_ for _ in ()).throw(RuntimeError())
    session._enqueue(("pcm", 1, 0, 0, b"x"))

    async def run():
        task = asyncio.create_task(session._writer())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert session.rejected_frames == 3


def test_listener_register_advertise_route_and_stop(monkeypatch):
    class Zc:
        def __init__(self): self.registered, self.unregistered, self.closed = [], [], False
        async def async_register_service(self, service, allow_name_change): self.registered.append((service, allow_name_change))
        async def async_unregister_service(self, service): self.unregistered.append(service)
        async def async_close(self): self.closed = True

    class Service:
        def __init__(self, **kwargs): self.kwargs = kwargs

    zc = Zc()
    listener = sendspin.SendspinListener(port=0, advertise_address="192.0.2.9", zeroconf_factory=lambda: zc, service_info_factory=Service)
    player = sendspin.SendspinPlayer("id", "Dot")
    with pytest.raises(ValueError): sendspin.SendspinListener(port=65536)

    async def run():
        assert listener.app
        await listener.register_player("id", player)
        await listener.start()
        assert listener.port > 0 and zc.registered
        with pytest.raises(ValueError): await listener.register_player("id", player)
        await listener.unregister_player("missing")
        await listener.unregister_player("id")
        assert not listener.players
        await listener.stop()
        await listener.stop()

    asyncio.run(run())
    assert zc.closed and zc.unregistered


def test_listener_websocket_unknown_and_adapter_errors():
    class Websocket:
        closed = False
        async def prepare(self, request): return None
        async def close(self, **kwargs): self.closed = True; self.kwargs = kwargs

    class Request:
        match_info = {"client_id": "missing"}

    listener = sendspin.SendspinListener(advertise=False)
    original = sendspin.web.WebSocketResponse
    sendspin.web.WebSocketResponse = Websocket
    try:
        async def run():
            response = await listener._handle_websocket(Request())
            assert response.closed and response.kwargs["code"] == 1008
        asyncio.run(run())
    finally:
        sendspin.web.WebSocketResponse = original


def test_discovery_timeout_cleans_up_zeroconf(monkeypatch):
    class State:
        Added = "added"
        Updated = "updated"

    class Browser:
        def __init__(self, *args, **kwargs): self.cancelled = False
        async def async_cancel(self): self.cancelled = True

    class Zc:
        def __init__(self): self.zeroconf = object(); self.closed = False
        async def async_close(self): self.closed = True

    zc = Zc()
    zasync = types.ModuleType("zeroconf.asyncio")
    zasync.AsyncServiceBrowser = Browser
    zasync.AsyncServiceInfo = object
    zasync.AsyncZeroconf = lambda: zc
    zeroconf = types.ModuleType("zeroconf")
    zeroconf.ServiceStateChange = State
    monkeypatch.setitem(sys.modules, "zeroconf", zeroconf)
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", zasync)

    assert asyncio.run(sendspin.discover_server_url(timeout=0.001)) is None
    assert zc.closed


def test_listener_advertisement_and_websocket_error(monkeypatch):
    class Zc:
        async def async_register_service(self, service, allow_name_change): self.service = service
        async def async_unregister_service(self, service): self.removed = service
        async def async_close(self): pass

    class Service:
        def __init__(self, **kwargs): self.kwargs = kwargs

    class ErrorPlayer(sendspin.SendspinPlayer):
        async def serve_websocket(self, websocket): raise PermissionError("no")

    listener = sendspin.SendspinListener(advertise=False, zeroconf_factory=Zc, service_info_factory=Service)
    player = ErrorPlayer("id", "Dot")

    async def run():
        await listener._start_zeroconf()
        await listener._start_zeroconf()
        await listener.register_player("id", player)
        await listener._advertise_player("id", player)
        assert listener._services["id"].kwargs["properties"]["name"] == "Dot"
        await listener._handle_websocket(types.SimpleNamespace(match_info={"client_id": "id"}))
        await listener._unadvertise_player("id")

    original = sendspin.web.WebSocketResponse
    class Websocket:
        closed = False
        async def prepare(self, request): pass
        async def close(self, **kwargs): self.closed = True; self.kwargs = kwargs
    sendspin.web.WebSocketResponse = Websocket
    try:
        asyncio.run(run())
    finally:
        sendspin.web.WebSocketResponse = original


def test_runtime_status_and_command_paths(monkeypatch):
    runtime = sendspin.SendspinRuntime(None, "/tmp")
    session = types.SimpleNamespace(device_id="d", client_id="cid", player=types.SimpleNamespace(name="D", state=sendspin.PlayerState(),
        send_group_command=lambda *a, **k: None,
    ), closed=False)
    runtime.sessions["d"] = session
    assert runtime.status()["devices"][0]["device_id"] == "d"

    async def run():
        await runtime.yield_device("missing")
        await runtime.release_device("missing")
        runtime.update_device_config("missing")
        with pytest.raises(RuntimeError): await runtime.command("missing", "mute")

    asyncio.run(run())


def test_runtime_register_configure_command_and_close(monkeypatch, tmp_path):
    install_sync_stubs(monkeypatch)
    monkeypatch.setattr("em_ha_sidechannels.sendspin_state", lambda *args, **kwargs: None, raising=False)
    created = []

    async def create(*args, **kwargs):
        created.append((args, kwargs))
        return FakeSdkClient()

    monkeypatch.setattr(sendspin, "create_sdk_client", create)
    listener = types.SimpleNamespace(registered=[], unregistered=[])
    async def register(*args): listener.registered.append(args)
    async def unregister(*args): listener.unregistered.append(args)
    listener.register_player = register
    listener.unregister_player = unregister
    runtime = sendspin.SendspinRuntime(listener, str(tmp_path))
    device = FakeDevice()
    device.volume = 0.4
    device.muted = True

    async def run():
        with pytest.raises(RuntimeError):
            await runtime.register_device("d", "D", types.SimpleNamespace(clock_sync=types.SimpleNamespace(synchronized=False)))
        session = await runtime.register_device("d", "D", device)
        assert runtime.sessions["d"] is session and created[0][0][0] == "echomuse-d"
        assert await runtime.register_device("d", "D", device) is session
        await runtime.configure("ws://server:9000")
        assert runtime.discovery is False and runtime.resolved_url.endswith(":9000/sendspin")
        await runtime.command("d", "volume", volume=55)
        await runtime.command("d", "mute", mute=True)
        assert session.player.state.volume == 55 and session.player.state.muted
        await runtime.close()
        assert not runtime.sessions

    asyncio.run(run())
    assert listener.registered and listener.unregistered


def test_runtime_connection_loop_success_and_failure(monkeypatch):
    class Player:
        def __init__(self, fail=False):
            self.state = sendspin.PlayerState()
            self.fail = fail
            self.calls = 0

        async def connect(self, url):
            self.calls += 1
            if self.fail:
                self.session.closed = True
                raise OSError("offline")
            self.state.connected = True
            self.session.disconnected.set()
            self.session.closed = True

        def publish(self, state): self.session.states.append(state.error)

    async def run():
        runtime = sendspin.SendspinRuntime(None, "/tmp")
        for fail in (False, True):
            player = Player(fail)
            session = types.SimpleNamespace(
                device_id="d", player=player, closed=False, _legacy_owns=False,
                disconnected=asyncio.Event(), _rearm=asyncio.Event(), states=[],
                publish_state=player.publish, connection_task=None,
            )
            player.session = session
            runtime.sessions["d"] = session
            if fail:
                await runtime._connection_loop(session, "ws://server")
                assert session.player.state.error == "offline"
            else:
                await runtime._connection_loop(session, "ws://server")
                assert player.calls == 1
            runtime.sessions.clear()

    asyncio.run(run())


def test_runtime_discovery_loop_restarts_sessions(monkeypatch):
    runtime = sendspin.SendspinRuntime(None, "/tmp")
    session = types.SimpleNamespace(device_id="d", closed=False, connection_task=None)
    runtime.sessions["d"] = session
    restarted = []

    async def discover():
        runtime.discovery = False
        return "ws://found:8927/sendspin"

    async def restart(value): restarted.append(value)

    monkeypatch.setattr(sendspin, "discover_server_url", discover)
    monkeypatch.setattr(runtime, "_restart_connection", restart)
    runtime.discovery = True
    asyncio.run(runtime._discovery_loop())
    assert runtime.resolved_url == "ws://found:8927/sendspin"
    assert restarted == [session]
