"""Trusted-LAN client for the EchoMuse controller's HA-facing surface.

Deliberately NOT a custom control-WebSocket protocol (docs/design/full-duplex-plan.md
"No standalone gateway / no custom control-WS protocol"): the controller's
existing `em_api.py` already has authenticated REST plus a broadcast
`/api/events` WebSocket, so this module is a thin typed wrapper around that —
ordinary request/response for turn lifecycle, and the existing event channel
for push notifications. The only bespoke wire format is the per-turn
bidirectional audio WebSocket (`audio_frame.py`).

Auth is the controller's long-lived integration API key
(`em_auth.generate_api_key` / `require_integration_or_admin`), sent as
`Authorization: Bearer <api_key>` on every request — including the
WebSocket upgrades, since this is a Python client and can set headers
(unlike a browser, which is why the dashboard falls back to a query param).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiohttp

from . import audio_frame

log = logging.getLogger(__name__)


class ControllerError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class ControllerClient:
    """One HA-owned `/api/events` connection plus REST calls to the controller."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._session = session
        self._owned_session = False
        self._events_ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task | None = None
        self._closing = False
        self.connected = False
        self.snapshot_ready = asyncio.Event()
        self.snapshot: dict[str, Any] | None = None
        self._event_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._disconnect_handler: Callable[[], Awaitable[None]] | None = None

    def set_event_handler(self, callback: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._event_handler = callback

    def set_disconnect_handler(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._disconnect_handler = callback

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owned_session = True
        return self._session

    # ── REST ────────────────────────────────────────────────────────────────

    async def async_get_devices(self) -> list[dict[str, Any]]:
        session = await self._ensure_session()
        try:
            async with session.get(
                f"{self.base_url}/api/devices", headers=self._headers()
            ) as response:
                if response.status != 200:
                    raise ControllerError(
                        "controller_unreachable", f"GET /api/devices HTTP {response.status}"
                    )
                return await response.json()
        except aiohttp.ClientError as exc:
            raise ControllerError("controller_unreachable", str(exc)) from exc

    async def async_create_turn(self, device_id: str, kind: str) -> dict[str, Any]:
        if kind != "announcement":
            raise ValueError("only announcement turns may be HA-initiated")
        return await self._post(f"/api/devices/{device_id}/turn", {"kind": kind})

    async def async_turn_action(
        self, turn_id: int, action: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._post(f"/api/turns/{turn_id}/{action}", data or {})

    async def async_media_command(self, device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/devices/{id}/media — play/pause/resume/stop/volume."""
        return await self._post(f"/api/devices/{device_id}/media", body)

    async def async_timer_event(
        self, device_id: str, event: dict[str, Any]
    ) -> dict[str, Any]:
        """Forward one Home Assistant timer lifecycle event."""
        return await self._post(f"/api/devices/{device_id}/timer-events", event)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        session = await self._ensure_session()
        try:
            async with session.post(
                f"{self.base_url}{path}", json=body, headers=self._headers()
            ) as response:
                payload = await response.json()
                if response.status >= 400:
                    raise ControllerError(
                        payload.get("error", "controller_error"),
                        payload.get("message", f"HTTP {response.status}"),
                    )
                return payload
        except aiohttp.ClientError as exc:
            raise ControllerError("controller_unreachable", str(exc)) from exc

    # ── Live events ─────────────────────────────────────────────────────────

    async def async_connect(self) -> None:
        session = await self._ensure_session()
        self.snapshot_ready.clear()
        self._events_ws = await session.ws_connect(
            f"{self.base_url}/api/events", headers=self._headers(), heartbeat=30
        )
        self.connected = True
        self._reader_task = asyncio.create_task(
            self._reader(), name="echo-voice-satellite-events-reader"
        )

    async def _reader(self) -> None:
        ws = self._events_ws
        if ws is None:
            return
        try:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self._dispatch(json.loads(message.data))
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except (asyncio.CancelledError, json.JSONDecodeError):
            raise
        except Exception:
            log.exception("echo_voice_satellite: /api/events reader failed")
        finally:
            self.connected = False
            if not self._closing and self._disconnect_handler is not None:
                await self._disconnect_handler()

    async def _dispatch(self, payload: dict[str, Any]) -> None:
        if payload.get("type") == "snapshot":
            self.snapshot = payload
            self.snapshot_ready.set()
        if self._event_handler is not None:
            await self._event_handler(payload)

    # ── Per-turn audio channel ─────────────────────────────────────────────

    async def async_attach_audio(self, turn_id: int) -> "AudioChannel":
        session = await self._ensure_session()
        try:
            websocket = await session.ws_connect(
                f"{self.base_url}/api/v1/ws/ha/audio/{turn_id}",
                headers=self._headers(),
                max_msg_size=audio_frame.TTS_FRAME_BYTES + 64,
            )
        except aiohttp.ClientError as exc:
            raise ControllerError("audio_unavailable", str(exc)) from exc
        return AudioChannel(websocket)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def async_close(self) -> None:
        self._closing = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._events_ws is not None:
            await self._events_ws.close()
            self._events_ws = None
        self.connected = False
        self.snapshot_ready.clear()
        if self._owned_session and self._session is not None:
            await self._session.close()
            self._session = None
        self._closing = False

    @staticmethod
    def reconnect_delay(attempt: int) -> float:
        return min(60.0, 2 ** min(attempt, 6)) * random.random()


class AudioChannel:
    """One transient audio WebSocket for one turn — mic up, TTS PCM down."""

    def __init__(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        self.websocket = websocket
        self._tts_sequence = 0
        self._sent_eos = False

    async def mic_frames(self) -> AsyncIterator[bytes]:
        """Yield controller MIC_PCM payloads until the explicit MIC_EOS."""
        while True:
            message = await self.websocket.receive()
            if message.type != aiohttp.WSMsgType.BINARY:
                return
            frame = audio_frame.decode_frame(message.data)
            if frame.frame_type == audio_frame.MIC_EOS:
                return
            if frame.frame_type != audio_frame.MIC_PCM:
                raise audio_frame.AudioProtocolError("unexpected frame on mic-up audio channel")
            yield frame.payload

    async def send_tts(self, payload: bytes) -> None:
        if self._sent_eos:
            raise audio_frame.AudioProtocolError("audio_sequence_error")
        await self.websocket.send_bytes(
            audio_frame.encode_frame(audio_frame.TTS_PCM, self._tts_sequence, payload)
        )
        self._tts_sequence += 1

    async def send_tts_eos(self) -> None:
        if self._sent_eos:
            return
        await self.websocket.send_bytes(
            audio_frame.encode_frame(audio_frame.TTS_EOS, self._tts_sequence)
        )
        self._tts_sequence += 1
        self._sent_eos = True

    async def close(self) -> None:
        await self.websocket.close()
