"""Controller-owned Sendspin client, discovery, pairing, and device transport."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web

SDK_VERSION = "6.0.5"
SENDSPIN_PATH = "/sendspin"
SENDSPIN_PORT = 8928
_runtime: "SendspinRuntime | None" = None
log = logging.getLogger("sendspin")

# Fixed initial format. It matches the current EchoMuse device music wire
# format and avoids introducing a decoder or stereo conversion in phase 1.
PCM_SAMPLE_RATE = 48_000
PCM_CHANNELS = 1
PCM_BIT_DEPTH = 16


def normalize_server_url(value: str) -> str:
    """Normalize one Music Assistant address into a Sendspin WebSocket URL.

    An empty value deliberately stays empty: it means discover a
    ``_sendspin-server._tcp.local.`` service. Explicit addresses accept the
    convenient ``host[:port]`` form as well as full ws/wss URLs.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"ws://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in ("ws", "wss"):
        raise ValueError("Music Assistant URL must use ws:// or wss://")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Music Assistant URL must contain only a host and optional port")
    try:
        parsed_port = parsed.port
        port = parsed_port if parsed_port is not None else 8927
    except ValueError as exc:
        raise ValueError("Music Assistant URL has an invalid port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Music Assistant URL port must be between 1 and 65535")
    host = parsed.hostname
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    path = parsed.path or "/sendspin"
    if not path.startswith("/"):
        path = f"/{path}"
    if parsed.query or parsed.fragment:
        raise ValueError("Music Assistant URL cannot contain a query or fragment")
    return urlunsplit((parsed.scheme, authority, path, "", ""))


async def discover_server_url(timeout: float = 5.0) -> str | None:
    """Return the first Music Assistant Sendspin server advertised over mDNS."""
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    service_type = "_sendspin-server._tcp.local."
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str | None] = loop.create_future()
    tasks: set[asyncio.Task] = set()
    azc = AsyncZeroconf()

    async def resolve(name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(azc.zeroconf, int(timeout * 1000)):
            return
        addresses = info.parsed_addresses()
        if not addresses or not info.port or result.done():
            return
        properties = {
            key.decode("utf-8", "replace"): value.decode("utf-8", "replace")
            for key, value in (info.properties or {}).items()
        }
        path = properties.get("path", "/sendspin")
        host = addresses[0]
        authority = f"[{host}]:{info.port}" if ":" in host else f"{host}:{info.port}"
        result.set_result(normalize_server_url(f"ws://{authority}{path}"))

    def on_change(_zc, _type, name, state) -> None:
        if state not in (ServiceStateChange.Added, ServiceStateChange.Updated):
            return
        task = asyncio.create_task(resolve(name))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    browser = AsyncServiceBrowser(azc.zeroconf, service_type, handlers=[on_change])
    try:
        return await asyncio.wait_for(result, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        await browser.async_cancel()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await azc.async_close()

AudioCallback = Callable[[int, bytes, Any], None]
StreamCallback = Callable[[Any], None]
RolesCallback = Callable[[list[str] | None], None]
DisconnectCallback = Callable[[], None]


def set_runtime(runtime: "SendspinRuntime | None") -> None:
    global _runtime
    _runtime = runtime


async def command(device_id: str, command_name: str, **kwargs: Any) -> None:
    if _runtime is None:
        raise RuntimeError("Sendspin runtime is unavailable")
    await _runtime.command(device_id, command_name, **kwargs)


async def register_device(device_id: str, name: str, device: Any) -> None:
    if _runtime is None:
        return
    await _runtime.register_device(device_id, name, device)


async def unregister_device(device_id: str) -> None:
    if _runtime is not None:
        await _runtime.unregister_device(device_id)


async def configure(server_url: str) -> None:
    if _runtime is not None:
        await _runtime.configure(server_url)


def runtime_status() -> dict[str, Any]:
    return _runtime.status() if _runtime is not None else {
        "configured_url": "", "resolved_url": None, "discovery": False, "devices": []
    }


@dataclass
class PlayerState:
    """Controller-facing state that does not expose SDK model objects."""

    connected: bool = False
    synchronized: bool = False
    available: bool = False
    server_id: str | None = None
    volume: int = 100
    muted: bool = False
    playback_state: str = "idle"
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    error: str | None = None


def player_support(*, buffer_capacity: int, commands: list[Any] | None = None) -> Any:
    """Build the pinned SDK's mono PCM player support object.

    The import is local so pure controller tests and tools that do not install
    the optional runtime dependencies can still import the module and exercise
    its lifecycle adapter.
    """
    from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
    from aiosendspin.models.types import AudioCodec, PlayerCommand

    supported_commands = commands or [PlayerCommand.VOLUME, PlayerCommand.MUTE]
    return ClientHelloPlayerSupport(
        supported_formats=[
            SupportedAudioFormat(
                codec=AudioCodec.PCM,
                sample_rate=PCM_SAMPLE_RATE,
                channels=PCM_CHANNELS,
                bit_depth=PCM_BIT_DEPTH,
            )
        ],
        buffer_capacity=buffer_capacity,
        supported_commands=supported_commands,
    )


async def create_sdk_client(
    client_id: str,
    name: str,
    *,
    buffer_capacity: int,
    initial_volume: int = 100,
    initial_muted: bool = False,
    controller_role: bool = True,
    metadata_role: bool = True,
) -> Any:
    """Construct the pinned SDK client without leaking SDK types to callers."""
    from aiosendspin.client import SendspinClient
    from aiosendspin.models.types import Roles

    roles = [Roles.PLAYER]
    if controller_role:
        roles.append(Roles.CONTROLLER)
    if metadata_role:
        roles.append(Roles.METADATA)

    return SendspinClient(
        client_id,
        name,
        roles,
        player_support=player_support(buffer_capacity=buffer_capacity),
        initial_volume=initial_volume,
        initial_muted=initial_muted,
    )


class SendspinPlayer:
    """A fakeable lifecycle and callback adapter for one Echo player."""

    def __init__(
        self,
        device_id: str,
        name: str,
        *,
        on_audio: AudioCallback | None = None,
        on_stream_start: StreamCallback | None = None,
        on_stream_clear: RolesCallback | None = None,
        on_stream_end: RolesCallback | None = None,
        on_disconnect: DisconnectCallback | None = None,
    ) -> None:
        if not device_id:
            raise ValueError("device_id is required")
        if not name:
            raise ValueError("name is required")
        self.device_id = device_id
        self.name = name
        self.state = PlayerState()
        self.client: Any | None = None
        self._on_audio = on_audio
        self._on_stream_start = on_stream_start
        self._on_stream_clear = on_stream_clear
        self._on_stream_end = on_stream_end
        self._on_disconnect = on_disconnect
        self._on_state = None
        self._unsubscribers: list[Callable[[], None]] = []

    def attach(self, client: Any) -> None:
        """Attach an SDK client and register all callbacks exactly once."""
        if self.client is not None:
            raise RuntimeError("Sendspin client is already attached")
        self.client = client
        self._register("add_audio_chunk_listener", self._audio)
        self._register("add_stream_start_listener", self._stream_start)
        self._register("add_stream_clear_listener", self._stream_clear)
        self._register("add_stream_end_listener", self._stream_end)
        self._register("add_disconnect_listener", self._disconnect)
        self._register("add_metadata_listener", self._metadata, optional=True)
        self._register("add_group_update_listener", self._group, optional=True)
        self._register("add_controller_state_listener", self._controller, optional=True)
        self._refresh_state()

    def set_state_listener(self, callback: Callable[[PlayerState], None] | None) -> None:
        self._on_state = callback
        self._notify_state()

    async def connect(self, url: str, *, expected_server_id: str | None = None) -> None:
        if self.client is None:
            raise RuntimeError("Sendspin client is not attached")
        await self.client.connect(url)
        self._refresh_state()

    async def disconnect(self) -> None:
        if self.client is None:
            return
        result = self.client.disconnect()
        if hasattr(result, "__await__"):
            await result
        self._refresh_state()

    async def send_group_command(self, command: str, **kwargs: Any) -> None:
        """Proxy a standard Sendspin group command to the connected SDK client."""
        if self.client is None:
            raise RuntimeError("Sendspin client is not attached")
        from aiosendspin.models.types import MediaCommand

        try:
            media_command = MediaCommand(command)
        except ValueError as exc:
            raise ValueError(f"unsupported Sendspin group command: {command}") from exc
        await self.client.send_group_command(media_command, **kwargs)

    def _notify_state(self) -> None:
        if self._on_state is not None:
            self._on_state(self.state)

    async def serve_websocket(self, websocket: Any, *, expected_server_id: str | None = None) -> None:
        """Hand an accepted WebSocket to the SDK's server-initiated lifecycle."""
        if self.client is None:
            raise RuntimeError("Sendspin client is not attached")
        await self.client.attach_websocket(websocket)

    def detach(self) -> None:
        """Remove SDK callbacks and release the adapter's client reference."""
        for unsubscribe in reversed(self._unsubscribers):
            unsubscribe()
        self._unsubscribers.clear()
        self.client = None
        self.state = PlayerState()

    def compute_play_time(self, server_timestamp_us: int) -> int:
        if self.client is None:
            raise RuntimeError("Sendspin client is not attached")
        return self.client.compute_play_time(server_timestamp_us)

    def _register(
        self, method: str, callback: Callable[..., None], *, optional: bool = False
    ) -> None:
        register = getattr(self.client, method, None)
        if register is None and optional:
            return
        unsubscribe = register(callback)
        if unsubscribe is not None:
            self._unsubscribers.append(unsubscribe)

    def _refresh_state(self) -> None:
        if self.client is None:
            return
        self.state.connected = bool(getattr(self.client, "connected", False))
        self.state.synchronized = bool(
            self.client.is_time_synchronized()
            if hasattr(self.client, "is_time_synchronized")
            else False
        )
        self.state.available = self.state.connected and self.state.synchronized
        info = getattr(self.client, "server_info", None)
        self.state.server_id = getattr(info, "server_id", None) if info else None
        self._notify_state()

    @staticmethod
    def _is_defined(value: Any) -> bool:
        return value.__class__.__name__ != "UndefinedField"

    def _metadata(self, payload: Any) -> None:
        metadata = getattr(payload, "metadata", None)
        if metadata is None or not self._is_defined(metadata):
            return
        for source, target in (("title", "title"), ("artist", "artist"), ("album", "album")):
            value = getattr(metadata, source, None)
            if self._is_defined(value):
                setattr(self.state, target, value)
        self._notify_state()

    def _group(self, payload: Any) -> None:
        playback = getattr(payload, "playback_state", None)
        if playback is not None:
            self.state.playback_state = getattr(playback, "value", str(playback))
        self._notify_state()

    def _controller(self, payload: Any) -> None:
        controller = getattr(payload, "controller", None)
        if controller is None or not self._is_defined(controller):
            return
        self.state.volume = int(getattr(controller, "volume", self.state.volume))
        self.state.muted = bool(getattr(controller, "muted", self.state.muted))
        self._notify_state()

    def _audio(self, timestamp_us: int, data: bytes, audio_format: Any) -> None:
        if self._on_audio is not None:
            self._on_audio(timestamp_us, data, audio_format)

    def _stream_start(self, message: Any) -> None:
        self.state.playback_state = "playing"
        if isinstance(message, dict):
            self.state.title = message.get("title") or message.get("media_title")
            self.state.artist = message.get("artist") or message.get("media_artist")
        else:
            self.state.title = getattr(message, "title", None)
            self.state.artist = getattr(message, "artist", None)
        self._notify_state()
        if self._on_stream_start is not None:
            self._on_stream_start(message)

    def _stream_clear(self, roles: list[str] | None) -> None:
        self.state.playback_state = "idle"
        self._notify_state()
        if self._on_stream_clear is not None:
            self._on_stream_clear(roles)

    def _stream_end(self, roles: list[str] | None) -> None:
        self.state.playback_state = "idle"
        self._notify_state()
        if self._on_stream_end is not None:
            self._on_stream_end(roles)

    def _disconnect(self) -> None:
        self.state.connected = False
        self.state.synchronized = False
        self.state.available = False
        self.state.server_id = None
        self.state.playback_state = "idle"
        self._notify_state()
        if self._on_disconnect is not None:
            self._on_disconnect()


class SendspinListener:
    """Shared aiohttp listener routing Sendspin connections by client identity."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = SENDSPIN_PORT,
        advertise_address: str | None = None,
        advertise: bool = True,
        zeroconf_factory: Callable[[], Any] | None = None,
        service_info_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not 0 <= port <= 65535:
            raise ValueError("Sendspin port must be between 0 and 65535")
        self.host = host
        self.port = port
        self.advertise_address = advertise_address
        self.advertise = advertise
        self._zeroconf_factory = zeroconf_factory
        self._service_info_factory = service_info_factory
        self._players: dict[str, SendspinPlayer] = {}
        self._services: dict[str, Any] = {}
        self._connections: set[Any] = set()
        self._runner: Any | None = None
        self._site: Any | None = None
        self._zeroconf: Any | None = None
        self._app = web.Application()
        self._app.router.add_get("/sendspin/{client_id}", self._handle_websocket)

    @property
    def app(self) -> web.Application:
        """Return the app for the controller or aiohttp test server to host."""
        return self._app

    @property
    def players(self) -> dict[str, SendspinPlayer]:
        return dict(self._players)

    async def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("Sendspin listener is already started")
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        sockets = getattr(self._site, "_server", None)
        if sockets is not None and sockets.sockets:
            self.port = int(sockets.sockets[0].getsockname()[1])
        if self.advertise:
            await self._start_zeroconf()
            for client_id, player in self._players.items():
                await self._advertise_player(client_id, player)

    async def stop(self) -> None:
        for websocket in list(self._connections):
            await websocket.close(code=1001, message=b"listener stopped")
        self._connections.clear()
        for client_id in list(self._services):
            await self._unadvertise_player(client_id)
        if self._zeroconf is not None:
            await self._zeroconf.async_close()
            self._zeroconf = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def register_player(self, client_id: str, player: SendspinPlayer) -> None:
        if not client_id:
            raise ValueError("Sendspin client_id is required")
        if client_id in self._players:
            raise ValueError(f"Sendspin client already registered: {client_id}")
        self._players[client_id] = player
        if self._runner is not None and self.advertise:
            await self._advertise_player(client_id, player)

    async def unregister_player(self, client_id: str) -> None:
        player = self._players.pop(client_id, None)
        if player is None:
            return
        await self._unadvertise_player(client_id)
        player.detach()

    async def _handle_websocket(self, request: web.Request) -> web.StreamResponse:
        client_id = request.match_info["client_id"]
        player = self._players.get(client_id)
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        self._connections.add(websocket)
        try:
            if player is None:
                await websocket.close(code=1008, message=b"unknown Sendspin client")
                return websocket
            try:
                await player.serve_websocket(websocket)
            except (PermissionError, RuntimeError) as exc:
                # Pairing/authentication failures must not remain as an open
                # route. The SDK normally closes these itself; this covers the
                # adapter boundary and keeps the listener's policy explicit.
                if not websocket.closed:
                    await websocket.close(code=1008, message=str(exc).encode()[:123])
            return websocket
        finally:
            self._connections.discard(websocket)

    async def _start_zeroconf(self) -> None:
        if self._zeroconf is not None:
            return
        if self._zeroconf_factory is not None:
            self._zeroconf = self._zeroconf_factory()
            return
        from zeroconf.asyncio import AsyncZeroconf

        self._zeroconf = AsyncZeroconf()

    async def _advertise_player(self, client_id: str, player: SendspinPlayer) -> None:
        if self._zeroconf is None or client_id in self._services:
            return
        from zeroconf import ServiceInfo

        factory = self._service_info_factory or ServiceInfo
        address = self.advertise_address or self.host
        service = factory(
            type_="_sendspin._tcp.local.",
            name=f"{client_id}._sendspin._tcp.local.",
            server=f"{client_id}.local.",
            parsed_addresses=[address],
            port=self.port,
            properties={"path": f"/sendspin/{client_id}", "name": player.name},
        )
        await self._zeroconf.async_register_service(service, allow_name_change=True)
        self._services[client_id] = service

    async def _unadvertise_player(self, client_id: str) -> None:
        service = self._services.pop(client_id, None)
        if service is not None and self._zeroconf is not None:
            await self._zeroconf.async_unregister_service(service)


class SendspinDeviceSession:
    """Serialize one Sendspin player's timestamped audio onto one Echo link."""

    def __init__(
        self, device_id: str, name: str, device: Any, clock_sync: Any
    ) -> None:
        import asyncio

        from em_music_sync import PcmFrame, encode_clear, encode_end, encode_pcm, encode_start

        self.device_id = device_id
        self.device = device
        self.clock_sync = clock_sync
        self._PcmFrame = PcmFrame
        self._encode_clear = encode_clear
        self._encode_end = encode_end
        self._encode_pcm = encode_pcm
        self._encode_start = encode_start
        self.player = SendspinPlayer(
            device_id,
            name,
            on_audio=self.on_audio,
            on_stream_start=self.on_stream_start,
            on_stream_clear=self.on_stream_clear,
            on_stream_end=self.on_stream_end,
            on_disconnect=self.on_disconnect,
        )
        self.client_id: str | None = None
        self._eq: Any | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self._writer_task: asyncio.Task | None = None
        self.connection_task: asyncio.Task | None = None
        self.disconnected = asyncio.Event()
        self._generation = 0
        self._sequence = 0
        self.dropped_frames = 0
        self.rejected_frames = 0
        self.closed = False

    def attach(self, client: Any) -> None:
        self.player.attach(client)

    def publish_state(self, state: PlayerState) -> None:
        import em_ha_sidechannels
        em_ha_sidechannels.sendspin_state(
            self.device_id,
            state.playback_state,
            volume=state.volume,
            muted=state.muted,
            title=state.title,
            artist=state.artist,
        )

    async def start(self) -> None:
        if self._writer_task is not None:
            raise RuntimeError("Sendspin device session is already started")
        self.closed = False
        self._writer_task = asyncio.create_task(self._writer(), name=f"sendspin-{self.device_id}")

    async def close(self) -> None:
        self.closed = True
        if self.connection_task is not None:
            self.connection_task.cancel()
            await asyncio.gather(self.connection_task, return_exceptions=True)
            self.connection_task = None
        await self.player.disconnect()
        if self._writer_task is not None:
            self._writer_task.cancel()
            await asyncio.gather(self._writer_task, return_exceptions=True)
            self._writer_task = None
        self.player.detach()

    def on_stream_start(self, _message: Any) -> None:
        # A synchronized group stream owns the music plane. Stop any legacy
        # URL feed before its first timestamped frame can reach the device.
        # The task is deliberately detached from the SDK callback so the
        # Sendspin reader is never blocked by the legacy player teardown.
        asyncio.create_task(self._stop_legacy_player())
        try:
            import em_eq
            self._eq = em_eq.StreamingEQ(
                PCM_SAMPLE_RATE,
                bands=getattr(self.device, "eq_bands", [0.0] * 8),
                loudness=bool(getattr(self.device, "eq_loudness", False)),
            )
        except Exception:
            self._eq = None
        self._generation = (self._generation + 1) & 0xFFFFFFFF
        if self._generation == 0:
            self._generation = 1
        self._sequence = 0
        self._discard_queued()
        self._enqueue(self._encode_start(self._generation))

    async def _stop_legacy_player(self) -> None:
        try:
            import em_player
            await em_player.stop(self.device_id)
        except Exception as exc:
            log.warning("[%s] legacy music stop failed: %s", self.device_id, exc)

    def on_stream_clear(self, roles: list[str] | None) -> None:
        if roles is not None and "player" not in roles:
            return
        if self._eq is not None:
            self._eq.reset()
        if self._generation:
            self._sequence = 0
            self._discard_queued()
            self._enqueue(self._encode_clear(self._generation))

    def on_stream_end(self, roles: list[str] | None) -> None:
        if roles is not None and "player" not in roles:
            return
        self._eq = None
        if self._generation:
            self._enqueue(self._encode_end(self._generation))

    def on_disconnect(self) -> None:
        self._eq = None
        self._discard_queued()
        self.disconnected.set()

    def on_audio(self, timestamp_us: int, data: bytes, _audio_format: Any) -> None:
        if not self._generation or not self.clock_sync.synchronized:
            self.rejected_frames += 1
            return
        try:
            if self._eq is not None:
                data = self._eq.process(data)
            target_us = self.clock_sync.controller_to_device(
                self.player.compute_play_time(timestamp_us)
            )
            frame = self._encode_pcm(self._PcmFrame(
                self._generation, self._sequence, target_us, data
            ))
        except (RuntimeError, ValueError):
            self.rejected_frames += 1
            return
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        self._enqueue(frame)

    def _enqueue(self, frame: bytes) -> None:
        try:
            self._queue.put_nowait(frame)
        except Exception:
            self.dropped_frames += 1

    def _discard_queued(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except Exception:
                return

    async def _writer(self) -> None:
        while True:
            frame = await self._queue.get()
            await self.device.send_data(frame)


class SendspinRuntime:
    """Own per-device Sendspin identities, sessions, and command routing."""

    def __init__(self, listener: SendspinListener | None, data_dir: str) -> None:
        from pathlib import Path

        self.listener = listener
        self.data_dir = Path(data_dir)
        self.sessions: dict[str, SendspinDeviceSession] = {}
        self.configured_url = ""
        self.resolved_url: str | None = None
        self.discovery = False
        self._discovery_task: asyncio.Task | None = None

    async def configure(self, server_url: str) -> None:
        normalized = normalize_server_url(server_url)
        if self._discovery_task is not None:
            self._discovery_task.cancel()
            await asyncio.gather(self._discovery_task, return_exceptions=True)
            self._discovery_task = None
        self.configured_url = normalized
        self.discovery = not bool(normalized)
        self.resolved_url = normalized or None
        for session in self.sessions.values():
            await self._restart_connection(session)
        if self.discovery:
            self._discovery_task = asyncio.create_task(
                self._discovery_loop(), name="sendspin-discovery"
            )

    async def _discovery_loop(self) -> None:
        while self.discovery:
            try:
                resolved = await discover_server_url()
                if resolved and resolved != self.resolved_url:
                    self.resolved_url = resolved
                    log.info("Discovered Sendspin server at %s", resolved)
                    for session in self.sessions.values():
                        await self._restart_connection(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Sendspin server discovery failed: %s", exc)
            await asyncio.sleep(10.0)

    async def register_device(self, device_id: str, name: str, device: Any) -> SendspinDeviceSession:
        if device_id in self.sessions:
            return self.sessions[device_id]
        if not getattr(device.clock_sync, "synchronized", False):
            raise RuntimeError("device clock is not synchronized")
        client_id = f"echomuse-{device_id}"
        session = SendspinDeviceSession(device_id, name, device, device.clock_sync)
        client = await create_sdk_client(
            client_id,
            name,
            buffer_capacity=2_000_000,
            initial_volume=round(float(getattr(device, "volume", 1.0)) * 100),
            initial_muted=bool(getattr(device, "muted", False)),
        )
        session.attach(client)
        session.player.set_state_listener(session.publish_state)
        await session.start()
        if self.listener is not None:
            await self.listener.register_player(client_id, session.player)
        session.client_id = client_id
        self.sessions[device_id] = session
        self._start_connection(session)
        return session

    async def unregister_device(self, device_id: str) -> None:
        session = self.sessions.pop(device_id, None)
        if session is None:
            return
        client_id = session.client_id
        await session.close()
        if client_id and self.listener is not None:
            await self.listener.unregister_player(client_id)

    def _start_connection(self, session: SendspinDeviceSession) -> None:
        if self.resolved_url is None or session.closed:
            return
        session.connection_task = asyncio.create_task(
            self._connection_loop(session, self.resolved_url),
            name=f"sendspin-connect-{session.device_id}",
        )

    async def _restart_connection(self, session: SendspinDeviceSession) -> None:
        if session.connection_task is not None:
            session.connection_task.cancel()
            await asyncio.gather(session.connection_task, return_exceptions=True)
            session.connection_task = None
        await session.player.disconnect()
        session.disconnected.clear()
        self._start_connection(session)

    async def _connection_loop(self, session: SendspinDeviceSession, url: str) -> None:
        delay = 1.0
        while not session.closed and self.sessions.get(session.device_id) is session:
            session.disconnected.clear()
            state = session.player.state
            state.error = None
            session.publish_state(state)
            try:
                await session.player.connect(url)
                state.error = None
                session.publish_state(state)
                log.info("[%s] connected to Sendspin server %s", session.device_id, url)
                delay = 1.0
                await session.disconnected.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.connected = False
                state.synchronized = False
                state.available = False
                state.error = str(exc)
                session.publish_state(state)
                log.warning("[%s] Sendspin connection failed: %s", session.device_id, exc)
            if not session.closed:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    @staticmethod
    def _session_status(session: SendspinDeviceSession) -> dict[str, Any]:
        state = session.player.state
        result = {
            "device_id": session.device_id,
            "name": session.player.name,
            "client_id": session.client_id,
            "connected": state.connected,
            "synchronized": state.synchronized,
            "available": state.available,
            "server_id": state.server_id,
            "error": state.error,
        }
        return result

    def status(self) -> dict[str, Any]:
        return {
            "configured_url": self.configured_url,
            "resolved_url": self.resolved_url,
            "discovery": self.discovery,
            "devices": [
                self._session_status(session)
                for session in self.sessions.values()
            ],
        }

    async def command(self, device_id: str, command: str, **kwargs: Any) -> None:
        session = self.sessions.get(device_id)
        if session is None:
            raise RuntimeError("Sendspin player is unavailable")
        await session.player.send_group_command(command, **kwargs)
        if command == "volume" and "volume" in kwargs:
            session.player.state.volume = int(kwargs["volume"])
        elif command == "mute" and "mute" in kwargs:
            session.player.state.muted = bool(kwargs["mute"])
        session.publish_state(session.player.state)

    async def close(self) -> None:
        self.discovery = False
        if self._discovery_task is not None:
            self._discovery_task.cancel()
            await asyncio.gather(self._discovery_task, return_exceptions=True)
            self._discovery_task = None
        for device_id in list(self.sessions):
            await self.unregister_device(device_id)
