"""Phase 4 cutover additions to em_turn_engine.py — repointing
VOICE_PREROLL_DISCARD/BUTTON_HOLD_MS/VAD_SENTINEL_* from em_esphome.py "with
equivalent signatures" (docs/design/full-duplex-plan.md) turned out to mean more than
relocating the names: the no-speech short-circuit and the preroll discard
were being computed upstream and silently thrown away. See em_turn_engine.py
for the full reasoning; this file pins the resulting behavior.
"""

import asyncio
import collections

import em_announce
import em_db
import em_turn_engine as engine
from em_audio_frame import MIC_FRAME_BYTES, MIC_PCM, decode_frame


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
        self.last_wake = None
        self.controls = []

    async def send_control(self, msg):
        self.controls.append(msg)


def test_constants_match_the_old_esphome_values():
    # Not load-bearing on their own, but a silent value change here would
    # change wake-tail trimming / hold-gesture timing without anyone
    # noticing — pin them explicitly.
    assert engine.VOICE_PREROLL_DISCARD == 0
    assert engine.BUTTON_HOLD_MS == 750
    assert engine.VAD_SENTINEL_END == "vad_end"
    assert engine.VAD_SENTINEL_TIMEOUT == "vad_no_speech_timeout"
    assert engine.VAD_SENTINEL_END != engine.VAD_SENTINEL_TIMEOUT


# ── No-speech short-circuit ─────────────────────────────────────────────────

def test_no_speech_timeout_sentinel_sets_no_speech_and_endpoint():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        turn.socket = FakeSocket()
        await device.voice_queue.put(engine.VAD_SENTINEL_TIMEOUT)
        await engine._send_mic(turn)
        assert turn.no_speech is True
        assert turn.endpoint.is_set()

    asyncio.run(run())


def test_normal_vad_end_sentinel_does_not_set_no_speech():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        turn.socket = FakeSocket()
        await device.voice_queue.put(engine.VAD_SENTINEL_END)
        await engine._send_mic(turn)
        assert turn.no_speech is False
        assert turn.endpoint.is_set()

    asyncio.run(run())


def test_run_turn_skips_post_turn_play_on_no_speech():
    async def run():
        device = FakeDevice()
        played = []

        async def post_turn_play(chunks):
            played.append(True)

        turn = engine.Turn(1, device, None, post_turn_play)
        turn.socket = object()
        turn.socket_ready.set()
        turn.no_speech = True
        turn.endpoint.set()

        result = await engine._run_turn(turn)
        return result, played

    result, played = asyncio.run(run())
    assert result is True
    assert played == []  # never waited on a TTS response that wasn't coming


def test_outcome_for_reports_no_speech_over_ok_or_cancelled():
    device = FakeDevice()
    turn = engine.Turn(1, device, None, None)
    turn.no_speech = True
    assert engine._outcome_for(turn, True) == "no_speech"
    assert engine._outcome_for(turn, False) == "no_speech"  # no_speech wins


def test_outcome_for_falls_back_to_ok_or_cancelled_when_not_no_speech():
    device = FakeDevice()
    turn = engine.Turn(1, device, None, None)
    assert engine._outcome_for(turn, True) == "ok"
    assert engine._outcome_for(turn, False) == "cancelled"


def test_finish_created_turn_persists_no_speech_outcome(monkeypatch, tmp_path):
    import em_db

    old_conn, old_path = em_db._conn, em_db._db_path
    em_db.init(str(tmp_path / "no_speech.db"))
    monkeypatch.setattr(engine, "ENGINE", engine.TurnEngine())
    events = []

    async def push(event):
        events.append(event)

    monkeypatch.setattr(engine, "_push_event", push)

    async def run():
        device = FakeDevice()
        turn_id = em_db.create_turn(device.device_id, "announcement")
        turn = engine.Turn(turn_id, device, None, None)
        turn.socket_ready.set()
        turn.no_speech = True
        turn.endpoint.set()
        engine.ENGINE.turns[turn_id] = turn
        await engine._finish_created_turn(turn)
        return turn_id

    try:
        turn_id = asyncio.run(run())
        row = em_db._q1("SELECT outcome FROM turns WHERE id = ?", (turn_id,))
        assert row["outcome"] == "no_speech"
        assert events[-1]["outcome"] == "no_speech"
    finally:
        if em_db._conn is not None:
            em_db._conn.close()
        em_db._conn, em_db._db_path = old_conn, old_path


# ── Preroll discard (wake-word tail trimming) ───────────────────────────────

def test_preroll_frames_are_discarded_before_forwarding():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None, preroll_remaining=2)
        turn.socket = FakeSocket()
        wake_tail = b"\x01" * MIC_FRAME_BYTES
        command = b"\x02" * MIC_FRAME_BYTES
        await device.voice_queue.put(wake_tail)
        await device.voice_queue.put(wake_tail)
        await device.voice_queue.put(command)
        await device.voice_queue.put(engine.VAD_SENTINEL_END)
        await engine._send_mic(turn)

        forwarded = [f for f in turn.socket.frames if f.frame_type == MIC_PCM]
        assert len(forwarded) == 1
        assert forwarded[0].payload == command
        assert turn.preroll_remaining == 0

    asyncio.run(run())


def test_zero_preroll_forwards_every_frame():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)  # preroll_remaining defaults to 0
        turn.socket = FakeSocket()
        frame = b"\x03" * MIC_FRAME_BYTES
        await device.voice_queue.put(frame)
        await device.voice_queue.put(engine.VAD_SENTINEL_END)
        await engine._send_mic(turn)

        forwarded = [f for f in turn.socket.frames if f.frame_type == MIC_PCM]
        assert len(forwarded) == 1

    asyncio.run(run())


def test_initial_audio_is_forwarded_before_live_voice_queue_frames():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None, initial_audio=(
            b"\x01" * MIC_FRAME_BYTES,
            b"\x02" * MIC_FRAME_BYTES,
        ))
        turn.socket = FakeSocket()
        live = b"\x03" * MIC_FRAME_BYTES
        await device.voice_queue.put(live)
        await device.voice_queue.put(engine.VAD_SENTINEL_END)
        await engine._send_mic(turn)

        forwarded = [f.payload for f in turn.socket.frames if f.frame_type == MIC_PCM]
        assert forwarded == [
            b"\x01" * MIC_FRAME_BYTES,
            b"\x02" * MIC_FRAME_BYTES,
            live,
        ]

    asyncio.run(run())


def test_two_second_preroll_and_live_audio_have_one_ordered_boundary():
    async def run():
        device = FakeDevice()
        preroll = tuple(bytes([i]) * MIC_FRAME_BYTES for i in range(25))
        turn = engine.Turn(1, device, None, None, initial_audio=preroll)
        turn.socket = FakeSocket()
        live = bytes([25]) * MIC_FRAME_BYTES
        await device.voice_queue.put(live)
        await device.voice_queue.put(engine.VAD_SENTINEL_END)

        await engine._send_mic(turn)

        forwarded = [f.payload for f in turn.socket.frames if f.frame_type == MIC_PCM]
        assert forwarded == list(preroll) + [live]

    asyncio.run(run())


def test_ring_preroll_does_not_discard_the_following_live_frames():
    async def run():
        device = FakeDevice()
        preroll = tuple(bytes([i]) * MIC_FRAME_BYTES for i in range(25))
        turn = engine.Turn(
            1, device, None, None,
            initial_audio=preroll,
            preroll_remaining=engine.VOICE_PREROLL_DISCARD,
        )
        turn.socket = FakeSocket()
        live = bytes([25]) * MIC_FRAME_BYTES
        await device.voice_queue.put(live)
        await device.voice_queue.put(engine.VAD_SENTINEL_END)

        await engine._send_mic(turn)

        forwarded = [f.payload for f in turn.socket.frames if f.frame_type == MIC_PCM]
        assert forwarded == list(preroll) + [live]

    asyncio.run(run())


def test_trigger_voice_turn_wires_preroll_discard_through_a_real_turn(monkeypatch, tmp_path):
    import em_db

    old_conn, old_path = em_db._conn, em_db._db_path
    em_db.init(str(tmp_path / "preroll.db"))
    monkeypatch.setattr(engine, "ENGINE", engine.TurnEngine())

    async def push(event):
        pass

    monkeypatch.setattr(engine, "_push_event", push)

    async def run():
        device = FakeDevice()

        async def drive():
            for _ in range(50):
                if engine.ENGINE.turns:
                    break
                await asyncio.sleep(0.01)
            turn = next(iter(engine.ENGINE.turns.values()))
            assert turn.preroll_remaining == engine.VOICE_PREROLL_DISCARD
            turn.socket_ready.set()
            turn.endpoint.set()
            turn.tts_end.set()

        driver = asyncio.create_task(drive())
        await engine.trigger_voice_turn(
            device, None, None, trigger_label="wakeword(0.9)",
            preroll_discard=engine.VOICE_PREROLL_DISCARD,
        )
        await driver

    try:
        asyncio.run(run())
    finally:
        if em_db._conn is not None:
            em_db._conn.close()
        em_db._conn, em_db._db_path = old_conn, old_path


# ── Bounded TTS/announcement playback ───────────────────────────────────────

def test_run_turn_bounds_post_turn_play_at_the_announce_timeout(monkeypatch):
    monkeypatch.setattr(em_announce, "ANNOUNCE_TIMEOUT_S", 0.05)

    async def push(event):
        pass

    monkeypatch.setattr(engine, "_push_event", push)

    async def run():
        device = FakeDevice()

        async def wedged(chunks):
            await asyncio.sleep(30)

        turn = engine.Turn(1, device, None, wedged)
        turn.socket = object()
        turn.socket_ready.set()
        turn.endpoint.set()

        try:
            await engine._run_turn(turn)
        except asyncio.TimeoutError:
            return "timed_out"
        return "did_not_time_out"

    assert asyncio.run(run()) == "timed_out"


def test_finish_created_turn_reports_audio_timeout_for_a_wedged_playback(monkeypatch, tmp_path):
    import em_db

    old_conn, old_path = em_db._conn, em_db._db_path
    em_db.init(str(tmp_path / "wedged.db"))
    monkeypatch.setattr(em_announce, "ANNOUNCE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(engine, "ENGINE", engine.TurnEngine())
    events = []

    async def push(event):
        events.append(event)

    monkeypatch.setattr(engine, "_push_event", push)

    async def run():
        device = FakeDevice()

        async def wedged(chunks):
            await asyncio.sleep(30)

        turn_id = em_db.create_turn(device.device_id, "announcement")
        turn = engine.Turn(turn_id, device, None, wedged)
        turn.socket = object()
        turn.socket_ready.set()
        turn.endpoint.set()
        engine.ENGINE.turns[turn_id] = turn

        await engine._finish_created_turn(turn)
        return turn_id

    try:
        turn_id = asyncio.run(run())
        row = em_db._q1("SELECT outcome FROM turns WHERE id = ?", (turn_id,))
        assert row["outcome"] == "audio_timeout"
        terminal = next(e for e in events if e.get("type") == "turn.terminal")
        assert terminal["outcome"] == "audio_timeout"
    finally:
        if em_db._conn is not None:
            em_db._conn.close()
        em_db._conn, em_db._db_path = old_conn, old_path


# ── device.turn_history stays live (playback-stats attachment + Activity tab) ─

def test_remember_turn_appends_the_persisted_row_in_get_turns_shape(tmp_path):
    old_conn, old_path = em_db._conn, em_db._db_path
    em_db.init(str(tmp_path / "remember.db"))
    try:
        async def run():
            device = FakeDevice()
            turn_id = em_db.create_turn(device.device_id, "wakeword(0.9)")
            em_db.update_turn(turn_id, {"outcome": "ok", "wake_score": 0.9})
            await engine._remember_turn(device, turn_id)
            return device, turn_id

        device, turn_id = asyncio.run(run())
        assert len(device.turn_history) == 1
        rec = device.turn_history[0]
        assert rec["turn_id"] == turn_id
        assert rec["outcome"] == "ok"
        assert rec["wake_score"] == 0.9
        # Same shape get_turns() would hydrate the deque with at connect time.
        assert rec.keys() == em_db.get_turns(device.device_id)[0].keys()
    finally:
        if em_db._conn is not None:
            em_db._conn.close()
        em_db._conn, em_db._db_path = old_conn, old_path


def test_remember_turn_is_a_quiet_no_op_for_an_unknown_turn_id(tmp_path):
    old_conn, old_path = em_db._conn, em_db._db_path
    em_db.init(str(tmp_path / "unknown_turn.db"))
    try:
        async def run():
            device = FakeDevice()
            await engine._remember_turn(device, 999999)  # never persisted
            return device

        device = asyncio.run(run())  # must not raise
        assert len(device.turn_history) == 0
    finally:
        if em_db._conn is not None:
            em_db._conn.close()
        em_db._conn, em_db._db_path = old_conn, old_path


# ── Wake telemetry reaches the turn row (device.last_wake) ─────────────────

def test_trigger_voice_turn_persists_and_pops_last_wake(monkeypatch, tmp_path):
    old_conn, old_path = em_db._conn, em_db._db_path
    em_db.init(str(tmp_path / "wake_info.db"))
    monkeypatch.setattr(engine, "ENGINE", engine.TurnEngine())

    async def push(event):
        pass

    monkeypatch.setattr(engine, "_push_event", push)

    async def run():
        device = FakeDevice()
        device.last_wake = {
            "model": "hey_jarvis_v0.1", "score": 0.87,
            "threshold": 0.5, "noise_floor": 0.01,
        }

        async def drive():
            for _ in range(50):
                if engine.ENGINE.turns:
                    break
                await asyncio.sleep(0.01)
            turn = next(iter(engine.ENGINE.turns.values()))
            turn.socket_ready.set()
            turn.endpoint.set()
            turn.tts_end.set()

        driver = asyncio.create_task(drive())
        await engine.trigger_voice_turn(
            device, None, None, trigger_label="wakeword(0.87)",
        )
        await driver
        return device

    try:
        device = asyncio.run(run())
        assert device.last_wake is None  # popped, not merely read
        row = device.turn_history[-1]
        assert row["wake_model"] == "hey_jarvis_v0.1"
        assert row["wake_score"] == 0.87
        assert row["wake_threshold"] == 0.5
        assert row["noise_floor"] == 0.01
    finally:
        if em_db._conn is not None:
            em_db._conn.close()
        em_db._conn, em_db._db_path = old_conn, old_path


def test_trigger_voice_turn_writes_null_wake_columns_for_a_button_turn(monkeypatch, tmp_path):
    old_conn, old_path = em_db._conn, em_db._db_path
    em_db.init(str(tmp_path / "button_wake.db"))
    monkeypatch.setattr(engine, "ENGINE", engine.TurnEngine())

    async def push(event):
        pass

    monkeypatch.setattr(engine, "_push_event", push)

    async def run():
        device = FakeDevice()
        device.last_wake = None  # button turns have no wake detail

        async def drive():
            for _ in range(50):
                if engine.ENGINE.turns:
                    break
                await asyncio.sleep(0.01)
            turn = next(iter(engine.ENGINE.turns.values()))
            turn.socket_ready.set()
            turn.endpoint.set()
            turn.tts_end.set()

        driver = asyncio.create_task(drive())
        await engine.trigger_voice_turn(device, None, None, trigger_label="button")
        await driver
        return device

    try:
        device = asyncio.run(run())
        row = device.turn_history[-1]
        assert row["wake_model"] is None
        assert row["wake_score"] is None
    finally:
        if em_db._conn is not None:
            em_db._conn.close()
        em_db._conn, em_db._db_path = old_conn, old_path


# ── No-speech downgrade on speech evidence (turn 650) ────────────────────────
#
# Turn 650: the device's 5s no-speech deadline fired while HA was still
# transcribing — 11 partials had already arrived, the full command among
# them, with the final STT ~1s behind the deadline. Treating the timeout as
# "nothing said" skipped the TTS wait, so no timer and no answer. A timeout
# that arrives with partial transcripts in hand is a lost race, not a silent
# room: it must end the mic side normally (EOS) and wait for HA's response.

class _TranscriptRequest:
    def __init__(self, tid, body):
        self.match_info = {"tid": str(tid)}
        self.path = f"/api/turns/{tid}/transcript"
        self._body = body

    async def json(self):
        return self._body


def test_no_speech_timeout_with_partials_is_treated_as_normal_end():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        turn.socket = FakeSocket()
        turn.stt_text = "hey apartment, set a timer for 1 minute."
        await device.voice_queue.put(engine.VAD_SENTINEL_TIMEOUT)
        await engine._send_mic(turn)
        assert turn.no_speech is False
        assert turn.endpoint.is_set()
        assert [frame.frame_type for frame in turn.socket.frames] == [2]  # MIC_EOS

    asyncio.run(run())


def test_no_speech_timeout_without_partials_still_ends_quietly():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(1, device, None, None)
        turn.socket = FakeSocket()
        await device.voice_queue.put(engine.VAD_SENTINEL_TIMEOUT)
        await engine._send_mic(turn)
        assert turn.no_speech is True
        assert turn.endpoint.is_set()

    asyncio.run(run())


def test_first_partial_transcript_disarms_the_device_deadline():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(7, device, None, None)
        engine.ENGINE.turns[7] = turn
        try:
            await engine.turn_action(_TranscriptRequest(7, {"text": "hey", "is_final": False}))
            assert device.controls == [{"type": "no_speech_disarm"}]
            assert turn.no_speech_disarmed is True
            # One disarm per turn — later partials must not re-send.
            await engine.turn_action(
                _TranscriptRequest(7, {"text": "hey apartment", "is_final": False}))
            assert device.controls == [{"type": "no_speech_disarm"}]
        finally:
            engine.ENGINE.turns.pop(7, None)

    asyncio.run(run())


def test_final_transcript_disarms_when_no_partial_came_first():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(8, device, None, None)
        engine.ENGINE.turns[8] = turn
        try:
            await engine.turn_action(_TranscriptRequest(8, {"text": "stop"}))
            assert device.controls == [{"type": "no_speech_disarm"}]
        finally:
            engine.ENGINE.turns.pop(8, None)

    asyncio.run(run())


def test_empty_transcript_text_does_not_disarm():
    async def run():
        device = FakeDevice()
        turn = engine.Turn(9, device, None, None)
        engine.ENGINE.turns[9] = turn
        try:
            await engine.turn_action(_TranscriptRequest(9, {"text": ""}))
            assert device.controls == []
            assert turn.no_speech_disarmed is False
        finally:
            engine.ENGINE.turns.pop(9, None)

    asyncio.run(run())


def test_run_turn_waits_for_tts_after_a_downgraded_timeout(monkeypatch):
    async def push(_event):
        pass

    monkeypatch.setattr(engine, "_push_event", push)

    async def run():
        device = FakeDevice()
        played = []

        async def post_turn_play(chunks):
            async for _ in chunks:
                pass
            played.append(True)

        turn = engine.Turn(1, device, None, post_turn_play)
        turn.socket = FakeSocket()
        turn.socket_ready.set()
        # Downgraded timeout: endpoint set by the sentinel path, no_speech
        # deliberately False because partials had arrived.
        turn.stt_text = "hey apartment, set a timer for 1 minute."
        turn.no_speech = False
        turn.endpoint.set()
        await turn.tts_queue.put(None)  # HACS tts/end
        result = await engine._run_turn(turn)
        return result, played

    result, played = asyncio.run(run())
    assert result is True
    assert played == [True]
