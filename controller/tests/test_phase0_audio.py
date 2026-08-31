import asyncio
import pytest
from aiohttp.test_utils import make_mocked_request

import em_db
import em_auth
from em_turn_engine import TurnEngine

from em_audio_frame import (
    AudioProtocolError,
    MIC_EOS,
    MIC_FRAME_BYTES,
    MIC_PCM,
    Sequence,
    TTS_PCM,
    decode_frame,
    encode_frame,
)


def test_audio_frame_round_trip_and_sequence():
    payload = b"\x01" * MIC_FRAME_BYTES
    frame = decode_frame(encode_frame(MIC_PCM, 0, payload))
    assert frame.frame_type == MIC_PCM
    assert frame.sequence == 0
    assert frame.payload == payload

    seq = Sequence()
    seq.accept(frame)
    eos = decode_frame(encode_frame(MIC_EOS, 1))
    seq.accept(eos)
    assert seq.eos


def test_audio_frame_rejects_bad_payload_and_sequence():
    with pytest.raises(AudioProtocolError):
        encode_frame(MIC_PCM, 0, b"short")
    with pytest.raises(AudioProtocolError):
        encode_frame(TTS_PCM, 0, b"\x00")

    seq = Sequence()
    seq.accept(decode_frame(encode_frame(MIC_EOS, 0)))
    with pytest.raises(AudioProtocolError):
        seq.accept(decode_frame(encode_frame(MIC_EOS, 1)))


def test_audio_frame_rejects_truncated_and_flagged_frames():
    with pytest.raises(AudioProtocolError):
        decode_frame(b"\x01\x00")
    raw = bytearray(encode_frame(MIC_EOS, 0))
    raw[1] = 1
    with pytest.raises(AudioProtocolError):
        decode_frame(bytes(raw))


def test_api_keys_are_random_prefixed_and_unique():
    first = em_auth.generate_api_key()
    second = em_auth.generate_api_key()
    assert first.startswith("em_")
    assert second.startswith("em_")
    assert first != second


def test_api_key_resolves_as_admin(monkeypatch):
    key = "em_test-key"
    monkeypatch.setattr(em_auth.db, "get_session", lambda token: None)
    monkeypatch.setattr(em_auth.db, "get_config", lambda name: key if name == "ha_api_key" else None)
    request = make_mocked_request(
        "GET", "/api/devices", headers={"Authorization": f"Bearer {key}"}
    )
    user = asyncio.run(em_auth.resolve_session(request))
    assert user["role"] == "integration"
    assert user["username"] == "home-assistant-integration"


def test_api_key_rejects_wrong_key_and_accepts_ws_query(monkeypatch):
    key = "em_test-key"
    monkeypatch.setattr(em_auth.db, "get_session", lambda token: None)
    monkeypatch.setattr(em_auth.db, "get_config", lambda name: key if name == "ha_api_key" else None)

    wrong = make_mocked_request("GET", "/api/devices?api_key=wrong")
    assert asyncio.run(em_auth.resolve_session(wrong)) is None

    valid = make_mocked_request("GET", f"/api/events?api_key={key}")
    user = asyncio.run(em_auth.ws_resolve_session(valid))
    assert user["role"] == "integration"


def test_turn_engine_registers_and_unregisters_socket():
    engine = TurnEngine()
    first = object()
    second = object()
    engine.register_audio_socket(7, first)
    assert engine.audio_sockets[7] is first

    # A stale connection cannot remove its replacement.
    engine.unregister_audio_socket(7, second)
    assert engine.audio_sockets[7] is first
    engine.unregister_audio_socket(7, first)
    assert 7 not in engine.audio_sockets


def test_create_turn_allocates_pending_row_and_update_completes_it(tmp_path):
    old_conn, old_path = em_db._conn, em_db._db_path
    try:
        em_db.init(str(tmp_path / "phase0.db"))
        turn_id = em_db.create_turn("device-1", "conversation")
        row = em_db._q1("SELECT * FROM turns WHERE id = ?", (turn_id,))
        assert row["state"] == "pending"
        assert row["trigger_type"] == "conversation"

        em_db.update_turn(turn_id, {"outcome": "ok", "stt_text": "hello"})
        row = em_db._q1("SELECT * FROM turns WHERE id = ?", (turn_id,))
        assert row["state"] == "done"
        assert row["outcome"] == "ok"
        assert row["stt_text"] == "hello"
    finally:
        if em_db._conn is not None:
            em_db._conn.close()
        em_db._conn, em_db._db_path = old_conn, old_path


def test_require_integration_or_admin_rejects_readonly_and_accepts_integration(monkeypatch):
    monkeypatch.setattr(em_auth.db, "get_session", lambda token: None)
    monkeypatch.setattr(em_auth.db, "get_config", lambda name: "em_key" if name == "ha_api_key" else None)

    async def fake_handler(request):
        return "ok"

    decorated = em_auth.require_integration_or_admin(fake_handler)

    integration_req = make_mocked_request(
        "POST", "/api/devices/x/turn", headers={"Authorization": "Bearer em_key"}
    )
    result = asyncio.run(decorated(integration_req))
    assert result == "ok"

    readonly_user = {"id": 2, "username": "viewer", "role": "readonly", "token": "t"}
    monkeypatch.setattr(em_auth.db, "get_session", lambda token: {"user_id": 2})
    monkeypatch.setattr(em_auth.db, "get_user_by_id", lambda uid: readonly_user)
    readonly_req = make_mocked_request(
        "POST", "/api/devices/x/turn", headers={"Authorization": "Bearer t"}
    )
    response = asyncio.run(decorated(readonly_req))
    assert response.status == 403
