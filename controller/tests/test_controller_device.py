import asyncio
import json
import sys
import types
import math

import pytest


def _install_import_stubs():
    zeroconf = types.ModuleType("zeroconf")
    zeroconf.ServiceInfo = type("ServiceInfo", (), {})
    zasync = types.ModuleType("zeroconf.asyncio")
    zasync.AsyncZeroconf = type("AsyncZeroconf", (), {})
    zeroconf.asyncio = zasync
    sys.modules.setdefault("zeroconf", zeroconf)
    sys.modules.setdefault("zeroconf.asyncio", zasync)

    websockets = types.ModuleType("websockets")
    websockets_async = types.ModuleType("websockets.asyncio")
    websockets_server = types.ModuleType("websockets.asyncio.server")
    websockets_server.ServerConnection = type("ServerConnection", (), {})
    websockets_async.server = websockets_server
    websockets.exceptions = types.SimpleNamespace(ConnectionClosed=Exception)
    websockets.asyncio = websockets_async
    sys.modules.setdefault("websockets", websockets)
    sys.modules.setdefault("websockets.asyncio", websockets_async)
    sys.modules.setdefault("websockets.asyncio.server", websockets_server)


_install_import_stubs()
import em_controller


class FakeWS:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def new_device(capabilities=None):
    return em_controller.Device("dev", "192.0.2.1", capabilities or [], FakeWS())


def test_device_wake_admission_rejects_reserved_device(monkeypatch):
    device = new_device(["wake_request_v1"])
    device.wake_request_id = "existing"
    sent = []
    device.send_control = lambda message: asyncio.sleep(0, result=sent.append(message))

    asyncio.run(em_controller._handle_wake_request(device, {
        "requestId": "new", "score": 0.8, "threshold": 0.5,
        "ageMs": 1, "activationSeq": 1,
    }))

    assert sent == [{"type": "wake_deny", "requestId": "new", "reason": "busy"}]
    assert device.wake_request_id == "existing"


def test_device_stop_generation_seeded_above_a_stale_device_counter():
    """
    Regression test: a fresh Device object (created on every controller
    restart, since it is not persisted) used to seed stop_generation at 0.
    The device's own stopword.Manager generation counter is long-lived
    across control-plane reconnects, so a controller restarted mid-fleet-
    operation would send generation 1, 2, 3... while the device still
    remembered a much higher generation from before the restart, and the
    device's monotonic check rejected every arm as "invalid arm" until the
    controller's counter organically climbed back past it. Seeding from wall
    clock instead puts the starting value far above anything a real device
    session could have accumulated.
    """
    now_ms = em_controller.time.time() * 1000
    device = new_device(["wake_request_v1", "stopword"])

    # Comfortably above any generation a real device could have reached
    # advancing by 1-2 per turn — not just above 0 — and within a second of
    # actual wall-clock time (int() truncates, so allow for that).
    assert device.stop_generation > 1_000_000
    assert abs(device.stop_generation - now_ms) < 1000


def test_wake_request_admission_gate_lets_barge_through_before_busy_check():
    """
    Regression test for a real, previously-shipped bug: the voice_lock/barge
    check must run BEFORE the ordinary busy check. device.wake_request_id
    stays set for an admitted turn's entire active duration (cleared only
    once trigger_voice_turn returns, not just during the admission
    handshake), so it is non-None for the whole window a device-originated
    barge attempt can arrive in. Checking busy first denied every barge
    request unconditionally — barge-in could not admit under any
    configuration — and no test caught it because every existing
    _handle_wake_request test pre-set wake_request_id to match the incoming
    id, which is exactly what establishing this gate correctly is for.
    """
    device = new_device(["wake_request_v1"])
    device.wake_request_id = "original-turn-request"  # the turn already in progress
    device.barge_in_enabled = True

    async def run():
        async with device.voice_lock:
            return em_controller._wake_request_admission_gate(device)

    assert asyncio.run(run()) is None


def test_wake_request_admission_gate_denies_barge_when_disabled():
    device = new_device(["wake_request_v1"])
    device.wake_request_id = "original-turn-request"
    device.barge_in_enabled = False

    async def run():
        async with device.voice_lock:
            return em_controller._wake_request_admission_gate(device)

    assert asyncio.run(run()) == "barge_disabled"


def test_wake_request_admission_gate_denies_ordinary_busy_when_not_mid_turn():
    device = new_device(["wake_request_v1"])
    device.wake_request_id = "existing"
    assert em_controller._wake_request_admission_gate(device) == "busy"


def test_wake_request_admission_gate_allows_fresh_request():
    device = new_device(["wake_request_v1"])
    assert em_controller._wake_request_admission_gate(device) is None


def test_stop_detected_cancels_turn_without_killing_the_handler(monkeypatch):
    """
    Regression test for a real, previously-shipped bug (5.3 in the
    device-only wake word design doc's hardware checklist): the accepted
    branch of the stop_detected handler counted the stop with

        await loop.run_in_executor(
            None, db.bump_wake_counters, device_id, stops_accepted=1)

    run_in_executor takes POSITIONAL args only — the kwargs call raised
    TypeError inside the control-plane dispatch loop. The exception was
    swallowed by handle_control's blanket handler, which then tore the
    device's control/data connections down. The stop itself never ran
    (stop_voice_turn is called BEFORE the counter bump, but the TypeError
    aborted the whole handler task, and on hardware the observed behaviour
    was: TTS did not stop, the device disconnected, and it reconnected a
    few seconds later with the turn still running).

    This test drives the real stop_detected handler end to end — real
    StopState arming, real run_in_executor hop — and asserts BOTH that the
    turn is cancelled AND that handle_control returns normally instead of
    raising.
    """
    import em_turn_engine

    stopped = []

    async def fake_stop_voice_turn(device_id, turn_id, detection=None):
        stopped.append((device_id, turn_id))
        return True

    async def no_op(*args, **kwargs):
        return None

    class WS:
        """Register, report the stop model ready, then detect a stop.

        The arm cannot be prepared up front: handle_control constructs a
        FRESH Device from the register handshake, so the armed StopState
        must live on that object. Arming lazily while producing the
        stop_detected message guarantees register/stop_status have been
        processed first (messages are consumed one at a time).
        """
        remote_address = ("192.0.2.13", 8767)

        def __init__(self, device):
            self.sent = []
            self.closed = False
            self._device = device
            self._messages = self._stream()

        def _stream(self):
            """Yield register, stop_status, then an armed stop_detected.

            The arm cannot be prepared up front: handle_control constructs a
            FRESH Device from the register handshake, so the armed StopState
            must live on that object. A generator guarantees register/
            stop_status are consumed before the stop_detected message is
            BUILT, so the live device exists in _devices by then.
            """
            yield json.dumps({
                "type": "register", "device_id": self._device.device_id,
                "ip": "192.0.2.13", "version": "test",
                "capabilities": ["wake_request_v1", "stopword"],
            })
            yield json.dumps({
                "type": "stop_status", "model": "stop", "ready": True,
            })
            live = em_controller._devices.get(self._device.device_id)
            assert live is not None, "register must have stored the device"
            live.stop_generation += 1
            generation = live.stop_generation
            loop = asyncio.get_event_loop()
            armed = live.stop_state.arm(
                501, generation, "playback", loop.time() + 135.0,
            )
            assert armed.action == "armed"
            yield json.dumps({
                "type": "stop_detected", "turnId": 501,
                "generation": generation,
                "score": 0.9, "threshold": 0.75, "ageMs": 120,
                "phase": "playback",
            })

        async def recv(self):
            return next(self._messages)

        async def send(self, value):
            self.sent.append(json.loads(value))

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                raise StopAsyncIteration

    async def run():
        device = new_device(["wake_request_v1", "stopword"])
        old_devices = em_controller._devices
        em_controller._devices = {device.device_id: device}
        old_engine = em_turn_engine.ENGINE
        em_turn_engine.ENGINE = em_turn_engine.TurnEngine()
        monkeypatch.setattr(em_controller.db, "get_config", lambda *args: "strict")
        monkeypatch.setattr(em_controller.db, "bump_wake_counters",
                            lambda *args, **kwargs: None)
        monkeypatch.setattr(em_controller.turn_engine, "stop_voice_turn",
                            fake_stop_voice_turn)
        monkeypatch.setattr(em_controller.turn_engine, "_push_event", no_op)
        monkeypatch.setattr(em_controller, "_link_auth_ok",
                            lambda *args: asyncio.sleep(0, result=True))
        monkeypatch.setattr(em_controller.db, "get_device",
                            lambda *args: {"label": "Test", "approved": 1,
                                           "firmware_ver": "v1"})
        monkeypatch.setattr(em_controller.db, "get_turns", lambda *args: [])
        monkeypatch.setattr(em_controller.db, "get_effective_device_config",
                            lambda *args: {})
        monkeypatch.setattr(em_controller.db, "get_device_config",
                            lambda *args: {})
        monkeypatch.setattr(em_controller.db, "set_device_config",
                            lambda *args: None)
        monkeypatch.setattr(em_controller.db, "record_device_stats",
                            lambda *args: None)
        monkeypatch.setattr(em_controller.db, "touch_device_seen",
                            lambda *args: None)
        monkeypatch.setattr(em_controller.db, "upsert_device_seen",
                            lambda *args: None)
        monkeypatch.setattr(em_controller.db, "log_device",
                            lambda *args: None)
        monkeypatch.setattr(em_controller.db, "set_turn_playback",
                            lambda *args: None)
        monkeypatch.setattr(em_controller.api, "_push_event", no_op)
        monkeypatch.setattr(em_controller.api, "_push_log_event", no_op)
        monkeypatch.setattr(em_controller.api, "wifi_record_result",
                            lambda *args: ({"pending": None}, False))
        monkeypatch.setattr(em_controller.api, "notify_device_connected", no_op)
        monkeypatch.setattr(em_controller.api, "notify_device_disconnected", no_op)
        monkeypatch.setattr(em_controller.api, "reconcile_oww_assets", no_op)
        monkeypatch.setattr(em_controller.em_player, "device_gone",
                            lambda *args: None)
        monkeypatch.setattr(em_controller, "leds_off", no_op)
        monkeypatch.setattr(em_controller, "_push_device_state", no_op)
        monkeypatch.setattr(em_controller.ha_sidechannels, "capabilities",
                            lambda *args: None)
        try:
            # stop_voice_turn matches the turn by device_id only, so the
            # Turn may hold the pre-register object.
            turn = em_turn_engine.Turn(501, device, None, None)
            em_turn_engine.ENGINE.turns[501] = turn

            ws = WS(device)
            # The old code raised TypeError HERE (swallowed by
            # handle_control's blanket except, which then closed the
            # connection) — so assert the turn cancelled AND the loop
            # survived to ack the register.
            await em_controller.handle_control(ws)
            assert stopped == [(device.device_id, 501)]
            assert any(m.get("type") == "ack" for m in ws.sent)
        finally:
            em_controller._devices = old_devices
            em_turn_engine.ENGINE = old_engine

    asyncio.run(run())


def test_device_wake_admission_arbitrates_and_releases(monkeypatch):
    capabilities = ["wake_request_v1", "stopword"]
    winner = new_device(capabilities)
    loser = new_device(capabilities)
    loser.device_id = "loser"
    for device in (winner, loser):
        device.oww_model_ready = True
        device.data_ws = object()
        device.stop_model_ready = True
        device.wake_arb_ms = 300
    sent = []
    for device in (winner, loser):
        device.send_control = lambda message, out=sent: asyncio.sleep(0, result=out.append(message))

    old_devices = em_controller._devices
    old_arbiter = em_controller._wake_arbiter
    em_controller._devices = {winner.device_id: winner, loser.device_id: loser}
    em_controller._wake_arbiter = em_controller.em_arbiter.WakeArbiter()
    try:
        async def claim_winner():
            em_controller._wake_arbiter.claim(winner.device_id, 0.3)
        asyncio.run(claim_winner())
        loser.wake_request_id = "loser"
        asyncio.run(em_controller._handle_wake_request(loser, {
            "requestId": "loser", "score": 0.8, "threshold": 0.5,
            "ageMs": 1, "activationSeq": 2, "source": "wakeword",
            "model": loser.oww_model,
        }))
    finally:
        em_controller._devices = old_devices
        em_controller._wake_arbiter = old_arbiter

    assert {message["reason"] for message in sent if message["type"] == "wake_deny"} == {"arbitration"}
    assert loser.wake_request_id is None
    em_controller._wake_arbiter.release(winner.device_id)


def test_ordinary_wake_request_starts_admission_without_idle_pcm(monkeypatch):
    async def run():
        device = new_device(["wake_request_v1", "stopword"])
        device.oww_model_ready = True
        device.stop_model_ready = True
        device.data_ws = object()
        device.wake_request_id = "wake:1"
        called = []

        async def voice_turn(*args, **kwargs):
            called.append(kwargs)
            assert device.voice_queue.empty()
            return False

        monkeypatch.setattr(em_controller, "_run_voice_locked", voice_turn)
        old_devices = em_controller._devices
        em_controller._devices = {device.device_id: device}
        try:
            await em_controller._handle_wake_request(device, {
                "requestId": "wake:1", "source": "wakeword",
                "model": device.oww_model, "score": 0.8, "threshold": 0.5,
                "ageMs": 1, "activationSeq": 20,
            })
        finally:
            em_controller._devices = old_devices
        assert called and called[0]["request_id"] == "wake:1"

    asyncio.run(run())


def test_wake_status_requires_selected_model_and_matching_classifier(monkeypatch):
    import em_oww_assets

    device = new_device(["wake_request_v1"])
    device.oww_model = "selected"
    monkeypatch.setattr(em_oww_assets, "classifier_source", lambda model: "/model.onnx")
    monkeypatch.setattr(em_oww_assets, "md5_file", lambda path: "expected")

    assert em_controller._wake_status_ready(device, {
        "ready": True, "model": "selected", "classifierMd5": "expected",
    })
    assert not em_controller._wake_status_ready(device, {
        "ready": True, "model": "other", "classifierMd5": "expected",
    })
    assert not em_controller._wake_status_ready(device, {
        "ready": True, "model": "selected", "classifierMd5": "stale",
    })
    assert not em_controller._wake_status_ready(device, {
        "ready": False, "model": "selected", "classifierMd5": "expected",
    })


def test_wake_request_past_device_deadline_is_denied_as_stale():
    device = new_device(["wake_request_v1", "stopword"])
    device.oww_model_ready = True
    device.stop_model_ready = True
    device.data_ws = object()
    device.wake_request_id = "wake:stale"
    sent = []
    device.send_control = lambda message: asyncio.sleep(0, result=sent.append(message))

    asyncio.run(em_controller._handle_wake_request(device, {
        "requestId": "wake:stale", "source": "wakeword",
        "model": device.oww_model, "score": 0.8, "threshold": 0.5,
        "ageMs": 4001, "activationSeq": 20,
    }))

    assert sent == [{
        "type": "wake_deny", "requestId": "wake:stale", "reason": "stale",
    }]


def test_capture_upload_requires_live_privacy_model_and_checksum_before_ack(monkeypatch):
    async def run():
        device = new_device(["wake_request_v1"])
        ws = object()
        device.data_ws = ws
        device.save_wake_captures = True
        device.oww_model = "wake"
        device.oww_classifier_md5 = "expected"
        sent = []
        saved = []
        discarded = []
        device.send_control = lambda message: asyncio.sleep(0, result=sent.append(message))
        monkeypatch.setattr(
            em_controller.em_training_captures, "save_uploaded",
            lambda *args, **kwargs: saved.append((args, kwargs))
            or ("capture.wav", False),
        )
        monkeypatch.setattr(
            em_controller.em_training_captures, "discard",
            lambda *args: discarded.append(args) or True,
        )
        old_devices = em_controller._devices
        em_controller._devices = {device.device_id: device}
        try:
            completed = types.SimpleNamespace(
                metadata={"captureId": "cap:1", "model": "wake", "classifierMd5": "expected"},
                pcm=b"pcm",
            )
            assert await em_controller._accept_capture_upload(device, ws, completed)
            assert await em_controller._accept_capture_upload(device, ws, completed)
            assert sent == [
                {"type": "capture_ack", "captureId": "cap:1"},
                {"type": "capture_ack", "captureId": "cap:1"},
            ]
            assert len(saved) == 2

            sent.clear()
            device.save_wake_captures = False
            assert not await em_controller._accept_capture_upload(device, ws, completed)
            device.save_wake_captures = True
            completed.metadata["classifierMd5"] = "wrong"
            assert not await em_controller._accept_capture_upload(device, ws, completed)
            completed.metadata["classifierMd5"] = "expected"
            device.data_ws = object()
            assert not await em_controller._accept_capture_upload(device, ws, completed)
            assert sent == [] and len(saved) == 2

            device.data_ws = ws
            device.save_wake_captures = True

            def disable_during_save(*args, **kwargs):
                device.save_wake_captures = False
                return "capture.wav", True

            monkeypatch.setattr(
                em_controller.em_training_captures,
                "save_uploaded",
                disable_during_save,
            )
            assert not await em_controller._accept_capture_upload(
                device, ws, completed
            )
            assert discarded == [("wake", "capture.wav")]

            device.save_wake_captures = True
            device.data_ws = ws

            def replace_socket_during_save(*args, **kwargs):
                device.data_ws = object()
                return "capture-from-old-socket.wav", True

            monkeypatch.setattr(
                em_controller.em_training_captures,
                "save_uploaded",
                replace_socket_during_save,
            )
            assert not await em_controller._accept_capture_upload(
                device, ws, completed
            )
            assert discarded == [("wake", "capture.wav")]

            device.save_wake_captures = True
            device.data_ws = ws

            def disable_during_duplicate(*args, **kwargs):
                device.save_wake_captures = False
                return "existing.wav", False

            monkeypatch.setattr(
                em_controller.em_training_captures,
                "save_uploaded",
                disable_during_duplicate,
            )
            assert not await em_controller._accept_capture_upload(
                device, ws, completed
            )
            assert discarded == [("wake", "capture.wav")]
        finally:
            em_controller._devices = old_devices

    asyncio.run(run())


def test_capabilities_and_rtt_aggregation():
    device = new_device(["led_anim", "audio_mix", "button_hold", "wake_request_v1", "sendspin_native", "output_chain"])
    assert device.led_anim_capable
    assert device.audio_mix_capable
    assert device.button_hold_capable
    assert device.wake_request_capable
    assert device.sendspin_native_capable
    assert device.output_chain_capable
    empty = new_device()
    assert not empty.led_anim_capable and not empty.wake_request_capable
    assert not empty.sendspin_native_capable
    assert not empty.output_chain_capable

    assert device.drain_rtt() == {}
    device.record_rtt(50, False)
    device.record_rtt(250, True)
    device.record_rtt(300, False)
    result = device.drain_rtt()
    assert result == {
        "rttSumMs": 600,
        "rttSamples": 3,
        "rttMinMs": 50,
        "rttMaxMs": 300,
        "rttExcursions": 2,
        "rttExcursionsIdle": 1,
        "rttSamplesIdle": 2,
    }
    assert device.drain_rtt() == {}


def test_control_messages_and_listening_field():
    async def run():
        device = new_device()
        device.oww_paused.set()
        await device.set_leds([{"id": 1}], listening=True)
        await device.send_led_anim({"pattern": "off"})
        await device.ping()
        await device.mic_start()
        await device.mic_start_turn()
        await device.mic_stop()
        await device.beam_lock()
        await device.beam_unlock()
        await device.push_config(owwThreshold=0.4)
        messages = [json.loads(value) for value in device.control_ws.messages]
        assert messages[0] == {"type": "leds", "leds": [{"id": 1}], "listening": True}
        assert messages[-1] == {"type": "config", "owwThreshold": 0.4}
        await device.set_leds([], listening=None)
        assert "listening" not in json.loads(device.control_ws.messages[-1])

    asyncio.run(run())


def test_send_control_and_data_swallow_disconnects_and_spend_one_budget():
    class Broken:
        async def send(self, _message):
            raise RuntimeError("gone")

    async def run():
        device = new_device()
        device.control_ws = Broken()
        await device.send_control({"type": "x"})
        device.begin_data_stream()
        sleeps = []

        async def sleep(step):
            sleeps.append(step)

        original_sleep = em_controller.asyncio.sleep
        em_controller.asyncio.sleep = sleep
        try:
            await device.send_data(b"one")
            remaining = device._data_grace_left
            await device.send_data(b"two")
        finally:
            em_controller.asyncio.sleep = original_sleep
        assert sleeps
        assert math.isclose(remaining, 0, abs_tol=1e-9)
        assert math.isclose(device._data_grace_left, 0, abs_tol=1e-9)

    asyncio.run(run())


def test_send_data_sends_when_connected_and_is_busy(monkeypatch):
    async def run():
        device = new_device()
        data = FakeWS()
        device.data_ws = data
        await device.send_data(b"pcm")
        assert data.messages == [b"pcm"]
        monkeypatch.setattr(em_controller.em_player, "is_playing", lambda device_id: True)
        assert device.is_busy()
        device.speaking = True
        assert device.is_busy()

    asyncio.run(run())


def test_speaking_state_is_assigned_even_when_dashboard_push_fails(monkeypatch):
    pushes = []

    async def push(device_id):
        pushes.append(device_id)
        raise asyncio.CancelledError()

    monkeypatch.setattr(em_controller, "_push_device_state", push)

    async def run():
        device = new_device()
        await device._set_speaking(True)
        assert device.speaking is True
        await device._set_speaking(True)
        device.thinking = True
        await device._set_speaking(False)
        assert device.speaking is False
        assert pushes == [device, device]

    asyncio.run(run())


def test_dashboard_state_and_led_helpers(monkeypatch):
    async def run():
        device = new_device(["led_anim"])
        events = []
        monkeypatch.setattr(em_controller.api, "_push_event", lambda event: asyncio.sleep(0, result=events.append(event)))
        await em_controller._push_device_state(device)
        assert events[0]["state"]["connected"] is True
        assert events[0]["state"]["timer_firing"] is False
        device.timer_firing = True
        await em_controller._push_device_state(device)
        assert events[1]["state"]["timer_firing"] is True
        assert len(em_controller._make_leds(1, 2, 3)) == em_controller.NUM_LEDS

        animations = []
        device.send_led_anim = lambda value: asyncio.sleep(0, result=animations.append(value))
        await em_controller.leds_off(device)
        await em_controller.leds_listening(device)
        device.last_turn_outcome = "no_speech"
        device.led_scene["nospeech_anim"] = {"pattern": "pulse"}
        await em_controller._leds_turn_end(device)
        assert animations[0] == {"pattern": "off"}
        assert animations[1] == device.led_scene["listening_anim"]
        assert animations[2] == {"pattern": "pulse"}
        assert device.last_turn_outcome is None

        legacy = new_device()
        frames = []
        legacy.set_leds = lambda *value, **kwargs: asyncio.sleep(0, result=frames.append((value, kwargs)))
        await em_controller.leds_off(legacy)
        await em_controller.leds_listening(legacy)
        assert len(frames) == 2
        assert frames[1][1] == {"listening": True}

    asyncio.run(run())


def test_timer_speech_dismissal_waits_for_stt_and_does_not_generate_response(monkeypatch):
    device = new_device()
    dismissed = []

    async def dismiss(device_id):
        dismissed.append(device_id)
        return True

    async def voice_turn(_device, **kwargs):
        assert kwargs["trigger_label"] == "timer-speech"
        assert kwargs["stt_only"] is True
        assert kwargs["initial_audio"] == (b"first",)
        await kwargs["on_transcript"]("stop")

    monkeypatch.setattr(em_controller.api, "dismiss_timer_alarm", dismiss)
    monkeypatch.setattr(em_controller, "_run_voice_locked", voice_turn)
    device.beam_lock = lambda: asyncio.sleep(0)
    device.beam_unlock = lambda: asyncio.sleep(0)

    asyncio.run(em_controller._run_timer_speech_turn(device, b"first"))
    assert dismissed == [device.device_id]


def test_timer_speech_without_stt_transcript_keeps_alarm_running(monkeypatch):
    device = new_device()
    dismissed = []

    async def dismiss(_device_id):
        dismissed.append(True)
        return True

    async def voice_turn(_device, **kwargs):
        return None

    monkeypatch.setattr(em_controller.api, "dismiss_timer_alarm", dismiss)
    monkeypatch.setattr(em_controller, "_run_voice_locked", voice_turn)
    device.beam_lock = lambda: asyncio.sleep(0)
    device.beam_unlock = lambda: asyncio.sleep(0)

    asyncio.run(em_controller._run_timer_speech_turn(device, b"first"))
    assert dismissed == []


def test_timer_button_tap_dismisses_locally_without_voice_turn(monkeypatch):
    device = new_device()
    dismissed = []
    sent = []
    device.send_control = lambda message: asyncio.sleep(0, result=sent.append(message))

    async def dismiss(device_id):
        dismissed.append(device_id)
        return True

    monkeypatch.setattr(em_controller.api, "timer_alarm_ringing", lambda _id: True)
    monkeypatch.setattr(em_controller.api, "dismiss_timer_alarm", dismiss)
    monkeypatch.setattr(
        em_controller, "_run_voice_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("timer tap must not start an Assist turn")
        ),
    )

    asyncio.run(em_controller.handle_button_event(
        device, {"clickType": 138, "down": False, "heldMs": 100,
                 "muted": False, "requestId": "button:timer"}
    ))
    assert dismissed == [device.device_id]
    assert sent == [{
        "type": "wake_deny", "requestId": "button:timer",
        "reason": "timer_dismissed",
    }]


def test_button_voice_turn_uses_correlated_admission_without_wake_readiness(monkeypatch):
    async def run():
        device = new_device(["wake_request_v1", "stopword"])
        device.stop_model_ready = True
        device.oww_model_ready = False
        device.data_ws = object()
        called = asyncio.Event()
        kwargs_seen = {}

        async def voice_turn(*args, **kwargs):
            kwargs_seen.update(kwargs)
            called.set()
            return False

        monkeypatch.setattr(em_controller.api, "timer_alarm_ringing", lambda _id: False)
        monkeypatch.setattr(em_controller, "_run_voice_locked", voice_turn)
        await em_controller.handle_button_event(device, {
            "clickType": 138, "down": False, "heldMs": 100,
            "muted": False, "requestId": "button:1",
        })
        await asyncio.wait_for(called.wait(), 1.0)
        assert kwargs_seen["request_id"] == "button:1"
        assert device.oww_model_ready is False

    asyncio.run(run())


def test_timer_button_hold_remains_an_ha_event(monkeypatch):
    device = new_device(["button_hold"])
    events = []
    monkeypatch.setattr(em_controller.api, "timer_alarm_ringing", lambda _id: True)
    monkeypatch.setattr(
        em_controller.ha_sidechannels, "button_event",
        lambda *args: events.append(args),
    )

    asyncio.run(em_controller.handle_button_event(
        device, {"clickType": 138, "down": False, "heldMs": 800, "muted": False}
    ))
    assert events == [(device.device_id, "long", 800)]


def test_legacy_spinner_stops_and_cleans_up():
    async def run():
        device = new_device()
        calls = []
        device.set_leds = lambda *value, **kwargs: asyncio.sleep(0, result=calls.append(value))
        stop = asyncio.Event()
        stop.set()
        await em_controller.leds_spin_green(device, stop)
        assert len(calls) == 1

    asyncio.run(run())


def test_stream_speaker_periods_padding_and_eos():
    async def run():
        device = new_device()
        frames = []
        device.send_data = lambda frame: asyncio.sleep(0, result=frames.append(frame))
        device._set_speaking = lambda value: asyncio.sleep(0)
        pcm = b"\x01" * (em_controller.SPEAKER_BYTES + 3)
        await device.stream_speaker(pcm)
        assert frames[0][0] == em_controller.SPEAKER_FRAME_TYPE
        assert len(frames[0]) == em_controller.SPEAKER_BYTES + 1
        assert len(frames[1]) == em_controller.SPEAKER_BYTES + 1
        assert frames[-1] == bytes([em_controller.SPEAKER_EOS_TYPE])

    asyncio.run(run())


def test_stream_speaker_chunks_preserves_partial_data_and_reports_metrics():
    class EQ:
        def __init__(self):
            self.calls = []

        def process(self, pcm):
            self.calls.append(pcm)
            return pcm

    async def chunks():
        yield b"a" * 100
        yield b"b" * (em_controller.SPEAKER_BYTES - 100 + 5)

    async def run():
        device = new_device()
        frames = []
        device.send_data = lambda frame: asyncio.sleep(0, result=frames.append(frame))
        device._set_speaking = lambda value: asyncio.sleep(0)
        eq = EQ()
        total, eq_ms, first, send_ms = await device.stream_speaker_chunks(chunks(), eq)
        assert total == em_controller.SPEAKER_BYTES + 5
        assert eq.calls == [b"a" * 100, b"b" * (em_controller.SPEAKER_BYTES - 95)]
        assert first is not None and eq_ms >= 0 and send_ms >= 0
        assert len(frames) == 3
        assert frames[-1] == bytes([em_controller.SPEAKER_EOS_TYPE])

    asyncio.run(run())


def test_stream_speaker_chunks_without_dsp_preserves_pcm_and_reports_zero_eq_time():
    async def chunks():
        yield b"x" * em_controller.SPEAKER_BYTES

    async def run():
        device = new_device(["output_chain"])
        frames = []
        device.send_data = lambda frame: asyncio.sleep(0, result=frames.append(frame))
        device._set_speaking = lambda value: asyncio.sleep(0)

        total, eq_ms, _first, _send_ms = await device.stream_speaker_chunks(chunks())

        assert total == em_controller.SPEAKER_BYTES
        assert eq_ms == 0
        assert frames[0] == bytes([em_controller.SPEAKER_FRAME_TYPE]) + b"x" * em_controller.SPEAKER_BYTES

    asyncio.run(run())


def test_button_handler_routes_hold_tap_mute_and_cancel(monkeypatch):
    events = []
    monkeypatch.setattr(em_controller.ha_sidechannels, "button_event", lambda *args: events.append(args))
    monkeypatch.setattr(
        em_controller.turn_engine, "cancel_voice_turn",
        lambda device_id, **kwargs: events.append(("cancel", device_id, kwargs)),
    )

    async def run():
        device = new_device(["button_hold"])
        device.button_single_tap_event = True
        device.button_multi_tap_ms = 0
        await em_controller.handle_button_event(device, {"clickType": 138, "down": True})
        await em_controller.handle_button_event(device, {"clickType": 138, "down": False, "heldMs": 900})
        await em_controller.handle_button_event(device, {"clickType": 138, "down": False, "heldMs": 100})
        await em_controller.handle_button_event(device, {"clickType": 138, "down": False, "heldMs": 100, "muted": True})
        assert events[:2] == [("dev", "long", 900), ("dev", "single")]

        device.voice_lock = asyncio.Lock()
        await device.voice_lock.acquire()
        device.button_single_tap_event = False
        device.send_control = lambda message: asyncio.sleep(0, result=events.append(message))
        await em_controller.handle_button_event(device, {"clickType": 138, "down": False, "heldMs": 100})
        assert device.cancel_event.is_set()
        assert ("cancel", "dev", {"reason": "cancelled"}) in events
        assert {"type": "speaker_flush"} in events
        device.voice_lock.release()

    asyncio.run(run())


def test_control_handler_rejects_bad_first_message_and_holds_unknown_device(monkeypatch):
    class WS:
        remote_address = ("192.0.2.9", 8767)

        def __init__(self, messages):
            self.incoming = iter(messages)
            self.sent = []
            self.closed = False

        async def recv(self):
            return next(self.incoming)

        async def send(self, value):
            self.sent.append(json.loads(value))

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def run():
        bad = WS([json.dumps({"type": "not_register"})])
        await em_controller.handle_control(bad)
        assert bad.closed and bad.sent == []

        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        monkeypatch.setattr(em_controller.db, "get_config", lambda *args: "strict")
        monkeypatch.setattr(em_controller.db, "get_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "register_new_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "log_device", lambda *args: None)
        monkeypatch.setattr(em_controller.api, "notify_device_pending", lambda *args: asyncio.sleep(0))
        pending = WS([json.dumps({"type": "register", "device_id": "new", "ip": "192.0.2.9"})])
        await em_controller.handle_control(pending)
        assert pending.sent == [{"type": "pending"}]
        assert pending.closed

    asyncio.run(run())


def test_control_handler_processes_device_state_messages(monkeypatch):
    class WS(FakeWS):
        remote_address = ("192.0.2.10", 8767)

        def __init__(self, first, messages):
            self.sent = []
            self.first = first
            self.messages = iter(messages)
            self.closed = False

        async def send(self, message):
            self.sent.append(message)

        async def recv(self):
            return self.first

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

        async def close(self):
            self.closed = True

    async def no_op(*args, **kwargs):
        return None

    row = {"label": "Kitchen", "approved": 1, "firmware_ver": "v1"}
    config = {"startupVolume": 80,
              "owwModel": "hey_jarvis_v0.1", "bleProxyEnabled": True}
    messages = [
        json.dumps({"type": "ambient_light", "lux": 12}),
        json.dumps({"type": "mute_state", "muted": True}),
        json.dumps({"type": "volume_state", "level": 90}),
        json.dumps({"type": "stats", "cpuPct": 5, "ambientLux": 12,
                    "wakeDetector": {"frames": 3, "drops": 1, "crossings": 1,
                                     "maxScore": 0.8, "threshold": 0.3}}),
        json.dumps({"type": "wifi_result", "ok": True, "ssid": "Home"}),
        json.dumps({"type": "playback_stats", "periods": 4, "underruns": 1,
                    "stats": {"min_depth": 2}}),
        json.dumps({"type": "ble_adverts", "adverts": [{"address": "x"}]}),
        json.dumps({"type": "wifi_scan_result", "networks": []}),
        json.dumps({"type": "log", "level": "info", "message": "hello"}),
        json.dumps({"type": "pong"}),
        json.dumps({"type": "unknown"}),
    ]

    async def run():
        old_devices = em_controller._devices
        em_controller._devices = {}
        em_controller.websockets.exceptions = types.SimpleNamespace(ConnectionClosed=Exception)
        ws = WS(json.dumps({"type": "register", "device_id": "dev",
                            "ip": "192.0.2.10", "capabilities": ["oww_shadow"]}), messages)
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        monkeypatch.setattr(em_controller.db, "get_config", lambda *args: "strict")
        monkeypatch.setattr(em_controller.db, "get_device", lambda *args: row)
        monkeypatch.setattr(em_controller.db, "get_turns", lambda *args: [])
        monkeypatch.setattr(em_controller.db, "get_effective_device_config", lambda *args: config)
        monkeypatch.setattr(em_controller.db, "get_device_config", lambda *args: {})
        monkeypatch.setattr(em_controller.db, "set_device_config", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "record_device_stats", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "touch_device_seen", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "bump_wake_counters", lambda *args, **kwargs: None)
        monkeypatch.setattr(em_controller.db, "upsert_device_seen", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "log_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "set_turn_playback", lambda *args: None)
        monkeypatch.setattr(em_controller.api, "_push_event", no_op)
        monkeypatch.setattr(em_controller.api, "_push_log_event", no_op)
        monkeypatch.setattr(em_controller.api, "wifi_record_result", lambda *args: ({"pending": None}, False))
        monkeypatch.setattr(em_controller.api, "notify_device_connected", no_op)
        monkeypatch.setattr(em_controller.api, "notify_device_disconnected", no_op)
        monkeypatch.setattr(em_controller.em_player, "device_gone", lambda *args: None)
        monkeypatch.setattr(em_controller, "leds_off", no_op)
        monkeypatch.setattr(em_controller.ha_sidechannels, "ambient_light", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "mute_state", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "volume", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "capabilities", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "wake_model", lambda *args: None)
        monkeypatch.setattr(em_controller.ha_sidechannels, "ble_adverts", lambda *args: None)
        try:
            await em_controller.handle_control(ws)
            assert ws.closed is False
            assert any(json.loads(value)["type"] == "ack" for value in ws.sent)
        finally:
            em_controller._devices = old_devices

    asyncio.run(run())


def test_data_handler_routes_valid_audio_and_vad_sentinel(monkeypatch):
    class DataWS:
        remote_address = ("192.0.2.11", 8767)

        def __init__(self, frames):
            self.frames = iter(frames)
            self.closed = False

        async def recv(self):
            return json.dumps({"type": "identify", "device_id": "dev"})

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.frames)
            except StopIteration:
                raise StopAsyncIteration

    async def run():
        device = new_device()
        device.oww_paused.set()
        old_devices = em_controller._devices
        em_controller._devices = {"dev": device}
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        ws = DataWS([
            "not binary",
            b"\x01",
            b"\x02bad",
            bytes([em_controller.MIC_FRAME_TYPE, 0, 0]) + b"audio",
            bytes([em_controller.MIC_FRAME_TYPE, 0, 0, em_controller.VAD_END_TYPE]),
        ])
        try:
            await em_controller.handle_data(ws)
            assert device.voice_queue.get_nowait() == b"audio"
            assert device.voice_queue.get_nowait() == em_controller.turn_engine.VAD_SENTINEL_END
            assert device.data_ws is None
            assert not device.data_ready.is_set()
        finally:
            em_controller._devices = old_devices

    asyncio.run(run())


def test_router_and_shell_handler_reject_invalid_sessions(monkeypatch):
    class WS:
        remote_address = ("192.0.2.12", 8767)

        def __init__(self, path="/"):
            self.path = path
            self.request = types.SimpleNamespace(path=path)
            self.closed = False

        async def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    async def run():
        missing = WS("/shell/")
        await em_controller.handle_shell(missing, "/shell/")
        assert missing.closed

        denied = WS("/shell/dev")
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=False))
        await em_controller.handle_shell(denied, "/shell/dev?pty=1")
        assert denied.closed

        no_pending = WS("/shell/dev")
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        em_controller._shell_pending.pop("dev", None)
        await em_controller.handle_shell(no_pending, "/shell/dev")
        assert no_pending.closed

        unknown = WS("/other")
        await em_controller._route(unknown, False)
        assert unknown.closed

    asyncio.run(run())


def test_control_and_data_handlers_cover_registration_failures(monkeypatch):
    class WS:
        remote_address = ("192.0.2.13", 8767)

        def __init__(self, raw=None):
            self.raw = raw
            self.closed = False

        async def recv(self):
            if isinstance(self.raw, BaseException):
                raise self.raw
            return self.raw

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def run():
        em_controller.websockets.exceptions = types.SimpleNamespace(ConnectionClosed=Exception)
        timeout_ws = WS(asyncio.TimeoutError())
        await em_controller.handle_control(timeout_ws)
        assert not timeout_ws.closed

        bad_data = WS(json.dumps({"type": "wrong", "device_id": "dev"}))
        await em_controller.handle_data(bad_data)
        assert bad_data.closed

        original_sleep = asyncio.sleep
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: original_sleep(0, result=True))
        monkeypatch.setattr(em_controller, "_devices", {})
        monkeypatch.setattr(em_controller.asyncio, "sleep", lambda *args: original_sleep(0))
        unknown = WS(json.dumps({"type": "identify", "device_id": "missing"}))
        await em_controller.handle_data(unknown)
        assert unknown.closed

    asyncio.run(run())


def test_shell_handler_bridges_programmatic_and_dashboard_sessions(monkeypatch):
    import aiohttp

    class DeviceWS:
        def __init__(self, messages=()):
            self.messages = iter(messages)
            self.sent = []
            self.closed = False

        async def send(self, value):
            self.sent.append(value)

        async def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

    class DashboardWS:
        def __init__(self, messages=()):
            self.messages = iter(messages)
            self.text = []
            self.binary = []

        async def send_str(self, value):
            self.text.append(value)

        async def send_bytes(self, value):
            self.binary.append(value)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

    async def run():
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        pending = asyncio.get_running_loop().create_future()
        em_controller._shell_pending["dev"] = pending
        programmatic = DeviceWS()
        await em_controller.handle_shell(programmatic, "/shell/dev?pty=1")
        assert pending.result() is programmatic
        assert not programmatic.closed
        em_controller._shell_pending.pop("dev", None)

        pending = asyncio.get_running_loop().create_future()
        dashboard = DashboardWS([
            types.SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=b"stdin"),
            types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data="text"),
            types.SimpleNamespace(type=aiohttp.WSMsgType.CLOSE, data=b""),
        ])
        device = DeviceWS([b"stdout", "status"])
        em_controller._shell_pending["dev"] = pending
        em_controller._shell_dashboard["dev"] = dashboard
        await em_controller.handle_shell(device, "/shell/dev?pty=1")
        assert json.loads(dashboard.text[0]) == {"type": "shell_meta", "pty": True}
        assert dashboard.binary == [b"stdout"]
        assert "status" in dashboard.text
        assert device.sent == [b"stdin", b"text"]
        em_controller._shell_pending.pop("dev", None)
        em_controller._shell_dashboard.pop("dev", None)

    asyncio.run(run())


def test_voice_playback_helpers_wait_for_device_completion(monkeypatch):
    async def run():
        monkeypatch.setattr(em_controller.em_eq, "apply", lambda pcm, *args, **kwargs: pcm)
        monkeypatch.setattr(em_controller, "_push_device_state", lambda *args: asyncio.sleep(0))
        device = new_device()
        device.eq_bands = [0.0] * 8
        device.eq_loudness = False

        async def buffered(pcm):
            device.playback_done.set()

        device.stream_speaker = buffered
        await em_controller._run_post_turn_playback(device, b"pcm")
        assert device.speaking is False

        async def chunks():
            yield b"pcm"

        async def streamed(source, stream_eq):
            async for _chunk in source:
                pass
            device.playback_done.set()
            return 4, 1, 0.0, 2

        device.stream_speaker_chunks = streamed
        assert await em_controller._run_streaming_post_turn_playback(device, chunks()) == 4

        device.cancel_event.set()
        assert await em_controller._run_streaming_post_turn_playback(device, chunks()) == 0

    asyncio.run(run())


def test_speaker_lock_serializes_buffered_and_streaming_playback(monkeypatch):
    async def run():
        device = new_device()
        entered = []
        release_buffered = asyncio.Event()

        async def buffered(_device, _pcm, _cancel=None):
            entered.append("buffered")
            await release_buffered.wait()

        async def streamed(_device, _chunks):
            entered.append("streamed")
            return 1

        monkeypatch.setattr(em_controller, "_run_post_turn_playback_unlocked", buffered)
        monkeypatch.setattr(em_controller, "_run_streaming_post_turn_playback_unlocked", streamed)

        async def chunks():
            yield b"pcm"

        first = asyncio.create_task(em_controller._run_post_turn_playback(device, b"pcm"))
        await asyncio.sleep(0)
        second = asyncio.create_task(em_controller._run_streaming_post_turn_playback(device, chunks()))
        await asyncio.sleep(0)
        assert entered == ["buffered"]
        release_buffered.set()
        await asyncio.gather(first, second)
        assert entered == ["buffered", "streamed"]

    asyncio.run(run())


def test_output_chain_devices_bypass_controller_dsp(monkeypatch):
    async def run():
        device = new_device(["output_chain"])
        device.eq_bands = [12.0] * 8
        buffered = []

        def unexpected(*_args, **_kwargs):
            raise AssertionError("controller DSP must not run for output_chain")

        monkeypatch.setattr(em_controller.em_eq, "apply", unexpected)
        monkeypatch.setattr(em_controller.em_eq, "StreamingEQ", unexpected)

        async def stream_speaker(pcm):
            buffered.append(pcm)
            device.playback_done.set()

        device.stream_speaker = stream_speaker
        pcm = b"unaltered 48k PCM"
        await em_controller._run_post_turn_playback(device, pcm)
        assert buffered == [pcm]
        assert device.playback_eq_ms == 0

        seen = []

        async def chunks():
            yield pcm

        async def stream_chunks(source, processor):
            assert processor is None
            async for chunk in source:
                seen.append(chunk)
            device.playback_done.set()
            return len(pcm), 0, 0.0, 0

        device.stream_speaker_chunks = stream_chunks
        assert await em_controller._run_streaming_post_turn_playback(device, chunks()) == len(pcm)
        assert seen == [pcm]
        assert device.playback_eq_ms == 0

    asyncio.run(run())


def test_meter_at_playback_start_fires_at_prime_or_exhaustion():
    async def run():
        starts = []

        async def chunks():
            yield b"x" * int(em_controller.SPEAKER_PRIME_SECONDS * em_controller.SPEAKER_RATE * 2)

        async for chunk in em_controller._meter_at_playback_start(chunks(), lambda: asyncio.sleep(0, result=starts.append(True))):
            assert chunk
        assert starts == [True]

        starts.clear()

        async def short():
            yield b"short"

        async for _chunk in em_controller._meter_at_playback_start(short(), lambda: asyncio.sleep(0, result=starts.append(True))):
            pass
        assert starts == [True]

    asyncio.run(run())


def test_run_voice_locked_handles_normal_turn_and_continuation(monkeypatch):
    async def run():
        device = new_device()
        device.capabilities.append("stopword")
        device.stop_model_ready = True
        device.oww_paused.set()
        device.barge_in_enabled = False
        calls = []

        async def record(name, *args, **kwargs):
            calls.append(name)

        device.mic_start = lambda: record("mic_start")
        device.mic_stop = lambda: record("mic_stop")
        monkeypatch.setattr(em_controller.em_player, "interrupt", lambda *args: record("interrupt"))
        monkeypatch.setattr(em_controller.em_player, "resume_interrupted", lambda *args: record("resume"))
        monkeypatch.setattr(em_controller, "leds_listening", lambda *args: record("listening"))
        monkeypatch.setattr(em_controller, "_leds_turn_end", lambda *args: record("turn_end"))
        monkeypatch.setattr(em_controller, "_push_device_state", lambda *args: record("state"))
        monkeypatch.setattr(em_controller, "leds_spin_green", lambda *args: asyncio.sleep(3600))
        monkeypatch.setattr(em_controller, "_run_streaming_post_turn_playback", lambda *args: asyncio.sleep(0, result=1))
        monkeypatch.setattr(em_controller._wake_arbiter, "release", lambda *args: calls.append("release"))

        turns = []

        async def trigger(**kwargs):
            turns.append((kwargs["trigger_label"], kwargs["preroll_discard"]))
            await kwargs["on_thinking"]()
            async def pcm():
                yield b"response"
            await kwargs["post_turn_play"](pcm())
            return len(turns) == 1

        monkeypatch.setattr(em_controller.turn_engine, "trigger_voice_turn", trigger)
        await em_controller._run_voice_locked(device, "wakeword", is_wakeword=True)

        assert turns[0][0] == "wakeword"
        assert turns[1][0] == "continuation"
        assert turns[1][1] == 0
        assert "interrupt" in calls and "resume" in calls
        assert calls.count("mic_start") == 1
        assert device.oww_paused.is_set() is False

    asyncio.run(run())


def test_run_voice_locked_barge_restart_gets_a_fresh_admission_deadline(monkeypatch):
    """
    Regression test for a real, previously-shipped bug: the barge-restart
    branch of _run_voice_locked's loop used to keep reusing whichever
    admission_valid closure the function was ORIGINALLY called with —
    deadline included. For a barge admitted well into an active turn (the
    whole point of barge-in), that deadline belongs to the original wake and
    has long since passed, so the replacement turn's admission_valid()
    always failed the instant HA accepted its wake.offer. From the outside
    that read as "barge-in stops TTS but never opens the follow-up turn — it
    just ends the voice session", with the actual cause (a stale timestamp
    check) invisible anywhere in the symptom.
    """
    async def run():
        device = new_device()
        device.capabilities.append("stopword")
        device.stop_model_ready = True
        device.oww_model_ready = True
        device.data_ws = object()
        device.oww_paused.set()
        device.barge_in_enabled = True

        async def record(name, *args, **kwargs):
            pass

        device.mic_start = lambda: record("mic_start")
        device.mic_stop = lambda: record("mic_stop")
        monkeypatch.setattr(em_controller.em_player, "interrupt", lambda *args: record("interrupt"))
        monkeypatch.setattr(em_controller.em_player, "resume_interrupted", lambda *args: record("resume"))
        monkeypatch.setattr(em_controller, "leds_listening", lambda *args: record("listening"))
        monkeypatch.setattr(em_controller, "_leds_turn_end", lambda *args: record("turn_end"))
        monkeypatch.setattr(em_controller, "_push_device_state", lambda *args: record("state"))
        monkeypatch.setattr(em_controller, "leds_spin_green", lambda *args: asyncio.sleep(3600))
        monkeypatch.setattr(em_controller, "_run_streaming_post_turn_playback", lambda *args: asyncio.sleep(0, result=1))

        # The deadline an original wake_request from well before this call
        # would have produced — already expired by the time this runs.
        already_expired = asyncio.get_running_loop().time() - 10.0
        stale_admission_valid = em_controller._make_admission_valid(device, already_expired)
        assert stale_admission_valid() is False  # sanity: it really is expired

        seen = []

        async def trigger(**kwargs):
            seen.append(kwargs["admission_valid"])
            if len(seen) == 1:
                device.barge_request_id = "barge-1"
                device.barge_detected = True
                return False
            async def pcm():
                yield b"response"
            await kwargs["post_turn_play"](pcm())
            return False

        monkeypatch.setattr(em_controller.turn_engine, "trigger_voice_turn", trigger)
        old_devices = em_controller._devices
        em_controller._devices = {device.device_id: device}
        try:
            await em_controller._run_voice_locked(
                device, "wakeword", is_wakeword=True,
                request_id="original-wake", admission_valid=stale_admission_valid,
            )
            assert len(seen) == 2
            assert seen[0] is stale_admission_valid
            assert seen[0]() is False
            # The replacement turn must not inherit the original's expired
            # deadline — a fresh closure, evaluating True right now.
            assert seen[1] is not stale_admission_valid
            assert seen[1]() is True
        finally:
            em_controller._devices = old_devices

    asyncio.run(run())


def test_device_wake_request_barges_thinking_and_playback(monkeypatch):
    async def run():
        device = new_device(["wake_request_v1", "stopword"])
        device.oww_model_ready = True
        device.data_ws = object()
        device.stop_model_ready = True
        device.barge_in_enabled = True
        sent = []
        device.send_control = lambda message: asyncio.sleep(0, result=sent.append(message))
        monkeypatch.setattr(em_controller.turn_engine, "_push_event", lambda event: asyncio.sleep(0))

        old_engine = em_controller.turn_engine.ENGINE
        em_controller.turn_engine.ENGINE = em_controller.turn_engine.TurnEngine()
        old_devices = em_controller._devices
        em_controller._devices = {device.device_id: device}
        try:
            turn = em_controller.turn_engine.Turn(1, device, None, None)
            em_controller.turn_engine.ENGINE.turns[1] = turn
            async with device.voice_lock:
                device.thinking = True
                device.wake_request_id = "thinking"
                await em_controller._handle_wake_request(device, {
                    "requestId": "thinking", "score": 0.8,
                    "threshold": 0.5, "ageMs": 1, "activationSeq": 7,
                    "source": "wakeword", "model": device.oww_model,
                })
            assert turn.cancelled.is_set()
            assert not any(message.get("type") == "wake_grant" for message in sent)
            assert device.barge_request_id == "thinking"
            assert not any(message.get("type") == "speaker_flush" for message in sent)

            turn = em_controller.turn_engine.Turn(2, device, None, None)
            em_controller.turn_engine.ENGINE.turns = {2: turn}
            device.speaking = True
            device.wake_request_id = "playback"
            async with device.voice_lock:
                await em_controller._handle_wake_request(device, {
                    "requestId": "playback", "score": 0.8,
                    "threshold": 0.2, "ageMs": 1, "activationSeq": 8,
                    "source": "wakeword", "model": device.oww_model,
                })
            assert turn.cancelled.is_set()
            assert {"type": "speaker_flush"} in sent
        finally:
            em_controller.turn_engine.ENGINE = old_engine
            em_controller._devices = old_devices

    asyncio.run(run())


def test_handle_control_rejects_non_register_and_holds_unknown_device_pending(monkeypatch):
    class WS:
        remote_address = ("192.0.2.1", 1234)

        def __init__(self, message):
            self.message = message
            self.sent = []
            self.closed = False

        async def recv(self):
            return self.message

        async def send(self, message):
            self.sent.append(json.loads(message))

        async def close(self):
            self.closed = True

    async def run():
        bad = WS(json.dumps({"type": "ping"}))
        await em_controller.handle_control(bad)
        assert bad.closed

        pending = WS(json.dumps({"type": "register", "device_id": "new", "ip": "192.0.2.2"}))
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *args: asyncio.sleep(0, result=True))
        monkeypatch.setattr(em_controller.db, "get_config", lambda *args: "strict")
        monkeypatch.setattr(em_controller.db, "get_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "register_new_device", lambda *args: None)
        monkeypatch.setattr(em_controller.db, "log_device", lambda *args: None)
        monkeypatch.setattr(em_controller.api, "notify_device_pending", lambda *args: asyncio.sleep(0))
        await em_controller.handle_control(pending)
        assert pending.sent == [{"type": "pending"}]
        assert pending.closed

    asyncio.run(run())
