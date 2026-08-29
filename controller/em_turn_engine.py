"""HA-facing voice turn engine.

The controller owns device capture and playback. This module owns the live
turn rendezvous with the HACS integration: REST/event lifecycle plus one
bidirectional audio WebSocket per turn. See docs/design/full-duplex-plan.md for the full
design and CLAUDE.md's "Voice backend" section for how this fits with
`em_controller.py` and the HACS integration under `hacs/`.

Known gaps, deliberately not closed by the Phase 4 cutover (mechanical
repoint + delete of the ESPHome-impersonation backend) — each is a real,
scoped follow-up rather than a silent omission:

- **Barge-in does not yet abort a running HA pipeline.** `_barge_watcher`
  (`em_controller.py`) still calls `abort_ha_run` on a barge-in exactly as it
  called the ESPHome-mode equivalent, but that call does not reach a pipeline
  HA has already started — `turn.cancel` reaches this module's turn state,
  not HA's `AssistSatelliteEntity`. `em_runbarrier`'s serialisation logic is
  kept for when this is wired through (see CLAUDE.md's "Barge-in
  serialisation is currently unused, not removed").
- **No SNR-relative no-speech backstop via `em_turnclock`.** The old
  ESPHome-mode turn used `em_turnclock` to end a turn against each room's
  measured noise floor rather than a fixed timer; this module does not yet
  call it, so no-speech timing here is less room-adaptive than the backend it
  replaced. It is bounded, though (`ENDPOINT_WAIT_TIMEOUT_S` on `_run_turn`'s
  `turn.endpoint.wait()`) — see that constant's comment for why an unbounded
  version of this same gap once hung a device's voice_lock for 3+ hours.
- **No `pending_playback_stats` fold-in for a turn whose DB row doesn't exist
  yet.** If a device's `playback_stats` report for a turn arrives before
  `db.create_turn`'s row exists (a narrow ordering window), it is not folded
  into that turn's persisted stats.
- **No on-device shadow-wake correlation via `last_wake_mono`.** The shadow
  wake-word comparison (`em_shadow.ShadowTracker`) is not yet wired to turns
  created here, so an on-device threshold crossing is not correlated against
  turns run through this engine the way it was under the old backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

import numpy as np
from aiohttp import web

import em_announce
import em_audio_frame as audio
import em_db as db
import em_recordings

log = logging.getLogger("echomuse.turn_engine")

# Ported from em_esphome.py's module constants (Phase 4 cutover) — same
# values, same meaning, now protocol-agnostic.
# The wake listener consumes the pre-trigger frames itself. Discarding more
# frames here removes the beginning of the user's command when they speak
# naturally after the wake word; retain the first routed frame instead.
VOICE_PREROLL_DISCARD = 0
BUTTON_HOLD_MS = 750        # device-measured heldMs threshold for a HOLD gesture

# Ceiling on how long _run_turn waits for *something* — the device's own VAD
# (a sentinel queued into voice_queue, see _send_mic) or HA's pipeline
# (POST .../endpoint) — to say a turn's speech has ended. Before this, that
# wait was unbounded: a marginal wake (score 0.403 against a 0.400 threshold,
# 2026-08-19 — effectively noise) started a turn where neither signal ever
# arrived, and _run_turn's `await turn.endpoint.wait()` hung for 3+ hours,
# holding device.voice_lock the entire time — wake word detection pauses for
# the duration of a locked turn, so the device looked permanently "stuck
# listening" with no way to recover short of a controller restart.
#
# This is deliberately the narrow fix, not the full em_turnclock port this
# module's docstring above still calls out as a gap: em_turnclock's two-clock
# logic (first-audio grace vs. no-speech-after-audio) needs the mic frames
# analysed for real speech against the room's noise floor, which _send_mic
# does not do — it only forwards frames. A single generous ceiling instead
# guarantees the turn always resolves, reusing the exact "audio_timeout"
# outcome and cleanup path a socket that never opens already produces
# (trigger_voice_turn's and _finish_created_turn's existing
# `except asyncio.TimeoutError` handlers), rather than adding a new one.
# 30s comfortably covers a long utterance plus VAD-close plus STT round trip
# (a healthy turn reaches /endpoint in a few seconds) while still recovering
# a genuinely stuck turn in a bounded time instead of hours.
ENDPOINT_WAIT_TIMEOUT_S = 30.0

# String sentinels queued into device.voice_queue in place of audio bytes —
# distinct values (not just "any string") because _send_mic below tells them
# apart: a device-reported no-speech timeout ends the turn immediately
# without waiting on a TTS response nobody was ever going to send, exactly
# as em_esphome._stream_mic_audio did. Defined here rather than in
# em_controller.py so there is one source of truth for both the producer
# (handle_data) and the consumer (_send_mic).
VAD_SENTINEL_END = "vad_end"
VAD_SENTINEL_TIMEOUT = "vad_no_speech_timeout"


@dataclass
class Turn:
    turn_id: int
    device: object
    on_thinking: object
    post_turn_play: object
    kind: str = "conversation"
    socket_ready: asyncio.Event = field(default_factory=asyncio.Event)
    endpoint: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    # The cause of an early end is diagnostic; cancellation itself still gates
    # control flow. A playback barge records its cause without cancelling a
    # pipeline that this native integration cannot abort yet.
    end_reason: str | None = None
    tts_end: asyncio.Event = field(default_factory=asyncio.Event)
    tts_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    socket: web.WebSocketResponse | None = None
    tts_sequence: audio.Sequence = field(default_factory=audio.Sequence)
    mic_sequence: int = 0
    continue_conversation: bool = False
    # Set by _send_mic on VAD_SENTINEL_TIMEOUT — the device never detected
    # speech at all, so there is nothing for HA to have replied to. Read by
    # _run_turn to skip the TTS wait and by the outcome-reporting callers to
    # record "no_speech" instead of "ok".
    no_speech: bool = False
    # Legacy discard knob retained for callers that still need it. Normal wake
    # turns use initial_audio for sequence-addressed preroll instead.
    preroll_remaining: int = 0
    started_mono: float = field(default_factory=time.monotonic)
    endpoint_mono: float | None = None
    transcript_mono: float | None = None
    intent_mono: float | None = None
    tts_started_mono: float | None = None
    tts_ended_mono: float | None = None
    stt_text: str | None = None
    tts_text: str | None = None
    capture_audio: bool = False
    initial_audio: tuple[bytes, ...] = ()
    mic_audio: bytearray = field(default_factory=bytearray)
    tts_audio: bytearray = field(default_factory=bytearray)


class TurnEngine:
    """Per-controller turn state and audio-socket registry."""

    def __init__(self) -> None:
        self.audio_sockets: dict[int, web.WebSocketResponse] = {}
        self.turns: dict[int, Turn] = {}

    def register_audio_socket(self, turn_id: int, ws: web.WebSocketResponse) -> None:
        self.audio_sockets[turn_id] = ws

    def unregister_audio_socket(self, turn_id: int, ws: web.WebSocketResponse) -> None:
        if self.audio_sockets.get(turn_id) is ws:
            self.audio_sockets.pop(turn_id, None)
        turn = self.turns.get(turn_id)
        if turn is not None and turn.socket is ws:
            turn.socket = None
            turn.tts_queue.put_nowait(None)


ENGINE = TurnEngine()
TEST_TASKS: dict[str, asyncio.Task] = {}


def test_turn_active(device_id: str) -> bool:
    task = TEST_TASKS.get(device_id)
    return task is not None and not task.done()


async def _push_event(event: dict) -> None:
    # Lazy import avoids em_api -> turn_engine -> em_api import cycles.
    import em_api
    await em_api._push_event(event)


async def _call(callback) -> None:
    if callback is None:
        return
    result = callback()
    if hasattr(result, "__await__"):
        await result


async def _not_ready(request: web.Request) -> web.Response:
    return web.json_response(
        {"error": "turn_engine_not_ready", "message": "Voice turn engine is not enabled yet"},
        status=501,
    )


async def audio_socket(request: web.Request) -> web.StreamResponse:
    """Handle one bidirectional per-turn HA audio connection."""
    try:
        turn_id = int(request.match_info["turn_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="invalid turn id")
    turn = ENGINE.turns.get(turn_id)
    if turn is None:
        raise web.HTTPNotFound(text="turn not found")

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    if turn.socket is not None:
        await ws.close(code=1008, message=b"turn already has an audio connection")
        return ws

    turn.socket = ws
    ENGINE.register_audio_socket(turn_id, ws)
    turn.socket_ready.set()
    await _push_event({
        "type": "turn.state", "device_id": turn.device.device_id,
        "turn_id": turn_id, "state": "listening",
    })
    try:
        async for message in ws:
            if message.type != 2:  # aiohttp.WSMsgType.BINARY
                continue
            try:
                frame = audio.decode_frame(message.data)
                turn.tts_sequence.accept(frame)
            except audio.AudioProtocolError as exc:
                await ws.close(code=1002, message=str(exc).encode()[:120])
                break
            if frame.frame_type not in (audio.TTS_PCM, audio.TTS_EOS):
                await ws.close(code=1002, message=b"unexpected audio direction")
                break
            await turn.tts_queue.put(
                None if frame.frame_type == audio.TTS_EOS else frame.payload
            )
            if frame.frame_type == audio.TTS_PCM and turn.capture_audio:
                remaining = 120 * 24_000 * 2 - len(turn.tts_audio)
                if remaining > 0:
                    turn.tts_audio.extend(frame.payload[:remaining])
            if frame.frame_type == audio.TTS_EOS:
                turn.tts_ended_mono = turn.tts_ended_mono or time.monotonic()
                break
    finally:
        ENGINE.unregister_audio_socket(turn_id, ws)
        turn.tts_end.set()
    return ws


async def create_turn(request: web.Request) -> web.Response:
    """Create an HACS-initiated announcement turn."""
    device_id = request.match_info.get("id")
    import em_api
    device = em_api._devices.get(device_id)
    if device is None:
        return web.json_response({"error": "device_offline"}, status=409)
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = body.get("kind", "announcement")
    if kind != "announcement":
        return web.json_response({"error": "unsupported_turn_kind"}, status=400)
    turn_id = await asyncio.get_running_loop().run_in_executor(
        None, db.create_turn, device_id, kind
    )

    async def play(chunks):
        import em_controller
        device.cancel_event.clear()
        await em_controller._run_streaming_post_turn_playback(device, chunks)

    turn = Turn(
        turn_id, device, None, play, kind=kind,
        capture_audio=bool(getattr(device, "save_utterances", False)),
    )
    turn.endpoint.set()
    ENGINE.turns[turn_id] = turn
    asyncio.create_task(_finish_created_turn(turn), name=f"announcement-{turn_id}")
    return web.json_response({"turn_id": turn_id, "kind": kind}, status=201)


async def turn_action(request: web.Request) -> web.Response:
    try:
        turn_id = int(request.match_info["tid"])
    except (KeyError, ValueError):
        return web.json_response({"error": "invalid_turn_id"}, status=400)
    turn = ENGINE.turns.get(turn_id)
    if turn is None:
        return web.json_response({"error": "turn_not_found"}, status=404)
    prefix = f"/api/turns/{turn_id}/"
    action = request.path[len(prefix):] if request.path.startswith(prefix) else ""
    if action == "endpoint":
        turn.endpoint_mono = turn.endpoint_mono or time.monotonic()
        turn.endpoint.set()
        await _call(turn.on_thinking)
        await _push_event({
            "type": "turn.state", "device_id": turn.device.device_id,
            "turn_id": turn_id, "state": "processing",
        })
    elif action == "cancel":
        _end_turn(turn, "cancelled")
    elif action == "tts/end":
        turn.tts_ended_mono = turn.tts_ended_mono or time.monotonic()
        turn.tts_end.set()
        turn.tts_queue.put_nowait(None)
    elif action == "tts/start":
        turn.tts_started_mono = turn.tts_started_mono or time.monotonic()
    elif action == "transcript":
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = body.get("text")
        if isinstance(text, str) and text:
            turn.stt_text = text
            turn.transcript_mono = turn.transcript_mono or time.monotonic()
    elif action == "tts-text":
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = body.get("text")
        if isinstance(text, str) and text:
            turn.tts_text = text
    elif action == "pipeline-event":
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        event = body.get("event", "")
        if event == "intent_end":
            turn.intent_mono = turn.intent_mono or time.monotonic()
            turn.continue_conversation = body.get("continue_conversation", False)
    return web.json_response({"turn_id": turn_id, "action": action})


async def _send_mic(turn: Turn) -> None:
    """Forward the controller's voice queue as fixed-size PCM frames."""
    async def forward(raw_payload: bytes) -> None:
        payload = raw_payload
        if turn.capture_audio:
            remaining = em_recordings.MAX_UTTERANCE_BYTES - len(turn.mic_audio)
            if remaining > 0:
                turn.mic_audio.extend(payload[:remaining])
        await turn.socket.send_bytes(
            audio.encode_frame(audio.MIC_PCM, turn.mic_sequence, payload)
        )
        turn.mic_sequence += 1

    try:
        for payload in turn.initial_audio:
            if turn.socket is None or turn.cancelled.is_set():
                return
            await forward(payload)
        while not turn.cancelled.is_set():
            try:
                payload = await asyncio.wait_for(turn.device.voice_queue.get(), 1.0)
            except asyncio.TimeoutError:
                continue
            if payload is None or isinstance(payload, str):
                if payload == VAD_SENTINEL_TIMEOUT:
                    # No speech was ever detected — mirrors em_esphome's old
                    # _no_speech_timeout shortcut: nothing was streamed for HA to
                    # respond to, so _run_turn skips the TTS round-trip entirely
                    # rather than waiting out a response that isn't coming.
                    turn.no_speech = True
                turn.endpoint.set()
                break
            if turn.socket is None:
                turn.endpoint.set()
                return
            if len(payload) != audio.MIC_FRAME_BYTES:
                log.warning("[%s] dropping non-80ms mic payload (%d bytes)",
                            getattr(turn.device, "device_id", "?"), len(payload))
                continue
            # Ring-backed audio already ends at the live boundary. Discarding
            # here would remove the first live frames and create a gap after
            # the prepended preroll; the discard is only for fallback wakes
            # without an initial ring window.
            if turn.preroll_remaining > 0 and not turn.initial_audio:
                turn.preroll_remaining -= 1
                continue

            await forward(payload)
        if turn.socket is not None and not turn.cancelled.is_set():
            await turn.socket.send_bytes(audio.encode_frame(audio.MIC_EOS, turn.mic_sequence))
    finally:
        pass

class _StreamingUpsampler:
    """Stateful 24->48kHz linear interpolator for streamed TTS.

    Calling scipy.signal.resample_poly independently per HA TTS chunk resets
    its FIR at every arbitrary network boundary, producing a transient in the
    middle of speech. Hold one input sample so the interpolation across each
    boundary is exactly the same as processing the concatenated response.
    """

    def __init__(self):
        self._last: int | None = None

    def process(self, payload: bytes) -> bytes:
        samples = np.frombuffer(payload, dtype="<i2").astype(np.int32)
        if samples.size == 0:
            return b""
        if self._last is not None:
            samples = np.concatenate([
                np.array([self._last], dtype=np.int32), samples,
            ])
        self._last = int(samples[-1])
        if samples.size < 2:
            return b""

        out = np.empty((samples.size - 1) * 2, dtype=np.int32)
        out[0::2] = samples[:-1]
        out[1::2] = (samples[:-1] + samples[1:]) // 2
        return out.astype(np.int16).tobytes()

    def flush(self) -> bytes:
        """Emit the final held sample and preserve the exact 2x length."""
        if self._last is None:
            return b""
        last = self._last
        self._last = None
        return np.array([last, last], dtype=np.int16).tobytes()


def _upsample_24_to_48(payload: bytes) -> bytes:
    """Upsample a complete mono S16 response with the streaming algorithm."""
    upsampler = _StreamingUpsampler()
    return upsampler.process(payload) + upsampler.flush()


async def _tts_chunks(turn: Turn):
    upsampler = _StreamingUpsampler()
    while True:
        chunk = await turn.tts_queue.get()
        if chunk is None:
            tail = upsampler.flush()
            if tail:
                yield tail
            return
        output = upsampler.process(chunk)
        if output:
            yield output


async def _run_turn(turn: Turn) -> bool:
    await asyncio.wait_for(turn.socket_ready.wait(), 15.0)
    mic_task = asyncio.create_task(_send_mic(turn))
    try:
        await asyncio.wait_for(turn.endpoint.wait(), ENDPOINT_WAIT_TIMEOUT_S)
        if turn.cancelled.is_set():
            return False
        if turn.no_speech:
            # Nothing was ever streamed for HA to respond to — waiting on
            # post_turn_play here would wait on a TTS response that the
            # device's own silence guarantees never arrives.
            return True
        if turn.socket is not None:
            await _push_event({
                "type": "turn.state", "device_id": turn.device.device_id,
                "turn_id": turn.turn_id, "state": "responding",
            })
            # Bounded the same way em_announce.run bounds a standalone
            # announcement: a wedged playback (device gone mid-stream, a
            # stuck ffmpeg on the HA side) must not hold this turn — and the
            # device's own voice_lock — forever. asyncio.TimeoutError
            # propagates to the callers' existing "audio_timeout" handling.
            await asyncio.wait_for(
                turn.post_turn_play(_tts_chunks(turn)), em_announce.ANNOUNCE_TIMEOUT_S
            )
        else:
            await turn.tts_end.wait()
        return not turn.cancelled.is_set()
    finally:
        mic_task.cancel()
        await asyncio.gather(mic_task, return_exceptions=True)


def _outcome_for(turn: Turn, result: bool) -> str:
    if turn.end_reason is not None:
        return turn.end_reason
    if turn.no_speech:
        return "no_speech"
    return "ok" if result else "cancelled"


def _turn_record(turn: Turn, outcome: str) -> dict:
    """Persist native turn-engine timings as independent stage durations."""
    end = time.monotonic()
    stt_end = turn.transcript_mono or turn.endpoint_mono
    ha_start = stt_end or turn.started_mono
    ha_end = turn.tts_started_mono or turn.intent_mono
    return {
        "outcome": outcome,
        "stt_text": turn.stt_text,
        "tts_text": turn.tts_text,
        "total_ms": round((end - turn.started_mono) * 1000),
        "stt_latency_ms": round((stt_end - turn.started_mono) * 1000) if stt_end else None,
        "ha_latency_ms": round((ha_end - ha_start) * 1000) if ha_end else None,
        # User-perceived TTS is not finished when HA has generated/TTS_EOS'd
        # it: queued controller/device playback can run for tens of seconds.
        # This turn record is built only after playback completion, so use the
        # real end edge rather than the synthesis-only EOS edge.
        "tts_latency_ms": round((end - turn.tts_started_mono) * 1000)
        if turn.tts_started_mono else None,
        "tts_bytes": len(turn.tts_audio) if turn.tts_audio else None,
    }


async def _persist_audio(turn: Turn) -> None:
    if not turn.capture_audio:
        return
    loop = asyncio.get_running_loop()
    try:
        if turn.mic_audio:
            name = await loop.run_in_executor(
                None, lambda: em_recordings.save(
                    turn.device.device_id, turn.turn_id, bytes(turn.mic_audio), kind="stt"
                )
            )
            await loop.run_in_executor(None, db.set_turn_audio, turn.turn_id, name)
        if turn.tts_audio:
            name = await loop.run_in_executor(
                None, lambda: em_recordings.save(
                    turn.device.device_id, turn.turn_id, bytes(turn.tts_audio),
                    kind="tts", sample_rate=24_000,
                )
            )
            await loop.run_in_executor(None, db.set_turn_tts_audio, turn.turn_id, name)
    except Exception:
        # Recording is diagnostics, never part of whether a voice turn works.
        log.exception("[%s] failed to persist turn audio", turn.turn_id)


async def _remember_turn(device, turn_id: int) -> None:
    """
    Append the just-persisted turn to Device.turn_history — the in-memory
    cache em_controller.handle_control's playback_stats handler searches by
    turn_id, and what the Activity tab reads without waiting for a
    reconnect. db.get_turn keeps this the same shape db.get_turns()
    hydrates the deque with at connect time.
    """
    loop = asyncio.get_running_loop()
    rec = await loop.run_in_executor(None, db.get_turn, turn_id)
    if rec is not None:
        device.turn_history.append(rec)


async def _finish_created_turn(turn: Turn) -> None:
    try:
        result = await _run_turn(turn)
        outcome = _outcome_for(turn, result)
    except asyncio.TimeoutError:
        outcome = "audio_timeout"
    except Exception:
        log.exception("[%s] turn failed", turn.turn_id)
        outcome = "error"
    await asyncio.get_running_loop().run_in_executor(
        None, db.update_turn, turn.turn_id, _turn_record(turn, outcome)
    )
    await _persist_audio(turn)
    await _remember_turn(turn.device, turn.turn_id)
    await _push_event({
        "type": "turn.terminal", "device_id": turn.device.device_id,
        "turn_id": turn.turn_id, "outcome": outcome,
    })
    ENGINE.turns.pop(turn.turn_id, None)
    ENGINE.audio_sockets.pop(turn.turn_id, None)


async def trigger_voice_turn(
    device,
    on_thinking,
    post_turn_play,
    trigger_label: str = "unknown",
    preroll_discard: int = 0,
    initial_audio: tuple[bytes, ...] = (),
) -> bool:
    """Offer a controller-triggered turn to the connected HACS integration."""
    # Pop, not read: a continuation turn loops back into trigger_voice_turn
    # with no new detection, and must not inherit the original wake's score
    # (mirrors em_esphome.trigger_voice_turn's same pop).
    wake_info = device.last_wake or {}
    device.last_wake = None
    turn_id = await asyncio.get_running_loop().run_in_executor(
        None, db.create_turn, device.device_id, trigger_label
    )
    turn = Turn(
        turn_id, device, on_thinking, post_turn_play,
        preroll_remaining=preroll_discard,
        capture_audio=bool(getattr(device, "save_utterances", False)),
        initial_audio=initial_audio,
    )
    ENGINE.turns[turn_id] = turn
    await _push_event({
        "type": "wake.offer",
        "device_id": device.device_id,
        "turn_id": turn_id,
        "trigger": trigger_label,
    })
    wake_columns = {
        "wake_model":     wake_info.get("model"),
        "wake_score":     wake_info.get("score"),
        "wake_threshold": wake_info.get("threshold"),
        "noise_floor":    wake_info.get("noise_floor"),
    }
    try:
        result = await _run_turn(turn)
        outcome = _outcome_for(turn, result)
        await asyncio.get_running_loop().run_in_executor(
            None, db.update_turn, turn_id,
            {**_turn_record(turn, outcome), **wake_columns}
        )
        await _persist_audio(turn)
        await _remember_turn(device, turn_id)
        await _push_event({
            "type": "turn.terminal",
            "device_id": device.device_id,
            "turn_id": turn_id,
            "outcome": outcome,
        })
        return turn.continue_conversation
    except asyncio.TimeoutError:
        await asyncio.get_running_loop().run_in_executor(
            None, db.update_turn, turn_id,
            {**_turn_record(turn, "audio_timeout"), **wake_columns}
        )
        await _persist_audio(turn)
        await _remember_turn(device, turn_id)
        await _push_event({
            "type": "turn.terminal", "device_id": device.device_id,
            "turn_id": turn_id, "outcome": "audio_timeout",
        })
        return False
    finally:
        ENGINE.turns.pop(turn_id, None)
        ENGINE.audio_sockets.pop(turn_id, None)


def _end_turn(turn: Turn, reason: str, *, cancel: bool = True) -> None:
    """Record the first cause and optionally unblock local turn work."""
    if turn.end_reason is None:
        turn.end_reason = reason
    if cancel:
        turn.cancelled.set()
        turn.tts_queue.put_nowait(None)


def cancel_voice_turn(
    device_id: str, abort_ha: bool = False, reason: str = "cancelled"
) -> None:
    del abort_ha
    for turn in ENGINE.turns.values():
        if turn.device.device_id == device_id:
            _end_turn(turn, reason)


def abort_ha_run(device_id: str) -> None:
    # This cannot yet abort AssistSatelliteEntity's running pipeline. Preserve
    # that limitation while recording why the locally interrupted turn ended.
    for turn in ENGINE.turns.values():
        if turn.device.device_id == device_id:
            _end_turn(turn, "barged", cancel=False)


def start_device_test_turn(device) -> asyncio.Task:
    """Run a synthetic wake whose mic frames are streamed by the Echo."""
    if test_turn_active(device.device_id):
        raise RuntimeError("a test turn is already running")

    async def run() -> None:
        import em_controller

        device.oww_paused.set()
        device.last_wake = {
            "model": getattr(device, "oww_model", "test"),
            "score": 0.9,
            "threshold": 0.5,
            "noise_floor": getattr(device, "noise_floor", 0.0),
        }
        voice_task = None
        try:
            # Stop real room audio before _run_voice_locked drains its queues;
            # otherwise one last ambient frame can sit in front of the file.
            await device.mic_stop()
            voice_task = asyncio.create_task(
                em_controller._run_voice_locked(
                    device, trigger_label="test_audio", is_wakeword=False
                )
            )
            for _ in range(100):
                if device.listening or voice_task.done():
                    break
                await asyncio.sleep(0.05)
            if voice_task.done():
                await voice_task
                return
            # Ask the device to stream the temporary WAV onto the same /data
            # mic plane. From this point onward the controller cannot
            # distinguish this from a real utterance, which is precisely what
            # makes it an E2E test.
            await device.send_control({"type": "test_audio"})
            await voice_task
        finally:
            if voice_task is not None and not voice_task.done():
                voice_task.cancel()
                await asyncio.gather(voice_task, return_exceptions=True)
            # This lands after normal TTS playback/drain completes. Keeping
            # cleanup explicit gives the temporary file the lifecycle the UI
            # promises rather than deleting it as soon as its input is read.
            try:
                await device.send_control({"type": "test_audio_cleanup"})
            except Exception as exc:
                log.warning("[%s] test audio cleanup command failed: %s", device.device_id, exc)
            # _run_voice_locked stops the mic at turn end. Restore ordinary
            # wake listening even on conversion/read/socket failure.
            try:
                await device.beam_unlock()
            except Exception as exc:
                log.warning("[%s] test audio beam unlock failed: %s", device.device_id, exc)
            try:
                await device.mic_start()
            except Exception as exc:
                log.warning("[%s] test audio mic restart failed: %s", device.device_id, exc)
            finally:
                device.oww_paused.clear()
                device.last_wake = None
                TEST_TASKS.pop(device.device_id, None)

    task = asyncio.create_task(run(), name=f"test-turn-{device.device_id}")
    TEST_TASKS[device.device_id] = task
    return task
