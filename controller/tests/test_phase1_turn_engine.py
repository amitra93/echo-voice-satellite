import asyncio
import collections
import io
import sys
import time
import wave
import types

import numpy as np

import em_turn_engine as engine
import em_test_audio
import em_ha_sidechannels
from em_audio_frame import MIC_EOS, MIC_FRAME_BYTES, MIC_PCM, decode_frame


class FakeSocket:
    def __init__(self):
        self.frames = []

    async def send_bytes(self, payload):
        self.frames.append(decode_frame(payload))


class FakeDevice:
    device_id = "device-1"

    def __init__(self):
        self.voice_queue = asyncio.Queue()
        self.turn_history = collections.deque(maxlen=50)


def test_mic_queue_is_forwarded_as_pcm_and_eos():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        turn.socket = FakeSocket()
        await device.voice_queue.put(b"\x01" * MIC_FRAME_BYTES)
        await device.voice_queue.put("vad_end")
        await engine._send_mic(turn)
        assert [frame.frame_type for frame in turn.socket.frames] == [MIC_PCM, MIC_EOS]
        assert len(turn.socket.frames[0].payload) == MIC_FRAME_BYTES

    asyncio.run(run())


def test_mic_queue_captures_exact_asr_pcm_when_enabled():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None, capture_audio=True)
        turn.socket = FakeSocket()
        payload = b"\x34\x12" * (MIC_FRAME_BYTES // 2)
        await device.voice_queue.put(payload)
        await device.voice_queue.put("vad_end")
        await engine._send_mic(turn)
        assert bytes(turn.mic_audio) == payload

    asyncio.run(run())


def test_tts_pcm_is_upsampled_to_48khz_s16():
    # Three samples become six samples via polyphase sinc interpolation.
    payload = b"\x00\x00\xe8\x03\x10\x00"  # 0, 1000, 16
    output = engine._upsample_24_to_48(payload)
    assert len(output) == 12
    assert engine._upsample_24_to_48(b"") == b""


def test_upsample_24_to_48_preserves_energy_and_bounds():
    # 24kHz sine wave at 1kHz
    sr = 24000
    t = np.arange(2400) / sr  # 100ms
    sine_24k = (np.sin(2 * np.pi * 1000 * t) * 0.5 * 32767).astype(np.int16).tobytes()
    out_48k = engine._upsample_24_to_48(sine_24k)
    assert len(out_48k) == len(sine_24k) * 2

    # RMS energy must be preserved across upsampler within 3%
    x_in = np.frombuffer(sine_24k, dtype=np.int16).astype(np.float64)
    x_out = np.frombuffer(out_48k, dtype=np.int16).astype(np.float64)
    rms_in = np.sqrt(np.mean(x_in * x_in))
    rms_out = np.sqrt(np.mean(x_out * x_out))
    assert abs(rms_out - rms_in) / rms_in < 0.03


def test_streaming_upsampler_matches_one_shot_across_chunk_boundaries():
    samples = np.array([0, 12000, -9000, 20000, -16000, 500], dtype=np.int16)
    one_shot = engine._upsample_24_to_48(samples.tobytes())
    upsampler = engine._StreamingUpsampler()
    chunked = b"".join([
        upsampler.process(samples[:2].tobytes()),
        upsampler.process(samples[2:5].tobytes()),
        upsampler.process(samples[5:].tobytes()),
        upsampler.flush(),
    ])
    assert chunked == one_shot
    assert len(chunked) == len(samples.tobytes()) * 2


def test_turn_action_endpoint_sets_processing_signal(monkeypatch):
    async def run():
        events = []
        async def push(event):
            events.append(event)
        monkeypatch.setattr(engine, "_push_event", push)
        turn = engine.Turn(9, FakeDevice(), lambda: None, None)
        engine.ENGINE.turns[9] = turn
        try:
            class Request:
                match_info = {"tid": "9"}
                path = "/api/turns/9/endpoint"

            response = await engine.turn_action(Request())
            assert response.status == 200
            assert turn.endpoint.is_set()
            assert events[-1]["state"] == "processing"
        finally:
            engine.ENGINE.turns.pop(9, None)

    asyncio.run(run())


def test_turn_action_cancel_sets_cancelled_and_unblocks_tts(monkeypatch):
    async def run():
        turn = engine.Turn(10, FakeDevice(), None, None)
        engine.ENGINE.turns[10] = turn
        try:
            class Request:
                match_info = {"tid": "10"}
                path = "/api/turns/10/cancel"

            response = await engine.turn_action(Request())
            assert response.status == 200
            assert turn.cancelled.is_set()
            assert turn.end_reason == "cancelled"
            assert await asyncio.wait_for(turn.tts_queue.get(), 0.1) is None
        finally:
            engine.ENGINE.turns.pop(10, None)

    asyncio.run(run())


def test_turn_actions_record_transcript_and_component_latencies(monkeypatch):
    async def run():
        async def push(_event):
            pass
        monkeypatch.setattr(engine, "_push_event", push)
        turn = engine.Turn(11, FakeDevice(), None, None)
        turn.started_mono = time.monotonic() - 1.0
        engine.ENGINE.turns[11] = turn

        class Request:
            match_info = {"tid": "11"}
            path = ""
            body = {}
            async def json(self):
                return self.body

        request = Request()
        try:
            request.path = "/api/turns/11/transcript"
            request.body = {"text": "turn on the light"}
            await engine.turn_action(request)
            request.path = "/api/turns/11/endpoint"
            await engine.turn_action(request)
            request.path = "/api/turns/11/pipeline-event"
            request.body = {"event": "intent_end", "continue_conversation": False}
            await engine.turn_action(request)
            request.path = "/api/turns/11/tts/start"
            await engine.turn_action(request)
            now = time.monotonic()
            turn.started_mono = now - 4.0
            turn.transcript_mono = now - 3.0
            turn.endpoint_mono = now - 3.0
            turn.intent_mono = now - 2.0
            turn.tts_started_mono = now - 1.95
            turn.tts_ended_mono = turn.tts_started_mono + 0.25

            rec = engine._turn_record(turn, "ok")
            assert rec["stt_text"] == "turn on the light"
            assert rec["stt_latency_ms"] >= 900
            assert rec["ha_latency_ms"] >= 0
            assert rec["tts_latency_ms"] >= 1900
        finally:
            engine.ENGINE.turns.pop(11, None)

    asyncio.run(run())


def test_test_audio_decoder_normalizes_wav_to_16k_mono(monkeypatch):
    raw = b"\x00\x00" * 160
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(raw)
    class FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return raw, b""

        async def wait(self):
            return 0

    async def fake_process(*args, **kwargs):
        assert "-ar" in args and args[args.index("-ar") + 1] == "16000"
        assert "-ac" in args and args[args.index("-ac") + 1] == "1"
        return FakeProcess()

    monkeypatch.setattr(em_test_audio.asyncio, "create_subprocess_exec", fake_process)
    pcm = asyncio.run(em_test_audio.decode_test_audio(buf.getvalue()))
    assert pcm == raw


def test_test_audio_pcm_is_wrapped_as_canonical_16k_mono_wav():
    pcm = b"\x01\x02" * 160
    encoded = em_test_audio.pcm_to_wav(pcm)

    with wave.open(io.BytesIO(encoded), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16_000
        assert wav.readframes(wav.getnframes()) == pcm


def test_test_audio_decoder_rejects_ffmpeg_failure(monkeypatch):
    class FakeProcess:
        returncode = 1

        async def communicate(self, input=None):
            return b"", b"bad input"

    async def fake_process(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(em_test_audio.asyncio, "create_subprocess_exec", fake_process)
    try:
        asyncio.run(em_test_audio.decode_test_audio(b"bad"))
    except ValueError as exc:
        assert "conversion failed" in str(exc)
    else:
        raise AssertionError("invalid audio was accepted")


def test_test_audio_decoder_enforces_duration_limit(monkeypatch):
    class FakeProcess:
        returncode = 0

        async def communicate(self, input=None):
            return b"\x00" * (em_test_audio.TEST_AUDIO_MAX_SECONDS * 16_000 * 2 + 2), b""

    async def fake_process(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(em_test_audio.asyncio, "create_subprocess_exec", fake_process)
    try:
        asyncio.run(em_test_audio.decode_test_audio(b"audio"))
    except ValueError as exc:
        assert "exceeds" in str(exc)
    else:
        raise AssertionError("oversized audio was accepted")


def test_test_audio_decoder_times_out_and_kills_process(monkeypatch):
    killed = []

    class FakeProcess:
        returncode = None

        async def communicate(self, input=None):
            raise asyncio.TimeoutError

        def kill(self):
            killed.append(True)

        async def wait(self):
            self.returncode = -9

    async def fake_process(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(em_test_audio.asyncio, "create_subprocess_exec", fake_process)
    try:
        asyncio.run(em_test_audio.decode_test_audio(b"audio"))
    except ValueError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("hung audio conversion was accepted")
    assert killed == [True]


def test_start_device_test_turn_uses_device_mic_plane_and_restores_wake_mic(monkeypatch):
    async def run():
        device = FakeDevice()
        device.listening = False
        device.oww_paused = asyncio.Event()
        device.last_wake = None
        calls = []

        async def mic_stop():
            calls.append("mic_stop")

        async def send_control(message):
            calls.append(message)
            await device.voice_queue.put("vad_end")

        async def beam_unlock():
            calls.append("beam_unlock")

        async def mic_start():
            calls.append("mic_start")

        device.mic_stop = mic_stop
        device.send_control = send_control
        device.beam_unlock = beam_unlock
        device.mic_start = mic_start

        async def fake_run_voice_locked(current, trigger_label, is_wakeword):
            assert current is device
            assert trigger_label == "test_audio"
            assert is_wakeword is False
            assert device.last_wake["score"] == 0.9
            device.listening = True
            await device.voice_queue.get()

        fake_controller = types.SimpleNamespace(_run_voice_locked=fake_run_voice_locked)
        monkeypatch.setitem(sys.modules, "em_controller", fake_controller)

        task = engine.start_device_test_turn(device)
        await asyncio.wait_for(task, timeout=2.0)
        assert not device.oww_paused.is_set()
        assert device.last_wake is None
        assert calls == [
            "mic_stop", {"type": "test_audio"},
            {"type": "test_audio_cleanup"}, "beam_unlock", "mic_start"
        ]

    asyncio.run(run())


def test_device_test_turn_restores_wake_state_when_initial_mic_stop_fails(monkeypatch):
    async def run():
        device = FakeDevice()
        device.listening = False
        device.oww_paused = asyncio.Event()
        device.last_wake = None
        calls = []

        async def mic_stop():
            raise OSError("control link dropped")

        async def send_control(message):
            calls.append(message)

        async def beam_unlock():
            calls.append("beam_unlock")

        async def mic_start():
            calls.append("mic_start")

        device.mic_stop = mic_stop
        device.send_control = send_control
        device.beam_unlock = beam_unlock
        device.mic_start = mic_start
        monkeypatch.setitem(sys.modules, "em_controller", types.SimpleNamespace())

        task = engine.start_device_test_turn(device)
        try:
            await task
        except OSError:
            pass
        else:
            raise AssertionError("mic stop failure did not fail the test turn")

        assert calls == [{"type": "test_audio_cleanup"}, "beam_unlock", "mic_start"]
        assert not device.oww_paused.is_set()
        assert device.last_wake is None
        assert not engine.test_turn_active(device.device_id)

    asyncio.run(run())


def test_send_mic_sets_endpoint_on_vad_sentinel():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        turn.socket = FakeSocket()
        await device.voice_queue.put(b"\x01" * MIC_FRAME_BYTES)
        await device.voice_queue.put("vad_end")
        await engine._send_mic(turn)
        assert turn.endpoint.is_set()
        assert [f.frame_type for f in turn.socket.frames] == [MIC_PCM, MIC_EOS]

    asyncio.run(run())


def test_trigger_voice_turn_persists_cancelled_outcome_and_cleans_registry(monkeypatch):
    async def run():
        device = FakeDevice()
        device.last_wake = {"model": "hey", "score": 0.8, "threshold": 0.5, "noise_floor": 0.1}
        updates = []
        events = []
        monkeypatch.setattr(engine.db, "create_turn", lambda *args: 21)
        monkeypatch.setattr(engine.db, "update_turn", lambda *args: updates.append(args))
        monkeypatch.setattr(engine.db, "get_turn", lambda *args: None)
        monkeypatch.setattr(engine, "_push_event", lambda event: asyncio.sleep(0, result=events.append(event)))
        monkeypatch.setattr(engine, "_run_turn", lambda turn: asyncio.sleep(0, result=False))
        engine.ENGINE.turns.clear()
        result = await engine.trigger_voice_turn(
            device, None, None, trigger_label="wakeword", preroll_discard=2
        )
        assert result is False
        assert updates[0][0] == 21
        assert updates[0][1]["outcome"] == "cancelled"
        assert updates[0][1]["wake_score"] == 0.8
        assert events[-1]["outcome"] == "cancelled"
        assert 21 not in engine.ENGINE.turns

    asyncio.run(run())


def test_turn_outcome_uses_the_first_cancellation_reason():
    turn = engine.Turn(12, FakeDevice(), None, None)
    engine._end_turn(turn, "barged")
    engine._end_turn(turn, "muted")

    assert turn.cancelled.is_set()
    assert turn.end_reason == "barged"
    assert engine._outcome_for(turn, False) == "barged"


def test_trigger_voice_turn_persists_the_exact_cancellation_cause(monkeypatch):
    async def run():
        device = FakeDevice()
        device.last_wake = None
        updates = []
        monkeypatch.setattr(engine.db, "create_turn", lambda *args: 23)
        monkeypatch.setattr(engine.db, "update_turn", lambda *args: updates.append(args))
        monkeypatch.setattr(engine.db, "get_turn", lambda *args: None)
        monkeypatch.setattr(engine, "_push_event", lambda _event: asyncio.sleep(0))

        async def cancelled_by_mute(turn):
            engine._end_turn(turn, "muted")
            return False

        monkeypatch.setattr(engine, "_run_turn", cancelled_by_mute)
        await engine.trigger_voice_turn(device, None, None)
        return updates

    updates = asyncio.run(run())
    assert updates[0][1]["outcome"] == "muted"


def test_trigger_voice_turn_handles_audio_timeout_and_cleanup(monkeypatch):
    async def run():
        device = FakeDevice()
        device.last_wake = None
        updates = []
        events = []
        monkeypatch.setattr(engine.db, "create_turn", lambda *args: 22)
        monkeypatch.setattr(engine.db, "update_turn", lambda *args: updates.append(args))
        monkeypatch.setattr(engine.db, "get_turn", lambda *args: None)
        monkeypatch.setattr(engine, "_push_event", lambda event: asyncio.sleep(0, result=events.append(event)))
        async def timeout(_turn):
            raise asyncio.TimeoutError()
        monkeypatch.setattr(engine, "_run_turn", timeout)
        engine.ENGINE.turns.clear()
        result = await engine.trigger_voice_turn(device, None, None)
        assert result is False
        assert updates[0][1]["outcome"] == "audio_timeout"
        assert events[-1] == {"type": "turn.terminal", "device_id": "device-1", "turn_id": 22, "outcome": "audio_timeout"}
        assert 22 not in engine.ENGINE.turns

    asyncio.run(run())


def test_pipeline_event_sets_continue_conversation(monkeypatch):
    async def run():
        turn = engine.Turn(20, FakeDevice(), None, None)
        engine.ENGINE.turns[20] = turn
        try:
            class Request:
                match_info = {"tid": "20"}
                path = "/api/turns/20/pipeline-event"
                async def json(self):
                    return {"event": "intent_end", "continue_conversation": True}

            await engine.turn_action(Request())
            assert turn.continue_conversation is True
        finally:
            engine.ENGINE.turns.pop(20, None)

    asyncio.run(run())
    async def run():
        events = []

        class FakeApi:
            async def _push_event(self, event):
                events.append(event)

        monkeypatch.setitem(sys.modules, "em_api", FakeApi())
        em_ha_sidechannels.button_event("device-1", "long", 900)
        await asyncio.sleep(0)
        assert events == [{
            "type": "button.event", "device_id": "device-1",
            "gesture": "long", "held_ms": 900,
        }]

    asyncio.run(run())
