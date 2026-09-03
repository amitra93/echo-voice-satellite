"""Tests for assist_satellite.py STT partial wiring (Phase 5)."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import types

import pytest

from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.components.assist_satellite import AssistSatelliteEntity

module = importlib.import_module("custom_components.echo_voice_satellite.assist_satellite")
stt = importlib.import_module("custom_components.echo_voice_satellite.stt")
from custom_components.echo_voice_satellite.client import ControllerError


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def async_turn_action(self, tid, action, data=None):
        self.calls.append((tid, action, data))
        return {}

    async def async_attach_audio(self, tid):
        return _FakeChannel()

    async def async_create_turn(self, *a, **kw):
        return {"turn_id": 1}


class _FakeHass:
    def __init__(self):
        self.tasks = []

    def async_create_task(self, coro, name=None):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


class _FakeChannel:
    def mic_frames(self):
        async def gen():
            for _ in range(2):
                yield b"\x00" * 2560
        return gen()

    async def close(self):
        pass


class _FakeCoordinator:
    def __init__(self, client):
        self.client = client
        self.data = {"devices": [{"device_id": "A", "muted": False}]}
        self.last_update_success = True
        self.control_available = True

    def async_add_event_listener(self, cb):
        return lambda: None

    def async_add_listener(self, cb):
        return lambda: None


def _make_sat(client=None):
    client = client or _FakeClient()
    coord = _FakeCoordinator(client)
    sat = object.__new__(module.EchoAssistSatellite)
    sat.coordinator = coord
    sat.client = client
    sat.device_id = "A"
    sat.hass = _FakeHass()
    sat._active_turn_id = None
    sat._active_channel = None
    sat._active_turn_token = object()
    sat._tts_task = None
    sat._tts_turn_token = None
    sat._pipeline_task = None
    sat._offer_lock = asyncio.Lock()
    sat._transcript_sent = False
    sat._endpoint_sent = False
    sat._continue_conversation = False
    sat._attr_unique_id = "A_assist_satellite"
    sat.async_write_ha_state = lambda: None
    return sat, client


# ---------------------------------------------------------------------------
# _on_stt_partial — direct entry (checks current active turn)
# ---------------------------------------------------------------------------

def test_on_stt_partial_sends_is_final_false_when_owning():
    async def run():
        sat, client = _make_sat()
        sat._active_turn_id = 7
        chan = _FakeChannel()
        sat._active_channel = chan
        sat._on_stt_partial("hello")
        await asyncio.sleep(0.05)
        assert client.calls == [(7, "transcript", {"text": "hello", "is_final": False})]

    asyncio.run(run())


def test_on_stt_partial_is_noop_when_no_active_turn():
    sat, client = _make_sat()
    sat._active_turn_id = None
    sat._active_channel = None
    sat._on_stt_partial("hello")
    asyncio.run(asyncio.sleep(0.05))
    assert client.calls == []


def test_on_stt_partial_is_noop_when_token_mismatch_via_clear():
    # Simulate cancel clearing active turn
    sat, client = _make_sat()
    sat._active_turn_id = 1
    sat._active_channel = _FakeChannel()
    # Clear as _async_gateway_event does on cancel
    sat._active_turn_id = None
    sat._active_turn_token = None
    sat._active_channel = None
    sat._on_stt_partial("stale")
    asyncio.run(asyncio.sleep(0.05))
    assert client.calls == []


# ---------------------------------------------------------------------------
# _bound_partial_callback — pinned to one turn
# ---------------------------------------------------------------------------

def test_bound_partial_drops_stale_after_barge_even_when_new_turn_active():
    async def run():
        sat, client = _make_sat()
        chan1 = _FakeChannel()
        token1 = object()
        sat._active_turn_id = 1
        sat._active_turn_token = token1
        sat._active_channel = chan1
        bound = sat._bound_partial_callback(1, token1, chan1)
        bound("first")
        await asyncio.sleep(0.05)
        assert client.calls[-1][2]["text"] == "first"
        # Barge-in: new turn replaces old
        chan2 = _FakeChannel()
        token2 = object()
        sat._active_turn_id = 2
        sat._active_turn_token = token2
        sat._active_channel = chan2
        client.calls.clear()
        bound("stale from old stream")
        await asyncio.sleep(0.05)
        assert client.calls == [], "stale partial must not be attributed to new turn"
        # New turn's own bound callback should still work
        bound2 = sat._bound_partial_callback(2, token2, chan2)
        bound2("new turn hello")
        await asyncio.sleep(0.05)
        assert client.calls == [(2, "transcript", {"text": "new turn hello", "is_final": False})]

    asyncio.run(run())


def test_bound_partial_is_noop_when_channel_mismatch():
    sat, client = _make_sat()
    chan = _FakeChannel()
    token = object()
    sat._active_turn_id = 5
    sat._active_turn_token = token
    sat._active_channel = chan
    bound = sat._bound_partial_callback(5, token, chan)
    # Change channel (simulating close/replace)
    sat._active_channel = _FakeChannel()
    bound("should drop")
    asyncio.run(asyncio.sleep(0.05))
    assert client.calls == []


# ---------------------------------------------------------------------------
# _run_wake_pipeline — wraps with CorrelatedMicStream
# ---------------------------------------------------------------------------

def test_run_wake_pipeline_wraps_mic_frames_with_correlated_stream(monkeypatch):
    sat, client = _make_sat()
    chan = _FakeChannel()
    sat._active_turn_id = 9
    sat._active_channel = chan
    token = sat._active_turn_token
    captured = {}

    async def fake_accept(self, mic_frames, end_stage=None):
        captured["mic_frames"] = mic_frames
        captured["end_stage"] = end_stage
        # Verify it's a CorrelatedMicStream
        assert isinstance(mic_frames, stt.CorrelatedMicStream)
        # Verify iteration still yields original chunks
        chunks = []
        async for c in mic_frames:
            chunks.append(c)
        assert len(chunks) == 2
        return None

    monkeypatch.setattr(AssistSatelliteEntity, "async_accept_pipeline_from_satellite", fake_accept)
    asyncio.run(sat._run_wake_pipeline(chan, turn_id=9, token=token, timer_speech=False))
    assert captured["end_stage"] is None
    assert isinstance(captured["mic_frames"], stt.CorrelatedMicStream)


def test_run_wake_pipeline_timer_speech_still_wraps_and_sets_end_stage(monkeypatch):
    sat, client = _make_sat()
    chan = _FakeChannel()
    sat._active_turn_id = 10
    sat._active_channel = chan
    token = sat._active_turn_token
    captured = {}

    async def fake_accept(self, mic_frames, end_stage=None):
        captured["mic_frames"] = mic_frames
        captured["end_stage"] = end_stage
        assert isinstance(mic_frames, stt.CorrelatedMicStream)
        return None

    monkeypatch.setattr(AssistSatelliteEntity, "async_accept_pipeline_from_satellite", fake_accept)
    asyncio.run(sat._run_wake_pipeline(chan, turn_id=10, token=token, timer_speech=True))
    # timer_speech must still wrap, and must pass end_stage=STT
    assert isinstance(captured["mic_frames"], stt.CorrelatedMicStream)
    # end_stage should be PipelineStage.STT string "stt" or enum
    assert captured["end_stage"] is not None
    # Accept either string or enum with .value == "stt"
    val = captured["end_stage"]
    assert str(val).lower().endswith("stt") or getattr(val, "value", val) == "stt"


def test_run_wake_pipeline_e2e_partial_via_stt(monkeypatch):
    # Full round-trip: pipeline receives CorrelatedMicStream, STT yields interims,
    # satellite forwards is_final:false
    async def run():
        sat, client = _make_sat()
        chan = _FakeChannel()
        sat._active_turn_id = 42
        sat._active_channel = chan
        token = sat._active_turn_token
        sat._pipeline_task = asyncio.current_task()

        from custom_components.echo_voice_satellite.const import (
            CONF_GEMINI_API_KEY,
            CONF_CUSTOM_VOCABULARY,
            CONF_LANGUAGE_CODES,
            CONF_TRANSCRIPTION_MODE,
        )

        entry = types.SimpleNamespace(
            entry_id="e",
            options={
                CONF_GEMINI_API_KEY: "valid",
                CONF_TRANSCRIPTION_MODE: "VERBATIM",
                CONF_CUSTOM_VOCABULARY: [],
                CONF_LANGUAGE_CODES: "",
            },
        )
        stt_ent = stt.GeminiTranscribeEntity(entry)

        async def fake_pipeline(self, mic_frames, end_stage=None):
            assert isinstance(mic_frames, stt.CorrelatedMicStream)
            meta = types.SimpleNamespace(language="en-US")
            result = await stt_ent.async_process_audio_stream(meta, mic_frames)
            assert result.text == "hello world"
            return result

        monkeypatch.setattr(AssistSatelliteEntity, "async_accept_pipeline_from_satellite", fake_pipeline)
        await sat._run_wake_pipeline(chan, turn_id=42, token=token, timer_speech=False)
        # Let the bound partial tasks run
        await asyncio.sleep(0.1)
        partials = [c for c in client.calls if c[1] == "transcript"]
        assert len(partials) == 2
        assert partials[0][2] == {"text": "hello", "is_final": False}
        assert partials[1][2] == {"text": "hello world", "is_final": False}
        # Endpoint and tts/end still sent by _run_wake_pipeline finally
        assert any(c[1] == "endpoint" for c in client.calls)
        assert any(c[1] == "tts/end" for c in client.calls)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Privacy — no transcript logging in assist_satellite new code
# ---------------------------------------------------------------------------

def test_assist_satellite_new_code_never_logs_transcript():
    src = inspect.getsource(module.EchoAssistSatellite._on_stt_partial)
    src += inspect.getsource(module.EchoAssistSatellite._bound_partial_callback)
    src += inspect.getsource(module.EchoAssistSatellite._run_wake_pipeline)
    # No log call should interpolate text
    lowered = src.lower()
    # The file logs other things (pipeline failed) but never transcript text
    assert "transcript" not in lowered or "text" not in lowered.split("log.")[1] if "log." in lowered else True
    # Explicit: ensure no log.* with %r and text
    for line in src.splitlines():
        if "log." in line.lower() and "text" in line.lower():
            assert False, f"transcript logging leaked in assist_satellite new code: {line!r}"
