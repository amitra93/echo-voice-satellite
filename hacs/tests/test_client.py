"""ControllerClient against a fake aiohttp server — no Home Assistant needed.

`client.py` deliberately imports nothing from `homeassistant`, so these tests
run in any environment with `aiohttp` installed (see
`custom_components/echo_voice_satellite/client.py`'s docstring).
"""

import asyncio
import json

import aiohttp
import pytest
from aiohttp import test_utils, web

from custom_components.echo_voice_satellite import audio_frame
from custom_components.echo_voice_satellite.client import (
    AudioChannel,
    ControllerClient,
    ControllerError,
)

API_KEY = "em_test_key"


def _require_key(request: web.Request) -> None:
    if request.headers.get("Authorization") != f"Bearer {API_KEY}":
        raise web.HTTPUnauthorized()


async def _get_devices(request):
    _require_key(request)
    return web.json_response([{"device_id": "ABC123", "connected": True}])


async def _create_turn(request):
    _require_key(request)
    return web.json_response({"turn_id": 7, "kind": "announcement"}, status=201)


async def _turn_action(request):
    _require_key(request)
    return web.json_response({"turn_id": int(request.match_info["tid"]), "action": "endpoint"})


async def _turn_action_error(request):
    _require_key(request)
    return web.json_response({"error": "turn_not_found"}, status=404)


async def _events_ws(request):
    _require_key(request)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_str(json.dumps({"type": "snapshot", "devices": [{"device_id": "ABC123"}]}))
    await ws.send_str(json.dumps({"type": "button.event", "device_id": "ABC123", "gesture": "long"}))
    async for _ in ws:
        pass
    return ws


async def _audio_ws(request):
    _require_key(request)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_bytes(audio_frame.encode_frame(audio_frame.MIC_PCM, 0, b"\x00" * audio_frame.MIC_FRAME_BYTES))
    await ws.send_bytes(audio_frame.encode_frame(audio_frame.MIC_EOS, 1))
    async for _ in ws:
        pass
    return ws


async def _audio_ws_echo(request):
    """An audio WS that records every frame HA sends it (TTS_PCM/TTS_EOS)."""
    _require_key(request)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    received = request.app["received_tts_frames"]
    async for message in ws:
        if message.type == aiohttp.WSMsgType.BINARY:
            received.append(audio_frame.decode_frame(message.data))
    return ws


async def _media_command(request):
    _require_key(request)
    body = await request.json()
    return web.json_response({"device_id": request.match_info["id"], **body})


async def _timer_event(request):
    _require_key(request)
    return web.json_response(await request.json())


async def _server_error(request):
    _require_key(request)
    return web.Response(status=500, text="boom")


async def _closes_immediately(request):
    _require_key(request)
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.close()
    return ws


def _make_app():
    app = web.Application()
    app["received_tts_frames"] = []
    app.router.add_get("/api/devices", _get_devices)
    app.router.add_post("/api/devices/{id}/turn", _create_turn)
    app.router.add_post("/api/devices/{id}/media", _media_command)
    app.router.add_post("/api/devices/{id}/timer-events", _timer_event)
    app.router.add_post("/api/turns/{tid}/endpoint", _turn_action)
    app.router.add_post("/api/turns/{tid}/reject", _turn_action_error)
    app.router.add_post("/api/turns/{tid}/error500", _server_error)
    app.router.add_get("/api/events", _events_ws)
    app.router.add_get("/api/events-drop", _closes_immediately)
    app.router.add_get("/api/v1/ws/ha/audio/{turn_id}", _audio_ws)
    app.router.add_get("/api/v1/ws/ha/audio-echo/{turn_id}", _audio_ws_echo)
    return app


async def _run(coro, *, with_app=False):
    app = _make_app()
    server = test_utils.TestServer(app)
    await server.start_server()
    try:
        base_url = f"http://{server.host}:{server.port}"
        return await (coro(base_url, app) if with_app else coro(base_url))
    finally:
        await server.close()


def test_get_devices_sends_bearer_key_and_parses_json():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        try:
            devices = await client.async_get_devices()
        finally:
            await client.async_close()
        return devices

    devices = asyncio.run(_run(body))
    assert devices == [{"device_id": "ABC123", "connected": True}]


def test_wrong_api_key_is_reported_as_controller_error():
    async def body(base_url):
        client = ControllerClient(base_url, "wrong-key")
        try:
            with pytest.raises(ControllerError):
                await client.async_get_devices()
        finally:
            await client.async_close()

    asyncio.run(_run(body))


def test_create_turn_rejects_non_announcement_kind_before_any_request():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        try:
            with pytest.raises(ValueError):
                await client.async_create_turn("ABC123", "conversation")
        finally:
            await client.async_close()

    asyncio.run(_run(body))


def test_turn_action_error_reply_raises_controller_error_with_code():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        try:
            with pytest.raises(ControllerError) as excinfo:
                await client.async_turn_action(1, "reject")
            assert excinfo.value.code == "turn_not_found"
        finally:
            await client.async_close()

    asyncio.run(_run(body))


def test_events_connect_delivers_snapshot_then_dispatches_events():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        received = []
        async def handler(event):
            received.append(event)
        client.set_event_handler(handler)
        try:
            await client.async_connect()
            await asyncio.wait_for(client.snapshot_ready.wait(), timeout=5)
            for _ in range(50):
                if len(received) >= 2:
                    break
                await asyncio.sleep(0.05)
        finally:
            await client.async_close()
        return received

    received = asyncio.run(_run(body))
    assert received[0]["type"] == "snapshot"
    assert received[1] == {"type": "button.event", "device_id": "ABC123", "gesture": "long"}


def test_audio_channel_mic_frames_stops_at_eos():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        try:
            channel = await client.async_attach_audio(1)
            frames = [frame async for frame in channel.mic_frames()]
            await channel.close()
        finally:
            await client.async_close()
        return frames

    frames = asyncio.run(_run(body))
    assert frames == [b"\x00" * audio_frame.MIC_FRAME_BYTES]


def test_reconnect_delay_is_bounded_and_grows_with_attempts():
    assert ControllerClient.reconnect_delay(0) <= 1.0
    assert ControllerClient.reconnect_delay(20) <= 60.0


def test_media_command_posts_json_body_and_returns_reply():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        try:
            reply = await client.async_media_command("ABC123", {"volume": 0.42})
        finally:
            await client.async_close()
        return reply

    reply = asyncio.run(_run(body))
    assert reply == {"device_id": "ABC123", "volume": 0.42}


def test_timer_event_posts_lifecycle_payload_and_returns_reply():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        event = {
            "event": "finished", "timer_id": "01J", "ha_device_id": "ha-dev",
            "name": "pizza", "total_seconds": 600, "seconds_left": 0,
            "is_active": False,
        }
        try:
            reply = await client.async_timer_event("ABC123", event)
        finally:
            await client.async_close()
        return reply

    assert asyncio.run(_run(body)) == {
        "event": "finished", "timer_id": "01J", "ha_device_id": "ha-dev",
        "name": "pizza", "total_seconds": 600, "seconds_left": 0,
        "is_active": False,
    }


def test_post_error_without_json_reason_still_raises_controller_error():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        try:
            with pytest.raises(ControllerError):
                await client.async_turn_action(1, "error500")
        finally:
            await client.async_close()

    asyncio.run(_run(body))


def test_disconnect_handler_fires_when_events_socket_is_closed_server_side():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        disconnected = asyncio.Event()

        async def on_disconnect():
            disconnected.set()

        client.set_disconnect_handler(on_disconnect)
        try:
            await client.async_connect()
            assert client.connected is True
            # Hit a route that upgrades then closes immediately, forcing a
            # fresh connect against the "drop" endpoint to prove the reader
            # exits and fires the handler rather than hanging.
            await client._events_ws.close()
            await asyncio.wait_for(disconnected.wait(), timeout=5)
        finally:
            await client.async_close()
        return client.connected

    connected_after = asyncio.run(_run(body))
    assert connected_after is False


def test_disconnect_handler_does_not_fire_on_deliberate_close():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        calls = []

        async def on_disconnect():
            calls.append(1)

        client.set_disconnect_handler(on_disconnect)
        await client.async_connect()
        await client.async_close()
        await asyncio.sleep(0.05)
        return calls

    assert asyncio.run(_run(body)) == []


def test_external_session_is_not_closed_by_async_close():
    async def body(base_url):
        session = aiohttp.ClientSession()
        client = ControllerClient(base_url, API_KEY, session=session)
        try:
            await client.async_get_devices()
        finally:
            await client.async_close()
        return session.closed

    assert asyncio.run(_run(body)) is False
    # Caller owns cleanup for a session it provided.


def test_audio_channel_send_tts_encodes_frames_and_guards_after_eos():
    async def body(base_url, app):
        # async_attach_audio always dials the mic-only /ws/ha/audio path
        # (that's the real endpoint), so this test builds an AudioChannel
        # directly over the test server's echo endpoint to inspect what
        # send_tts/send_tts_eos actually put on the wire.
        client = ControllerClient(base_url, API_KEY)
        try:
            session = await client._ensure_session()
            ws = await session.ws_connect(
                f"{base_url}/api/v1/ws/ha/audio-echo/1",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            echo_channel = AudioChannel(ws)
            await echo_channel.send_tts(b"\x01\x02\x03\x04")
            await echo_channel.send_tts_eos()
            with pytest.raises(audio_frame.AudioProtocolError):
                await echo_channel.send_tts(b"\x00\x00")
            await echo_channel.close()
            await asyncio.sleep(0.05)
        finally:
            await client.async_close()
        return app["received_tts_frames"]

    frames = asyncio.run(_run(body, with_app=True))
    assert [f.frame_type for f in frames] == [audio_frame.TTS_PCM, audio_frame.TTS_EOS]
    assert frames[0].sequence == 0 and frames[0].payload == b"\x01\x02\x03\x04"
    assert frames[1].sequence == 1


def test_send_tts_eos_is_idempotent():
    async def body(base_url):
        client = ControllerClient(base_url, API_KEY)
        try:
            session = await client._ensure_session()
            ws = await session.ws_connect(
                f"{base_url}/api/v1/ws/ha/audio-echo/1",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            channel = AudioChannel(ws)
            await channel.send_tts_eos()
            await channel.send_tts_eos()  # no-op, must not raise
            await channel.close()
        finally:
            await client.async_close()

    asyncio.run(_run(body))
