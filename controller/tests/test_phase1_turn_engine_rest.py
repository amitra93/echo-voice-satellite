"""REST/WS handler coverage for em_turn_engine.py — the gap left by
test_phase1_turn_engine.py's lower-level Turn/queue tests: create_turn,
audio_socket, _run_turn, _finish_created_turn, trigger_voice_turn,
cancel_voice_turn/abort_ha_run.
"""

import asyncio
import collections
import sys
import types

import pytest
from aiohttp import WSMsgType, test_utils, web

import em_db
import em_turn_engine as engine
from em_audio_frame import (
    MIC_EOS,
    MIC_FRAME_BYTES,
    MIC_PCM,
    TTS_EOS,
    TTS_PCM,
    decode_frame,
    encode_frame,
)


class FakeDevice:
    def __init__(self, device_id="device-1"):
        self.device_id = device_id
        self.voice_queue = asyncio.Queue()
        self.cancel_event = asyncio.Event()
        self.turn_history = collections.deque(maxlen=50)
        self.last_wake = None


@pytest.fixture()
def fresh_engine(monkeypatch):
    """A clean TurnEngine + a push_event spy, isolated per test."""
    monkeypatch.setattr(engine, "ENGINE", engine.TurnEngine())
    events = []

    async def push(event):
        events.append(event)

    monkeypatch.setattr(engine, "_push_event", push)
    return events


@pytest.fixture()
def turn_db(tmp_path):
    old_conn, old_path = em_db._conn, em_db._db_path
    em_db.init(str(tmp_path / "turn_engine_rest.db"))
    yield
    if em_db._conn is not None:
        em_db._conn.close()
    em_db._conn, em_db._db_path = old_conn, old_path


# ── create_turn ─────────────────────────────────────────────────────────────

class _Request:
    def __init__(self, match_info, body=None, path=""):
        self.match_info = match_info
        self._body = body if body is not None else {}
        self.path = path

    async def json(self):
        return self._body


def test_create_turn_rejects_when_device_is_offline(fresh_engine, monkeypatch):
    fake_api = types.SimpleNamespace(_devices={})
    monkeypatch.setitem(sys.modules, "em_api", fake_api)

    response = asyncio.run(engine.create_turn(_Request({"id": "device-1"}, {"kind": "announcement"})))
    assert response.status == 409


def test_create_turn_rejects_unsupported_kind(fresh_engine, monkeypatch, turn_db):
    device = FakeDevice()
    fake_api = types.SimpleNamespace(_devices={"device-1": device})
    monkeypatch.setitem(sys.modules, "em_api", fake_api)

    response = asyncio.run(engine.create_turn(_Request({"id": "device-1"}, {"kind": "conversation"})))
    assert response.status == 400


def test_create_turn_registers_a_pre_endpointed_turn_and_returns_201(fresh_engine, monkeypatch, turn_db):
    device = FakeDevice()
    fake_api = types.SimpleNamespace(_devices={"device-1": device})
    monkeypatch.setitem(sys.modules, "em_api", fake_api)
    fake_controller = types.SimpleNamespace(
        _run_streaming_post_turn_playback=lambda device, chunks: _consume(chunks)
    )
    monkeypatch.setitem(sys.modules, "em_controller", fake_controller)

    response = asyncio.run(engine.create_turn(_Request({"id": "device-1"}, {"kind": "announcement"})))
    assert response.status == 201

    turn_id = list(engine.ENGINE.turns)[0]
    turn = engine.ENGINE.turns[turn_id]
    assert turn.kind == "announcement"
    assert turn.endpoint.is_set()  # announcement turns skip the "user stopped speaking" wait
    row = em_db._q1("SELECT * FROM turns WHERE id = ?", (turn_id,))
    assert row["trigger_type"] == "announcement"

    # Let the background _finish_created_turn task run to completion so the
    # event loop has nothing pending when this test's loop closes.
    asyncio.run(asyncio.sleep(0.1))


async def _consume(chunks):
    async for _ in chunks:
        pass
    return 0


# ── audio_socket ─────────────────────────────────────────────────────────

def _make_audio_app():
    app = web.Application()
    app.router.add_get("/ws/{turn_id}", engine.audio_socket)
    return app


def test_audio_socket_404s_for_an_unknown_turn(fresh_engine):
    async def run():
        app = _make_audio_app()
        server = test_utils.TestServer(app)
        client = test_utils.TestClient(server)
        await client.start_server()
        try:
            resp = await client.get("/ws/999")
            assert resp.status == 404
        finally:
            await client.close()

    asyncio.run(run())


def test_audio_socket_registers_turn_and_pushes_listening_state(fresh_engine):
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        engine.ENGINE.turns[1] = turn
        app = _make_audio_app()
        server = test_utils.TestServer(app)
        client = test_utils.TestClient(server)
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws/1")
            await asyncio.sleep(0.05)
            assert turn.socket_ready.is_set()
            assert engine.ENGINE.audio_sockets[1] is turn.socket
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())
    assert fresh_engine[-1] == {
        "type": "turn.state", "device_id": "device-1", "turn_id": 1, "state": "listening",
    }


def test_audio_socket_rejects_a_second_connection_for_the_same_turn(fresh_engine):
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        engine.ENGINE.turns[1] = turn
        app = _make_audio_app()
        server = test_utils.TestServer(app)
        client = test_utils.TestClient(server)
        await client.start_server()
        try:
            first = await client.ws_connect("/ws/1")
            await asyncio.sleep(0.05)
            second = await client.ws_connect("/ws/1")
            msg = await second.receive()
            assert msg.type == WSMsgType.CLOSE
            assert msg.data == 1008
            await first.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_audio_socket_forwards_tts_pcm_and_stops_on_eos(fresh_engine):
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None, capture_audio=True)
        engine.ENGINE.turns[1] = turn
        app = _make_audio_app()
        server = test_utils.TestServer(app)
        client = test_utils.TestClient(server)
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws/1")
            await ws.send_bytes(encode_frame(TTS_PCM, 0, b"\x01\x02"))
            await ws.send_bytes(encode_frame(TTS_EOS, 1))
            await asyncio.sleep(0.1)
        finally:
            await client.close()
        return turn

    turn = asyncio.run(run())
    assert asyncio.run(asyncio.wait_for(turn.tts_queue.get(), 1.0)) == b"\x01\x02"
    assert asyncio.run(asyncio.wait_for(turn.tts_queue.get(), 1.0)) is None
    assert turn.tts_end.is_set()
    assert bytes(turn.tts_audio) == b"\x01\x02"
    assert turn.tts_ended_mono is not None


def test_audio_socket_rejects_mic_frames_sent_by_ha(fresh_engine):
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        engine.ENGINE.turns[1] = turn
        app = _make_audio_app()
        server = test_utils.TestServer(app)
        client = test_utils.TestClient(server)
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws/1")
            await ws.send_bytes(encode_frame(MIC_PCM, 0, b"\x00" * MIC_FRAME_BYTES))
            msg = await ws.receive()
            assert msg.type == WSMsgType.CLOSE
            assert msg.data == 1002
        finally:
            await client.close()

    asyncio.run(run())


# ── _run_turn / _finish_created_turn ────────────────────────────────────

def test_run_turn_returns_false_and_skips_playback_when_cancelled_before_endpoint(fresh_engine):
    async def run():
        device = FakeDevice()
        played = []

        async def post_turn_play(chunks):
            played.append(True)

        turn = engine.Turn(1, device, None, post_turn_play)
        turn.socket_ready.set()
        turn.cancelled.set()
        turn.endpoint.set()  # cancelled turns still release the endpoint waiter
        result = await engine._run_turn(turn)
        return result, played

    result, played = asyncio.run(run())
    assert result is False
    assert played == []


def test_run_turn_pushes_responding_state_and_calls_post_turn_play(fresh_engine):
    async def run():
        device = FakeDevice()
        played = []

        async def post_turn_play(chunks):
            played.append([c async for c in chunks])

        turn = engine.Turn(1, device, None, post_turn_play)
        turn.socket = object()
        turn.socket_ready.set()
        await turn.tts_queue.put(b"\x01\x00\x02\x00")  # valid S16 PCM: 2 samples
        await turn.tts_queue.put(None)
        turn.endpoint.set()

        result = await engine._run_turn(turn)
        return result, played

    result, played = asyncio.run(run())
    assert result is True
    assert len(played) == 1
    # _tts_chunks may emit the interpolator's final held sample separately;
    # concatenation remains exactly the same as one-shot 24kHz->48kHz.
    assert b"".join(played[0]) == engine._upsample_24_to_48(b"\x01\x00\x02\x00")
    assert fresh_engine[-1]["state"] == "responding"


def test_run_turn_waits_on_tts_end_when_socket_never_attached(fresh_engine):
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        turn.socket_ready.set()
        turn.endpoint.set()
        turn.tts_end.set()  # socket is None -> _run_turn awaits this instead
        result = await engine._run_turn(turn)
        return result

    assert asyncio.run(run()) is True


def test_run_turn_times_out_rather_than_hanging_forever_when_endpoint_never_arrives(
    fresh_engine, monkeypatch,
):
    """
    Regression test for a real production hang (2026-08-19): a marginal wake
    (score 0.403 against a 0.400 threshold — effectively noise) started a
    turn where neither the device's own VAD sentinel nor HA's POST
    .../endpoint ever arrived. `await turn.endpoint.wait()` had no timeout,
    so it hung for 3+ hours — holding device.voice_lock (wake detection
    pauses for a locked turn) the entire time, with no recovery short of a
    controller restart. `ENDPOINT_WAIT_TIMEOUT_S` bounds that same wait.
    """
    monkeypatch.setattr(engine, "ENDPOINT_WAIT_TIMEOUT_S", 0.05)

    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        turn.socket_ready.set()
        # turn.endpoint is deliberately never set — nobody (device or HA)
        # ever signals the turn's speech has ended.
        await engine._run_turn(turn)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())


def test_finish_created_turn_persists_outcome_and_pushes_terminal(fresh_engine, turn_db):
    async def run():
        device = FakeDevice()
        turn_id = em_db.create_turn(device.device_id, "announcement")
        turn = engine.Turn(turn_id, device, None, None)
        turn.socket_ready.set()
        turn.endpoint.set()
        turn.tts_end.set()
        engine.ENGINE.turns[turn_id] = turn
        engine.ENGINE.audio_sockets[turn_id] = object()

        await engine._finish_created_turn(turn)
        return turn_id

    turn_id = asyncio.run(run())
    row = em_db._q1("SELECT * FROM turns WHERE id = ?", (turn_id,))
    assert row["outcome"] == "ok"
    assert row["state"] == "done"
    assert fresh_engine[-1] == {
        "type": "turn.terminal", "device_id": "device-1", "turn_id": turn_id, "outcome": "ok",
    }
    assert turn_id not in engine.ENGINE.turns
    assert turn_id not in engine.ENGINE.audio_sockets


def test_finish_created_turn_records_error_outcome_when_run_turn_raises(fresh_engine, turn_db):
    async def run():
        device = FakeDevice()
        turn_id = em_db.create_turn(device.device_id, "announcement")

        async def boom(chunks):
            raise RuntimeError("playback exploded")

        turn = engine.Turn(turn_id, device, None, boom)
        turn.socket = object()
        turn.socket_ready.set()
        turn.endpoint.set()
        engine.ENGINE.turns[turn_id] = turn

        await engine._finish_created_turn(turn)
        return turn_id

    turn_id = asyncio.run(run())
    row = em_db._q1("SELECT * FROM turns WHERE id = ?", (turn_id,))
    assert row["outcome"] == "error"


# ── trigger_voice_turn / cancel_voice_turn / abort_ha_run ─────────────────

def test_trigger_voice_turn_offers_the_wake_and_reports_terminal_outcome(fresh_engine, turn_db):
    async def run():
        device = FakeDevice()

        async def on_thinking():
            pass

        async def post_turn_play(chunks):
            return 0

        async def drive_turn():
            # Wait for trigger_voice_turn to register the turn, then satisfy it
            # exactly like a real audio_socket connection + endpoint would.
            for _ in range(50):
                if engine.ENGINE.turns:
                    break
                await asyncio.sleep(0.01)
            turn = next(iter(engine.ENGINE.turns.values()))
            turn.socket_ready.set()
            turn.endpoint.set()
            turn.tts_end.set()

        driver = asyncio.create_task(drive_turn())
        result = await engine.trigger_voice_turn(
            device, on_thinking, post_turn_play, trigger_label="wakeword(0.9)",
        )
        await driver
        return result

    result = asyncio.run(run())
    assert result is False  # continue_conversation defaults False
    offer = next(e for e in fresh_engine if e["type"] == "wake.offer")
    assert offer == {
        "type": "wake.offer", "device_id": "device-1", "turn_id": offer["turn_id"],
        "trigger": "wakeword(0.9)",
    }
    terminal = next(e for e in fresh_engine if e["type"] == "turn.terminal")
    assert terminal["outcome"] == "ok"


def test_trigger_voice_turn_recovers_and_reports_audio_timeout_when_endpoint_never_arrives(
    fresh_engine, turn_db, monkeypatch,
):
    """
    The end-to-end shape of the same regression: trigger_voice_turn (what
    em_controller.py's turn loop actually awaits) must itself return —
    with the existing audio_timeout outcome and full cleanup — rather than
    the caller being left hanging on a turn nobody will ever complete.
    """
    monkeypatch.setattr(engine, "ENDPOINT_WAIT_TIMEOUT_S", 0.05)

    async def run():
        device = FakeDevice()

        async def on_thinking():
            pass

        async def post_turn_play(chunks):
            return 0

        async def drive_turn():
            # Only satisfies socket_ready — never endpoint, matching the
            # "device connected the audio socket but nothing ever told the
            # turn speech had ended" failure this test pins.
            for _ in range(50):
                if engine.ENGINE.turns:
                    break
                await asyncio.sleep(0.01)
            next(iter(engine.ENGINE.turns.values())).socket_ready.set()

        driver = asyncio.create_task(drive_turn())
        result = await engine.trigger_voice_turn(
            device, on_thinking, post_turn_play, trigger_label="wakeword(0.403)",
        )
        await driver
        return result

    result = asyncio.run(run())
    assert result is False
    terminal = next(e for e in fresh_engine if e["type"] == "turn.terminal")
    assert terminal["outcome"] == "audio_timeout"
    # Cleaned up — a real caller (em_controller._run_voice_locked) must see
    # this turn as fully finished, not still tracked as in-flight.
    assert engine.ENGINE.turns == {}
    assert engine.ENGINE.audio_sockets == {}


def test_cancel_voice_turn_only_touches_turns_for_the_named_device(fresh_engine):
    device_a = FakeDevice("device-a")
    device_b = FakeDevice("device-b")
    turn_a = engine.Turn(1, device_a, None, None)
    turn_b = engine.Turn(2, device_b, None, None)
    engine.ENGINE.turns = {1: turn_a, 2: turn_b}

    engine.cancel_voice_turn("device-a")

    assert turn_a.cancelled.is_set()
    assert not turn_b.cancelled.is_set()


def test_abort_ha_run_records_barge_without_claiming_to_abort_assist(fresh_engine):
    device = FakeDevice()
    turn = engine.Turn(1, device, None, None)
    engine.ENGINE.turns = {1: turn}

    engine.abort_ha_run("device-1")

    assert not turn.cancelled.is_set()
    assert turn.end_reason == "barged"


def test_cancel_voice_turn_preserves_the_requested_cause(fresh_engine):
    device = FakeDevice()
    turn = engine.Turn(1, device, None, None)
    engine.ENGINE.turns = {1: turn}

    engine.cancel_voice_turn("device-1", reason="muted")
    engine.cancel_voice_turn("device-1", reason="cancelled")

    assert turn.cancelled.is_set()
    assert turn.end_reason == "muted"
