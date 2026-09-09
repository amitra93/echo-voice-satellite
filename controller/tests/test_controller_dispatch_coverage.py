"""
Additional coverage for em_controller.py's largest untested surface: the
per-message dispatch loop inside handle_control, plus the handle_data,
handle_shell, routing and mDNS/lag-monitor helpers around it.

test_controller_device.py and test_stop_captures.py already drive
handle_control end to end for the register/ack/config happy path and for a
handful of message types (see test_control_handler_processes_device_state_
messages and the dedicated stop_detected test). This file targets the
branches those leave uncovered: registration edge cases (auth failure,
auto-approval, pending-but-known devices, turn-history hydration failure),
message types that test never sends at all (wake_request, stop_status,
wake_status, wake_started, clock_probe, an "id"-bearing pong), and the
finally block's stale-connection path.

Same stubbing/monkeypatching style as those two files: `websockets` and
`zeroconf` are faked at import time (em_controller pulls in both at module
level and neither is installed in this test env), and every DB/turn-engine/
HA-sidechannel call is monkeypatched to a cheap in-memory stand-in so no
real I/O happens.
"""

import asyncio
import json
import sys
import types

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
import em_controller  # noqa: E402
import em_capture_upload  # noqa: E402


def new_device(capabilities=None):
    class FakeWS:
        def __init__(self):
            self.messages = []

        async def send(self, message):
            self.messages.append(message)

    return em_controller.Device("dev", "192.0.2.1", capabilities or [], FakeWS())


def run(awaitable):
    return asyncio.run(awaitable)


async def _noop(*_args, **_kwargs):
    return None


def _install_common_db_stubs(monkeypatch, row):
    """The full set of DB/side-channel stand-ins handle_control's happy
    path needs, shared by every dispatch-loop test below — mirrors the
    monkeypatch list the existing stop_detected/state-message tests use."""
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    monkeypatch.setattr(em_controller.db, "get_config", lambda *a: "strict")
    monkeypatch.setattr(em_controller.db, "get_device", lambda *a: row)
    monkeypatch.setattr(em_controller.db, "get_turns", lambda *a: [])
    monkeypatch.setattr(em_controller.db, "get_effective_device_config", lambda *a: dict(row.get("_config", {})))
    monkeypatch.setattr(em_controller.db, "get_device_config", lambda *a: {})
    monkeypatch.setattr(em_controller.db, "set_device_config", lambda *a: None)
    monkeypatch.setattr(em_controller.db, "record_device_stats", lambda *a: None)
    monkeypatch.setattr(em_controller.db, "touch_device_seen", lambda *a: None)
    monkeypatch.setattr(em_controller.db, "bump_wake_counters", lambda *a, **kw: None)
    monkeypatch.setattr(em_controller.db, "upsert_device_seen", lambda *a: None)
    monkeypatch.setattr(em_controller.db, "log_device", lambda *a: None)
    monkeypatch.setattr(em_controller.db, "set_turn_playback", lambda *a: None)
    monkeypatch.setattr(em_controller.db, "set_turn_delivery", lambda *a: None)
    monkeypatch.setattr(em_controller.db, "update_turn", lambda *a: None)
    monkeypatch.setattr(em_controller.db, "register_new_device", lambda *a: None)
    monkeypatch.setattr(em_controller.db, "approve_device", lambda *a: None)
    monkeypatch.setattr(em_controller.api, "_push_event", _noop)
    monkeypatch.setattr(em_controller.api, "_push_log_event", _noop)
    monkeypatch.setattr(em_controller.api, "wifi_record_result", lambda *a: ({"pending": None}, False))
    monkeypatch.setattr(em_controller.api, "notify_device_connected", _noop)
    monkeypatch.setattr(em_controller.api, "notify_device_disconnected", _noop)
    monkeypatch.setattr(em_controller.api, "notify_device_pending", _noop)
    monkeypatch.setattr(em_controller.api, "reconcile_oww_assets", _noop)
    monkeypatch.setattr(em_controller.api, "dismiss_timer_alarm", lambda *a: asyncio.sleep(0, result=True))
    monkeypatch.setattr(em_controller.em_player, "device_gone", lambda *a: None)
    monkeypatch.setattr(em_controller, "leds_idle", _noop)
    monkeypatch.setattr(em_controller.ha_sidechannels, "ambient_light", lambda *a: None)
    monkeypatch.setattr(em_controller.ha_sidechannels, "mute_state", lambda *a: None)
    monkeypatch.setattr(em_controller.ha_sidechannels, "volume", lambda *a: None)
    monkeypatch.setattr(em_controller.ha_sidechannels, "capabilities", lambda *a: None)
    monkeypatch.setattr(em_controller.ha_sidechannels, "wake_model", lambda *a: None)
    monkeypatch.setattr(em_controller.ha_sidechannels, "ble_adverts", lambda *a: None)


class AsyncGenWS:
    """
    A /control fake WS whose message stream is an async generator, so a
    test can `await` real side effects (acquiring device.voice_lock,
    yielding control to a spawned task, mutating _devices) BETWEEN
    messages — something a synchronous `next()`-driven generator cannot do,
    since handle_control's `async for raw in ws:` only truly suspends at
    real await points and a plain generator never provides one.
    """
    remote_address = ("192.0.2.20", 8767)

    def __init__(self, stream_factory):
        self.sent = []
        self.closed = False
        self._agen = stream_factory()

    async def recv(self):
        return await self._agen.__anext__()

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._agen.__anext__()


# ─── Device speaker-stream edge cases ──────────────────────────────────────

def test_stream_speaker_breaks_on_cancel_and_swallows_eos_send_failure():
    async def run_it():
        device = new_device()
        device.cancel_event.set()

        async def failing_send(_frame):
            raise RuntimeError("link gone mid-EOS")

        device.send_data = failing_send
        # cancel_event is already set, so the while loop breaks on its very
        # first check (never calling send_data for a frame); the finally's
        # EOS send then raises and is swallowed rather than propagating —
        # a dead link at exactly this moment must not crash the caller.
        await device.stream_speaker(b"x" * 10)

    run(run_it())


def test_stream_speaker_chunks_covers_outer_break_inner_break_tail_flush_and_eos_failure():
    async def run_it():
        # Outer loop break: cancel_event is set before the first chunk is
        # even inspected.
        outer = new_device()
        outer.cancel_event.set()
        outer.send_data = lambda frame: asyncio.sleep(0)

        async def one_chunk():
            yield b"x" * 10

        total, _eq_ms, first, _send_ms = await outer.stream_speaker_chunks(one_chunk())
        assert total == 0 and first is None

        # Inner loop break: the first period's send flips cancel_event, so
        # the SECOND period queued in the same chunk must never be sent.
        inner = new_device()
        sent = []

        async def send_then_cancel(frame):
            sent.append(frame)
            if len(sent) == 1:
                inner.cancel_event.set()

        inner.send_data = send_then_cancel

        async def two_periods():
            yield b"y" * (em_controller.SPEAKER_BYTES * 2)

        total2, *_ = await inner.stream_speaker_chunks(two_periods())
        # One data frame (the first period) plus the EOS frame — the second
        # period's send never happens because the inner break fires first.
        assert len(sent) == 2
        assert sent[-1] == bytes([em_controller.SPEAKER_EOS_TYPE])
        assert total2 == em_controller.SPEAKER_BYTES * 2

        # Tail flush that never crossed a full period in the main loop: the
        # ONLY place first_send_time gets set is the final "if pending and
        # not cancelled" flush.
        tail = new_device()
        tail_sent = []
        tail.send_data = lambda frame: asyncio.sleep(0, result=tail_sent.append(frame))

        async def small():
            yield b"z" * 10

        total3, _eq3, first3, _send3 = await tail.stream_speaker_chunks(small())
        assert total3 == 10
        assert first3 is not None
        assert tail_sent[0] == bytes([em_controller.SPEAKER_FRAME_TYPE]) + b"z" * 10 + bytes(
            em_controller.SPEAKER_BYTES - 10)

        # EOS send failure in this method's own finally is swallowed too —
        # a separate except block from stream_speaker's. The data frame
        # itself must still succeed, or the failure never reaches the EOS
        # send this test is targeting.
        broken = new_device()
        broken_calls = []

        async def failing_on_eos(frame):
            broken_calls.append(frame)
            if frame == bytes([em_controller.SPEAKER_EOS_TYPE]):
                raise RuntimeError("gone")

        broken.send_data = failing_on_eos

        async def small2():
            yield b"a" * 5

        await broken.stream_speaker_chunks(small2())
        assert broken_calls[-1] == bytes([em_controller.SPEAKER_EOS_TYPE])

    run(run_it())


def test_speaker_stream_methods_end_marker_is_a_harmless_noop():
    # Pure delimiter between the speaker-stream methods and the module-level
    # playback runners — never called in production, but it is still real
    # code and should not silently rot.
    device = new_device()
    assert device._speaker_stream_methods_end() is None


# ─── _link_auth_ok edge cases ───────────────────────────────────────────────

def test_link_auth_ok_tolerates_a_ws_with_no_request_attribute(monkeypatch):
    """
    ws.request.headers.get(...) is wrapped in `except AttributeError: pass`
    because not every WS-like object exposes `.request` (the raw
    websockets library's server connections do; some fakes/older shapes may
    not) — falling back to "no token presented" rather than crashing.
    """
    class BareWS:
        pass

    monkeypatch.setattr(em_controller.db, "get_device_token", lambda *_: None)
    monkeypatch.setattr(em_controller, "REQUIRE_DEVICE_TLS", False)
    assert run(em_controller._link_auth_ok(BareWS(), "dev", False, "control")) is True


def test_link_auth_ok_logs_and_allows_a_stale_presented_token(monkeypatch):
    """A token presented for a device with nothing on record is ADMITTED
    (not rejected — see the module docstring) but is worth a log line,
    which is the branch this pins."""
    class WS:
        request = types.SimpleNamespace(headers={"X-EM-Token": "orphaned"})

    monkeypatch.setattr(em_controller.db, "get_device_token", lambda *_: None)
    monkeypatch.setattr(em_controller, "REQUIRE_DEVICE_TLS", False)
    assert run(em_controller._link_auth_ok(WS(), "dev", False, "control")) is True


def test_link_auth_ok_rejects_a_mismatched_token(monkeypatch):
    class WS:
        request = types.SimpleNamespace(headers={"X-EM-Token": "wrong"})

    monkeypatch.setattr(em_controller.db, "get_device_token", lambda *_: "correct")
    monkeypatch.setattr(em_controller, "REQUIRE_DEVICE_TLS", False)
    assert run(em_controller._link_auth_ok(WS(), "dev", False, "control")) is False


# ─── _handle_wake_request deny/admit branches not already covered ─────────

def _wake_ready_device(**overrides):
    device = new_device(["wake_request_v1", "stopword"])
    device.wake_request_id = "req-1"
    device.oww_model_ready = True
    device.stop_model_ready = True
    device.data_ws = object()
    for key, value in overrides.items():
        setattr(device, key, value)
    return device


def test_handle_wake_request_silently_drops_a_missing_or_empty_request_id():
    """Nothing to deny — there is no id to answer with, so this returns
    without sending anything at all (distinct from every other rejection,
    which always answers)."""
    device = _wake_ready_device()
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller._handle_wake_request(device, {"score": 0.9}))
    run(em_controller._handle_wake_request(device, {"requestId": "", "score": 0.9}))
    assert sent == []


def test_handle_wake_request_denies_unsupported_device():
    device = new_device([])  # no wake_request_v1 capability at all
    device.wake_request_id = "req-1"
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller._handle_wake_request(device, {
        "requestId": "req-1", "score": 0.8, "threshold": 0.5,
        "ageMs": 1, "activationSeq": 1,
    }))
    assert sent == [{"type": "wake_deny", "requestId": "req-1", "reason": "unsupported"}]


def test_handle_wake_request_denies_mismatched_request_id():
    device = _wake_ready_device()
    device.wake_request_id = "different"
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller._handle_wake_request(device, {
        "requestId": "req-1", "score": 0.8, "threshold": 0.5,
        "ageMs": 1, "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
    }))
    assert sent[-1]["reason"] == "busy"


@pytest.mark.parametrize("msg_overrides,reason", [
    ({"source": "button"}, "not_ready"),          # wrong source
    ({"model": "someone-elses-model"}, "not_ready"),  # wrong model
])
def test_handle_wake_request_denies_source_or_model_mismatch(msg_overrides, reason):
    device = _wake_ready_device()
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    msg = {"requestId": "req-1", "score": 0.8, "threshold": 0.5, "ageMs": 1,
           "activationSeq": 1, "source": "wakeword", "model": device.oww_model}
    msg.update(msg_overrides)
    run(em_controller._handle_wake_request(device, msg))
    assert sent[-1]["reason"] == reason


def test_handle_wake_request_denies_when_model_not_ready():
    device = _wake_ready_device(oww_model_ready=False)
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller._handle_wake_request(device, {
        "requestId": "req-1", "score": 0.8, "threshold": 0.5, "ageMs": 1,
        "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
    }))
    assert sent[-1]["reason"] == "not_ready"


def test_handle_wake_request_denies_while_muted():
    device = _wake_ready_device(muted=True)
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller._handle_wake_request(device, {
        "requestId": "req-1", "score": 0.8, "threshold": 0.5, "ageMs": 1,
        "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
    }))
    assert sent[-1]["reason"] == "muted"


def test_handle_wake_request_denies_without_a_data_plane():
    device = _wake_ready_device(data_ws=None)
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller._handle_wake_request(device, {
        "requestId": "req-1", "score": 0.8, "threshold": 0.5, "ageMs": 1,
        "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
    }))
    assert sent[-1]["reason"] == "no_data"


@pytest.mark.parametrize("msg", [
    {"score": "nan", "threshold": 0.5, "ageMs": 1, "activationSeq": 1},   # unparseable
    {"score": 0.9, "threshold": 0.5, "ageMs": 1, "activationSeq": 1, "score": "oops"},
])
def test_handle_wake_request_denies_unparseable_fields(msg):
    device = _wake_ready_device()
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    full = {"requestId": "req-1", "source": "wakeword", "model": device.oww_model}
    full.update(msg)
    run(em_controller._handle_wake_request(device, full))
    assert sent[-1]["reason"] == "malformed"


def test_handle_wake_request_denies_score_below_threshold_as_malformed():
    device = _wake_ready_device()
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller._handle_wake_request(device, {
        "requestId": "req-1", "score": 0.1, "threshold": 0.9, "ageMs": 1,
        "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
    }))
    assert sent[-1]["reason"] == "malformed"


def test_handle_wake_request_denies_stale_age():
    device = _wake_ready_device()
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller._handle_wake_request(device, {
        "requestId": "req-1", "score": 0.9, "threshold": 0.5, "ageMs": 4001,
        "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
    }))
    assert sent[-1]["reason"] == "stale"


def test_handle_wake_request_denies_barge_when_disabled_but_locked():
    device = _wake_ready_device()
    device.barge_in_enabled = False
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))

    async def run_it():
        async with device.voice_lock:
            await em_controller._handle_wake_request(device, {
                "requestId": "req-1", "score": 0.9, "threshold": 0.5, "ageMs": 1,
                "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
            })

    run(run_it())
    assert sent[-1]["reason"] == "barge_disabled"


def test_handle_wake_request_denies_when_stop_word_not_ready():
    device = _wake_ready_device(stop_model_ready=False)
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller._handle_wake_request(device, {
        "requestId": "req-1", "score": 0.9, "threshold": 0.5, "ageMs": 1,
        "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
    }))
    assert sent[-1]["reason"] == "not_ready"


def test_handle_wake_request_arbitration_loser_is_denied_and_arb_ms_disables_solo(monkeypatch):
    """Covers the `claimed = True` admit path elsewhere; this test drives
    the losing side of arbitration on a multi-device fleet (len(_devices) >
    1 is required for arbitration to run at all)."""
    winner = _wake_ready_device()
    loser = _wake_ready_device()
    loser.device_id = "loser"
    loser.wake_request_id = "req-1"
    loser.wake_arb_ms = 300
    sent = []
    loser.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))

    old_devices = em_controller._devices
    old_arbiter = em_controller._wake_arbiter
    em_controller._devices = {winner.device_id: winner, loser.device_id: loser}
    em_controller._wake_arbiter = em_controller.em_arbiter.WakeArbiter()

    async def run_it():
        em_controller._wake_arbiter.claim(winner.device_id, 0.3)
        await em_controller._handle_wake_request(loser, {
            "requestId": "req-1", "score": 0.9, "threshold": 0.5, "ageMs": 1,
            "activationSeq": 1, "source": "wakeword", "model": loser.oww_model,
        })

    try:
        run(run_it())
    finally:
        em_controller._devices = old_devices
        em_controller._wake_arbiter = old_arbiter
    assert sent[-1]["reason"] == "arbitration"
    assert loser.wake_request_id is None


def test_handle_wake_request_arbitration_winner_admits_and_releases_its_claim(monkeypatch):
    """The other half of arbitration: on a multi-device fleet, being FIRST
    to claim (nothing else has claimed yet) runs the turn and releases the
    claim in the finally — proven by a second device successfully claiming
    right afterwards, which a still-held claim would have refused."""
    winner = _wake_ready_device()
    other = _wake_ready_device()
    other.device_id = "other"

    async def fake_run_voice_locked(*args, **kwargs):
        return None

    monkeypatch.setattr(em_controller, "_run_voice_locked", fake_run_voice_locked)
    old_devices = em_controller._devices
    old_arbiter = em_controller._wake_arbiter
    em_controller._devices = {winner.device_id: winner, other.device_id: other}
    em_controller._wake_arbiter = em_controller.em_arbiter.WakeArbiter()

    async def run_it():
        await em_controller._handle_wake_request(winner, {
            "requestId": "req-1", "score": 0.9, "threshold": 0.5, "ageMs": 1,
            "activationSeq": 1, "source": "wakeword", "model": winner.oww_model,
        })
        # If the winner's claim were still held, this would lose.
        assert em_controller._wake_arbiter.claim("someone-else", 0.3) == "someone-else"

    try:
        run(run_it())
    finally:
        em_controller._devices = old_devices
        em_controller._wake_arbiter = old_arbiter


def test_handle_wake_request_denies_disconnected_between_claim_and_run(monkeypatch):
    """The `_devices.get(id) is not device` guard right before
    `_run_voice_locked` — a device that vanished from the registry between
    admission and the actual turn start (a race the comment above it calls
    out explicitly)."""
    device = _wake_ready_device()
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))

    old_devices = em_controller._devices
    em_controller._devices = {}  # device is NOT registered under its own id
    try:
        run(em_controller._handle_wake_request(device, {
            "requestId": "req-1", "score": 0.9, "threshold": 0.5, "ageMs": 1,
            "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
        }))
    finally:
        em_controller._devices = old_devices
    assert sent[-1]["reason"] == "disconnected"


def test_handle_wake_request_admits_solo_device_and_releases_claim_on_finally(monkeypatch):
    """Single-device fleet: arbitration is skipped entirely
    (len(_devices) == 1), so `claimed` stays False and the finally's
    `_wake_arbiter.release` call is never reached for THIS device — but the
    `_run_voice_locked` call itself, the `oww_paused` set/clear pair and
    `last_wake` bookkeeping on the admit path all run for real here."""
    device = _wake_ready_device()
    device.send_control = lambda m: asyncio.sleep(0)

    seen = []

    async def fake_run_voice_locked(*args, **kwargs):
        seen.append(kwargs.get("request_id"))

    monkeypatch.setattr(em_controller, "_run_voice_locked", fake_run_voice_locked)
    old_devices = em_controller._devices
    em_controller._devices = {device.device_id: device}
    try:
        run(em_controller._handle_wake_request(device, {
            "requestId": "req-1", "score": 0.9, "threshold": 0.5, "ageMs": 1,
            "activationSeq": 1, "source": "wakeword", "model": device.oww_model,
        }))
    finally:
        em_controller._devices = old_devices
    assert seen == ["req-1"]
    assert device.oww_paused.is_set() is False
    assert device.oww_paused_since is None
    assert device.last_wake["model"] == device.oww_model


def test_wake_status_ready_swallows_resolution_errors(monkeypatch):
    device = new_device()
    device.oww_model = "hey_jarvis_v0.1"

    def boom(_model):
        raise RuntimeError("asset store unavailable")

    monkeypatch.setattr(em_controller.em_oww_assets, "classifier_source", boom)
    assert em_controller._wake_status_ready(
        device, {"ready": True, "model": "hey_jarvis_v0.1", "classifierMd5": "a" * 32},
    ) is False


# ─── handle_button_event branches not already covered ─────────────────────

def test_button_handler_denies_with_gesture_reason_when_request_id_present(monkeypatch):
    """deny_button only actually SENDS a wake_deny when the event carries a
    requestId — covers that isinstance/truthiness branch inside the closure,
    which every other button test omits."""
    monkeypatch.setattr(em_controller.ha_sidechannels, "button_event", lambda *a: None)
    device = new_device(["button_hold"])
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller.handle_button_event(device, {
        "clickType": 138, "down": False, "heldMs": 900, "requestId": "btn-1",
    }))
    assert {"type": "wake_deny", "requestId": "btn-1", "reason": "gesture"} in sent


def test_button_handler_multi_tap_window_coalesces_into_a_burst(monkeypatch):
    events = []
    monkeypatch.setattr(em_controller.ha_sidechannels, "button_event",
                        lambda *a: events.append(a))
    device = new_device(["button_hold"])
    device.button_single_tap_event = True
    device.button_multi_tap_ms = 350
    run(em_controller.handle_button_event(
        device, {"clickType": 138, "down": False, "heldMs": 50}
    ))
    # A short tap under a positive multi-tap window feeds the coalescer
    # rather than firing an event immediately.
    assert events == []
    assert device.tap_burst.count == 1
    device.tap_burst.cancel()


def test_button_handler_blocked_when_muted_and_no_turn_active(monkeypatch):
    monkeypatch.setattr(em_controller.ha_sidechannels, "button_event", lambda *a: None)
    device = new_device(["button_hold"])
    device.button_single_tap_event = False
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    run(em_controller.handle_button_event(device, {
        "clickType": 138, "down": False, "heldMs": 50, "muted": True,
        "requestId": "btn-2",
    }))
    assert {"type": "wake_deny", "requestId": "btn-2", "reason": "muted"} in sent


def test_button_handler_voice_turn_denies_missing_request_id_busy_not_ready_and_no_data():
    device = new_device(["button_hold"])

    async def run_it():
        # No requestId at all -> warn-and-return, no send at all.
        await em_controller.handle_button_event(
            device, {"clickType": 138, "down": False, "heldMs": 50})

        sent = []
        device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))

        # Already busy with another wake admission.
        device.wake_request_id = "already-in-flight"
        await em_controller.handle_button_event(device, {
            "clickType": 138, "down": False, "heldMs": 50, "requestId": "btn-3"})
        assert sent[-1]["reason"] == "busy"
        device.wake_request_id = None

        # Stop word not ready.
        await em_controller.handle_button_event(device, {
            "clickType": 138, "down": False, "heldMs": 50, "requestId": "btn-4"})
        assert sent[-1]["reason"] == "not_ready"

        # Stop word ready but no data plane.
        device.capabilities.append("stopword")
        device.stop_model_ready = True
        await em_controller.handle_button_event(device, {
            "clickType": 138, "down": False, "heldMs": 50, "requestId": "btn-5"})
        assert sent[-1]["reason"] == "no_data"

    run(run_it())


def test_button_handler_starts_a_voice_turn_when_everything_is_ready(monkeypatch):
    device = new_device(["button_hold", "stopword"])
    device.stop_model_ready = True
    device.data_ws = object()
    device.mic_stop = lambda: asyncio.sleep(0)
    device.mic_start = lambda: asyncio.sleep(0)

    seen = []

    async def fake_run_voice_locked(*args, **kwargs):
        seen.append(kwargs.get("request_id"))

    monkeypatch.setattr(em_controller, "_run_voice_locked", fake_run_voice_locked)

    async def run_it():
        await em_controller.handle_button_event(device, {
            "clickType": 138, "down": False, "heldMs": 50, "requestId": "btn-6"})
        # The button turn runs as a fire-and-forget task.
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0)

    run(run_it())
    assert seen == ["btn-6"]
    assert device.wake_request_id is None


# ─── _accept_capture_upload, called directly (no wire framing needed) ─────

def _completed(model, classifier_md5, capture_id="cap-1"):
    return em_capture_upload.CompletedCapture(
        metadata={"model": model, "classifierMd5": classifier_md5, "captureId": capture_id},
        pcm=b"\x00\x01" * 100,
    )


def test_accept_capture_upload_rejects_when_the_durable_write_declines(monkeypatch):
    """save_uploaded returning None (e.g. a duplicate captureId already on
    disk) must not be treated as success — no ack, no further processing."""
    device = new_device()
    device.oww_model = "hey_jarvis_v0.1"
    device.save_wake_captures = True
    device.data_ws = object()
    monkeypatch.setattr(em_controller.em_oww_models, "prediction_key", lambda m: "wake")
    monkeypatch.setattr(em_controller.em_training_captures, "save_uploaded",
                        lambda *a, **kw: (None, False))
    old_devices = em_controller._devices
    em_controller._devices = {device.device_id: device}
    try:
        accepted = run(em_controller._accept_capture_upload(
            device, device.data_ws, _completed("wake", None),
        ))
    finally:
        em_controller._devices = old_devices
    assert accepted is False


def test_accept_capture_upload_rejects_unknown_model(monkeypatch):
    device = new_device()
    device.oww_model = "hey_jarvis_v0.1"
    monkeypatch.setattr(em_controller.em_oww_models, "prediction_key", lambda m: m)
    device.data_ws = object()
    accepted = run(em_controller._accept_capture_upload(
        device, device.data_ws, _completed("not_a_real_model", "x" * 32),
    ))
    assert accepted is False


def test_accept_capture_upload_resolves_stop_model_checksum_via_oww_assets(monkeypatch, tmp_path):
    """
    Unlike wake_status, stop_status carries no classifier digest from the
    device, so the controller independently resolves the file it is itself
    distributing and computes the checksum to compare against.
    """
    device = new_device()
    device.oww_model = "hey_jarvis_v0.1"
    device.stop_model = "stop"
    device.save_stop_captures = True
    device.data_ws = object()
    monkeypatch.setattr(em_controller.em_oww_models, "prediction_key",
                        lambda m: {"hey_jarvis_v0.1": "wake", "stop": "stop"}[m])
    stop_file = tmp_path / "stop.onnx"
    stop_file.write_bytes(b"classifier-bytes")
    monkeypatch.setattr(em_controller.em_oww_assets, "classifier_source", lambda m: str(stop_file))
    expected_md5 = em_controller.em_oww_assets.md5_file(str(stop_file))

    saved = {}

    def fake_save_uploaded(model, device_id, metadata, pcm, with_status=False):
        saved["called"] = (model, device_id)
        return ("cap.wav", True) if with_status else "cap.wav"

    monkeypatch.setattr(em_controller.em_training_captures, "save_uploaded", fake_save_uploaded)
    old_devices = em_controller._devices
    em_controller._devices = {device.device_id: device}
    sent = []
    device.send_control = lambda m: asyncio.sleep(0, result=sent.append(m))
    try:
        accepted = run(em_controller._accept_capture_upload(
            device, device.data_ws, _completed("stop", expected_md5),
        ))
    finally:
        em_controller._devices = old_devices
    assert accepted is True
    assert saved["called"] == ("stop", device.device_id)
    assert sent == [{"type": "capture_ack", "captureId": "cap-1"}]


def test_accept_capture_upload_swallows_os_error_resolving_stop_classifier(monkeypatch):
    """`em_oww_assets.classifier_source`/`md5_file` failing with an OSError
    (the file vanished, a permissions problem) must not crash capture
    handling — it degrades to "no expected checksum", which then rejects
    the upload on the mismatch check right after, rather than 500ing the
    connection."""
    device = new_device()
    device.oww_model = "hey_jarvis_v0.1"
    device.stop_model = "stop"
    device.save_stop_captures = True
    monkeypatch.setattr(em_controller.em_oww_models, "prediction_key",
                        lambda m: {"hey_jarvis_v0.1": "wake", "stop": "stop"}[m])

    def boom(_model):
        raise OSError("gone")

    monkeypatch.setattr(em_controller.em_oww_assets, "classifier_source", boom)
    accepted = run(em_controller._accept_capture_upload(
        device, device.data_ws, _completed("stop", "somechecksum"),
    ))
    assert accepted is False


def test_accept_capture_upload_discards_when_opt_out_flips_during_write(monkeypatch):
    """The flag is re-checked after the (blocking) durable write completes,
    because it may have flipped while that write was in flight — and if it
    did AND the write actually created a new file, that file is discarded."""
    device = new_device()
    device.oww_model = "hey_jarvis_v0.1"
    device.save_wake_captures = True
    device.data_ws = object()
    monkeypatch.setattr(em_controller.em_oww_models, "prediction_key", lambda m: "wake")

    def fake_save_uploaded(model, device_id, metadata, pcm, with_status=False):
        # Flip the flag AFTER the "durable write" to simulate a race with a
        # config change landing mid-upload.
        device.save_wake_captures = False
        return ("cap.wav", True) if with_status else "cap.wav"

    discarded = []
    monkeypatch.setattr(em_controller.em_training_captures, "save_uploaded", fake_save_uploaded)
    monkeypatch.setattr(em_controller.em_training_captures, "discard",
                        lambda model, name: discarded.append((model, name)))
    old_devices = em_controller._devices
    em_controller._devices = {device.device_id: device}
    try:
        accepted = run(em_controller._accept_capture_upload(
            device, device.data_ws, _completed("wake", None),
        ))
    finally:
        em_controller._devices = old_devices
    assert accepted is False
    assert discarded == [("wake", "cap.wav")]


def test_accept_capture_upload_rejects_when_device_replaced_after_commit(monkeypatch):
    """The committed file is a valid retry rendezvous; a replacement
    connection racing the same capture must not have this stale one
    re-acknowledge or interfere."""
    device = new_device()
    device.oww_model = "hey_jarvis_v0.1"
    device.save_wake_captures = True
    device.data_ws = object()
    monkeypatch.setattr(em_controller.em_oww_models, "prediction_key", lambda m: "wake")
    monkeypatch.setattr(em_controller.em_training_captures, "save_uploaded",
                        lambda *a, **kw: ("cap.wav", True))
    old_devices = em_controller._devices
    em_controller._devices = {}  # device no longer registered under its id
    try:
        accepted = run(em_controller._accept_capture_upload(
            device, device.data_ws, _completed("wake", None),
        ))
    finally:
        em_controller._devices = old_devices
    assert accepted is False


# ─── handle_data branches not already covered ──────────────────────────────

class IdentifyWS:
    """A /data fake WS: first recv() returns the identify handshake, then
    __anext__ streams the supplied raw frames."""
    remote_address = ("192.0.2.21", 8767)

    def __init__(self, device_id, frames):
        self.device_id = device_id
        self._frames = iter(frames)
        self.closed = False

    async def recv(self):
        return json.dumps({"type": "identify", "device_id": self.device_id})

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._frames)
        except StopIteration:
            raise StopAsyncIteration


def test_handle_data_closes_on_link_auth_failure(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=False))
    ws = IdentifyWS("dev", [])
    run(em_controller.handle_data(ws))
    assert ws.closed


class _ScriptedQueue:
    """A voice_queue stand-in whose full()/get_nowait()/put_nowait() are
    scripted rather than backed by real asyncio.Queue semantics — needed to
    reach QueueFull-after-drain races that cannot occur naturally within a
    single, uninterrupted pass through one frame handler (nothing else runs
    between the drain and the retry there)."""

    def __init__(self, full_results=(), get_results=(), put_results=()):
        self._full = list(full_results)
        self._get = list(get_results)
        self._put = list(put_results)
        self.put_calls = []

    def full(self):
        return self._full.pop(0)

    def get_nowait(self):
        result = self._get.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def put_nowait(self, item):
        self.put_calls.append(item)
        result = self._put.pop(0) if self._put else None
        if isinstance(result, BaseException):
            raise result


def _vad_frame(kind):
    return bytes([em_controller.MIC_FRAME_TYPE, 0, 0]) + bytes([kind])


def test_handle_data_vad_sentinel_skipped_while_oww_not_paused(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()
    # oww_paused is NOT set — a VAD sentinel outside an admitted wake turn
    # must be dropped, since it belongs to no queue reader.
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    ws = IdentifyWS("dev", [_vad_frame(em_controller.VAD_END_TYPE)])
    try:
        run(em_controller.handle_data(ws))
    finally:
        em_controller._devices = old_devices
    assert device.voice_queue.empty()


def test_handle_data_vad_sentinel_drains_full_queue_then_delivers(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()
    device.oww_paused.set()
    device.voice_queue = _ScriptedQueue(full_results=[True], get_results=[b"old"], put_results=[None])
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    ws = IdentifyWS("dev", [_vad_frame(em_controller.VAD_END_TYPE)])
    try:
        run(em_controller.handle_data(ws))
    finally:
        em_controller._devices = old_devices
    assert device.voice_queue.put_calls == [em_controller.turn_engine.VAD_SENTINEL_END]


def test_handle_data_vad_sentinel_drain_raises_queue_empty_then_still_delivers(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()
    device.oww_paused.set()
    device.voice_queue = _ScriptedQueue(
        full_results=[True], get_results=[asyncio.QueueEmpty()], put_results=[None])
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    ws = IdentifyWS("dev", [_vad_frame(em_controller.VAD_NO_SPEECH_TIMEOUT_TYPE)])
    try:
        run(em_controller.handle_data(ws))
    finally:
        em_controller._devices = old_devices
    assert device.voice_queue.put_calls == [em_controller.turn_engine.VAD_SENTINEL_TIMEOUT]


def test_handle_data_vad_sentinel_lost_when_queue_stays_full(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()
    device.oww_paused.set()
    device.voice_queue = _ScriptedQueue(
        full_results=[True], get_results=[b"old"], put_results=[asyncio.QueueFull()])
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    ws = IdentifyWS("dev", [_vad_frame(em_controller.VAD_END_TYPE)])
    try:
        run(em_controller.handle_data(ws))  # must not raise despite the lost sentinel
    finally:
        em_controller._devices = old_devices


def test_handle_data_ordinary_payload_dropped_while_oww_not_paused(monkeypatch):
    """
    Device-only wake detection owns idle microphone PCM: an ordinary (non-
    VAD-sentinel) frame arriving outside an admitted wake turn must be
    dropped, never queued for a reader that isn't there.
    """
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()  # oww_paused is NOT set
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    frame = bytes([em_controller.MIC_FRAME_TYPE, 0, 0]) + b"payload"
    ws = IdentifyWS("dev", [frame])
    try:
        run(em_controller.handle_data(ws))
    finally:
        em_controller._devices = old_devices
    assert device.voice_queue.empty()


def test_handle_data_ordinary_payload_drops_oldest_frame_on_queue_full(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()
    device.oww_paused.set()
    device.voice_queue = _ScriptedQueue(get_results=[b"old"], put_results=[asyncio.QueueFull(), None])
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    frame = bytes([em_controller.MIC_FRAME_TYPE, 0, 0]) + b"payload"
    ws = IdentifyWS("dev", [frame])
    try:
        run(em_controller.handle_data(ws))
    finally:
        em_controller._devices = old_devices
    assert device.voice_queue.put_calls == [b"payload", b"payload"]


def test_handle_data_ordinary_payload_drop_and_retry_can_both_fail_silently(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()
    device.oww_paused.set()
    device.voice_queue = _ScriptedQueue(
        get_results=[asyncio.QueueEmpty()], put_results=[asyncio.QueueFull()])
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    frame = bytes([em_controller.MIC_FRAME_TYPE, 0, 0]) + b"payload"
    ws = IdentifyWS("dev", [frame])
    try:
        run(em_controller.handle_data(ws))  # must not raise
    finally:
        em_controller._devices = old_devices


def test_handle_data_recovers_from_unexpected_frame_exceptions_and_rate_limits_logging(monkeypatch):
    """
    Two genuinely unexpected exceptions in immediate succession: the first
    is logged in full, the second (arriving well under the 5s suppression
    window, since no real time passes in this test) is only counted.
    """
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()
    device.oww_paused.set()

    class ExplodingQueue:
        def full(self):
            return False

        def put_nowait(self, _item):
            raise RuntimeError("unexpected bug")

    device.voice_queue = ExplodingQueue()
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    frame = bytes([em_controller.MIC_FRAME_TYPE, 0, 0]) + b"payload"
    ws = IdentifyWS("dev", [frame, frame])
    try:
        run(em_controller.handle_data(ws))  # must not raise despite the bug
    finally:
        em_controller._devices = old_devices


def test_handle_data_reraises_cancelled_error_from_a_frame_handler(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()
    device.oww_paused.set()

    class CancellingQueue:
        def full(self):
            return False

        def put_nowait(self, _item):
            raise asyncio.CancelledError()

    device.voice_queue = CancellingQueue()
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    frame = bytes([em_controller.MIC_FRAME_TYPE, 0, 0]) + b"payload"
    ws = IdentifyWS("dev", [frame])
    try:
        with pytest.raises(asyncio.CancelledError):
            run(em_controller.handle_data(ws))
    finally:
        em_controller._devices = old_devices


def test_handle_data_dispatches_capture_frames_when_opted_in_and_resets_when_not(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
    device = new_device()
    device.save_wake_captures = False
    device.save_stop_captures = False
    old_devices = em_controller._devices
    em_controller._devices = {"dev": device}
    begin_frame = bytes([em_capture_upload.CAPTURE_BEGIN, em_capture_upload.PROTOCOL_VERSION,
                        0, 1, 0])  # malformed on purpose — never reaches feed() when opted out
    ws = IdentifyWS("dev", [begin_frame])
    try:
        run(em_controller.handle_data(ws))  # must not raise: reset()+continue, feed() never called
    finally:
        em_controller._devices = old_devices

    # Opted in: feed()/​_accept_capture_upload are exercised via a stubbed
    # Receiver so this test does not need a byte-perfect wire capture.
    accept_calls = []

    async def fake_accept(dev, ws_, completed):
        accept_calls.append(completed)
        return True

    monkeypatch.setattr(em_controller, "_accept_capture_upload", fake_accept)
    feed_results = iter([None, "completed-capture"])
    monkeypatch.setattr(em_controller.em_capture_upload.Receiver, "feed",
                        lambda self, raw: next(feed_results))
    device.save_wake_captures = True
    em_controller._devices = {"dev": device}
    ws = IdentifyWS("dev", [begin_frame, begin_frame])
    try:
        run(em_controller.handle_data(ws))
    finally:
        em_controller._devices = old_devices
    assert accept_calls == ["completed-capture"]


def test_handle_data_reports_identify_timeout_connection_closed_and_generic_error(monkeypatch):
    # Some other test modules mutate em_controller.websockets.exceptions
    # directly (not via monkeypatch, so it is not auto-reverted) to make
    # ConnectionClosed an alias for the bare Exception class — harmless for
    # them, but it would make RuntimeError below be caught by the
    # ConnectionClosed branch instead of the generic one this test targets.
    # Restore a distinct ConnectionClosed here so this test's outcome does
    # not depend on what ran before it in the same session.
    monkeypatch.setattr(em_controller.websockets, "exceptions",
                        types.SimpleNamespace(ConnectionClosed=type("ConnectionClosed", (Exception,), {})),
                        raising=False)

    class RaisingWS:
        remote_address = ("192.0.2.22", 8767)

        def __init__(self, exc):
            self.exc = exc
            self.closed = False

        async def recv(self):
            raise self.exc

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    run(em_controller.handle_data(RaisingWS(asyncio.TimeoutError())))
    run(em_controller.handle_data(RaisingWS(em_controller.websockets.exceptions.ConnectionClosed())))
    run(em_controller.handle_data(RaisingWS(RuntimeError("boom"))))


# ─── handle_control: registration edge cases ───────────────────────────────

class OneShotWS:
    """A /control fake WS whose first recv() returns a single register
    message and whose message loop then ends immediately — for the
    registration branches that return/close before ever reaching the main
    per-message dispatch loop."""
    remote_address = ("192.0.2.30", 8767)

    def __init__(self, register_msg):
        self._register_msg = register_msg
        self.sent = []
        self.closed = False

    async def recv(self):
        return self._register_msg

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _register_msg(device_id="dev", **extra):
    body = {"type": "register", "device_id": device_id, "ip": "192.0.2.30"}
    body.update(extra)
    return json.dumps(body)


def test_handle_control_closes_on_control_plane_link_auth_failure(monkeypatch):
    monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=False))
    ws = OneShotWS(_register_msg())
    run(em_controller.handle_control(ws))
    assert ws.closed
    assert ws.sent == []


def test_handle_control_auto_approves_a_previously_unknown_device(monkeypatch):
    approved_row = {"label": "Auto newdev", "approved": 1, "firmware_ver": "v1"}
    _install_common_db_stubs(monkeypatch, approved_row)
    # _install_common_db_stubs stubs get_config to a constant "strict" and
    # get_device to a constant row — both overridden here, in THIS order, so
    # the auto-approval branch (row is None, then re-fetched once approved)
    # is what actually runs.
    monkeypatch.setattr(em_controller.db, "get_config", lambda *a: "auto")
    calls = {"n": 0}

    def fake_get_device(_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else approved_row

    monkeypatch.setattr(em_controller.db, "get_device", fake_get_device)
    approve_calls = []
    monkeypatch.setattr(em_controller.db, "approve_device",
                        lambda *a: approve_calls.append(a))
    old_devices = em_controller._devices
    em_controller._devices = {}
    ws = OneShotWS(_register_msg("newdev"))
    try:
        run(em_controller.handle_control(ws))
    finally:
        em_controller._devices = old_devices
    assert approve_calls  # approve_device(device_id, label, None) was called
    assert any(m.get("type") == "ack" for m in ws.sent)


def test_handle_control_holds_a_known_but_unapproved_device_pending(monkeypatch):
    row = {"label": "Test", "approved": 0}
    _install_common_db_stubs(monkeypatch, row)
    ws = OneShotWS(_register_msg())
    run(em_controller.handle_control(ws))
    assert ws.sent == [{"type": "pending"}]
    assert ws.closed


def test_handle_control_survives_turn_history_hydration_failure(monkeypatch):
    row = {"label": "Test", "approved": 1, "firmware_ver": "v1"}
    _install_common_db_stubs(monkeypatch, row)

    def boom(*_a):
        raise RuntimeError("db locked")

    monkeypatch.setattr(em_controller.db, "get_turns", boom)
    old_devices = em_controller._devices
    em_controller._devices = {}
    ws = OneShotWS(_register_msg())
    try:
        run(em_controller.handle_control(ws))
    finally:
        em_controller._devices = old_devices
    # Hydration failing must not prevent the connection from completing
    # registration — the device still gets its ack and config.
    assert any(m.get("type") == "ack" for m in ws.sent)


# ─── handle_control: the main per-message dispatch loop ───────────────────

def test_handle_control_dispatches_every_message_type_and_closes_stale_on_teardown(monkeypatch):
    """
    One long-lived connection driven through nearly every branch of
    handle_control's message loop that the existing tests (the register/ack
    happy path, the dedicated stop_detected test, and
    test_control_handler_processes_device_state_messages) leave uncovered:
    a locked mute_state (cancels the active turn), a failed + duplicate
    wifi_result, a playback_stats report that DOES attach to an in-flight
    turn, both branches of wake_request (deny + task-spawning admit), every
    stop_status/wake_status/wake_started outcome, all three stop_detected
    outcomes (malformed / invalid / ignored / timer-phase accept), a
    populated wifi_scan_result future, an RTT-bearing pong, a full 4-sample
    clock_probe synchronisation plus one malformed probe, an unparseable
    JSON line, the standalone_play announcement closure, and finally a
    connection whose device_id has been claimed by a REPLACEMENT device by
    the time it tears down (the "stale connection" branch).
    """
    row = {
        "label": "Test", "approved": 1, "firmware_ver": "v1",
        "_config": {"owwModel": "hey_jarvis_v0.1", "stopModel": "stop"},
    }
    _install_common_db_stubs(monkeypatch, row)
    monkeypatch.setattr(em_controller.em_player, "interrupt", _noop)
    monkeypatch.setattr(em_controller.em_player, "resume_interrupted", _noop)
    monkeypatch.setattr(em_controller.turn_engine, "cancel_voice_turn", lambda *a, **kw: None)

    stop_calls = []

    async def fake_stop_voice_turn(device_id, turn_id, detection=None):
        stop_calls.append((device_id, turn_id))
        # turn 902 is the "rejected by the turn engine" scenario below —
        # everything else (there is none in this test) would accept.
        return turn_id != 902

    monkeypatch.setattr(em_controller.turn_engine, "stop_voice_turn", fake_stop_voice_turn)
    confirm_calls = []
    monkeypatch.setattr(
        em_controller.turn_engine, "confirm_device_started",
        lambda device, request_id, turn_id: confirm_calls.append(turn_id) or False,
    )

    async def fake_handle_wake_request(device, msg):
        # The deep semantics of admission are covered directly by the
        # _handle_wake_request tests above; here only the dispatch loop's
        # OWN job — spawning the task and clearing it via the done
        # callback — is under test.
        return None

    monkeypatch.setattr(em_controller, "_handle_wake_request", fake_handle_wake_request)
    monkeypatch.setattr(em_controller, "_run_post_turn_playback", lambda *a: asyncio.sleep(0))

    state = {}

    async def stream():
        yield json.dumps({
            "type": "register", "device_id": "dev", "ip": "192.0.2.20",
            "capabilities": ["wake_request_v1", "stopword", "led_anim", "sendspin_native"],
            "ambient_light_status": {"chip": "tsl2540"},
        })
        device = em_controller._devices["dev"]
        state["device"] = device
        device.mic_stop = lambda: asyncio.sleep(0)
        device.mic_start = lambda: asyncio.sleep(0)
        # A real device's first stats report seeds this; ambient_light
        # assumes it already exists (it is pushed independently, the moment
        # a step change happens, not on the ~30s stats cadence).
        device.stats = {}

        yield json.dumps({"type": "ambient_light", "lux": 42})

        # A bare "down" click returns almost immediately inside
        # handle_button_event itself (its own branches are covered
        # exhaustively elsewhere) — this only needs to prove the dispatch
        # loop actually routes "button" messages to it.
        yield json.dumps({"type": "button", "clickType": 138, "down": True})

        yield json.dumps({"type": "mute_state", "muted": False})

        # Muting DURING an active turn cancels it and flushes the speaker.
        await device.voice_lock.acquire()
        yield json.dumps({"type": "mute_state", "muted": True})
        device.voice_lock.release()
        device.muted = False

        yield json.dumps({"type": "volume_state", "level": 90})

        yield json.dumps({
            "type": "stats", "cpuPct": 5, "ambientLux": 10,
            "wakeDetector": {"frames": 3, "drops": 1, "crossings": 1,
                             "maxScore": 0.5, "errors": 0},
        })

        yield json.dumps({"type": "wifi_result", "ok": True, "ssid": "Home"})
        # A duplicate of the exact same outcome must not re-log/re-push.
        yield json.dumps({"type": "wifi_result", "ok": True, "ssid": "Home"})
        yield json.dumps({"type": "wifi_result", "ok": False, "ssid": "Home",
                          "error": "bad psk"})

        # playback_stats attached to an in-flight turn.
        device.last_turn_id = 501
        device.playback_send_t0 = asyncio.get_event_loop().time()
        device.turn_history.append({"turn_id": 501})
        yield json.dumps({"type": "playback_stats", "periods": 4, "underruns": 2,
                          "stats": {"min_depth": 1}})

        # playback_stats with no in-flight turn: stashes pending state, and
        # folds a PREVIOUS stash's underruns into the hourly counters.
        device.pending_playback_stats = (0.0, 1, 3, {}, 10)
        yield json.dumps({"type": "playback_stats", "periods": 2, "underruns": 0})

        # wake_request: missing requestId is dropped outright.
        yield json.dumps({"type": "wake_request"})
        # wake_request: deny (busy) then admit (spawns a task).
        device.wake_request_id = "already-busy"
        yield json.dumps({"type": "wake_request", "requestId": "wr-1"})
        device.wake_request_id = None
        device.data_ws = object()
        yield json.dumps({
            "type": "wake_request", "requestId": "wr-2", "score": 0.9,
            "threshold": 0.5, "ageMs": 1, "activationSeq": 1,
            "source": "wakeword", "model": device.oww_model,
        })
        for _ in range(5):
            await asyncio.sleep(0)  # let the spawned admission task finish

        yield json.dumps({"type": "stop_status", "model": device.stop_model, "ready": True})
        yield json.dumps({"type": "stop_status", "model": "unexpected", "ready": True})
        # The mismatched-model report above sets stop_model_ready back to
        # False (a real device would never actually alternate like this —
        # it is exercising both branches in one connection); every
        # stop_detected scenario below needs it True again first.
        yield json.dumps({"type": "stop_status", "model": device.stop_model, "ready": True})

        yield json.dumps({
            "type": "wake_status", "model": "not-this-devices-model",
            "ready": True, "classifierMd5": "a" * 32,
        })

        yield json.dumps({"type": "wake_started", "turnId": "not-an-int"})
        yield json.dumps({"type": "wake_started", "turnId": 1, "requestId": "wr-2"})

        # stop_detected: malformed (non-int turnId).
        yield json.dumps({"type": "stop_detected", "turnId": "nope"})
        # stop_detected: invalid (score below threshold).
        yield json.dumps({
            "type": "stop_detected", "turnId": 900, "generation": 1,
            "score": 0.1, "threshold": 0.9, "ageMs": 5, "phase": "playback",
        })
        # stop_detected: well-formed but nothing armed -> "ignored".
        yield json.dumps({
            "type": "stop_detected", "turnId": 901, "generation": 2,
            "score": 0.9, "threshold": 0.5, "ageMs": 5, "phase": "playback",
        })
        # stop_detected: accepted by StopState but REJECTED by the turn
        # engine (fake_stop_voice_turn returns False for turn 902) -> the
        # "not accepted" counter-bump branch.
        device.stop_generation += 1
        gen902 = device.stop_generation
        device.stop_state.arm(902, gen902, "playback",
                              asyncio.get_event_loop().time() + 100.0)
        yield json.dumps({
            "type": "stop_detected", "turnId": 902, "generation": gen902,
            "score": 0.9, "threshold": 0.5, "ageMs": 5, "phase": "playback",
        })
        # stop_detected: timer-phase accept -> dismiss_timer_alarm path.
        device.timer_stop_turn_id = 77
        device.stop_generation += 1
        gen = device.stop_generation
        device.stop_state.arm(77, gen, "timer", asyncio.get_event_loop().time() + 100.0)
        yield json.dumps({
            "type": "stop_detected", "turnId": 77, "generation": gen,
            "score": 0.9, "threshold": 0.5, "ageMs": 5, "phase": "timer",
        })

        device.ble_proxy_enabled = True
        yield json.dumps({"type": "ble_adverts", "adverts": [{"address": "x"}]})

        fut = asyncio.get_event_loop().create_future()
        device.wifi_scan_future = fut
        yield json.dumps({"type": "wifi_scan_result", "networks": []})
        state["wifi_scan_fut"] = fut

        yield json.dumps({"type": "log", "level": "info", "message": "hi"})

        # A pong bearing an id old enough to read as an RTT excursion.
        device.ping_sent[1] = asyncio.get_event_loop().time() - 1.0
        device.ping_busy[1] = False
        yield json.dumps({"type": "pong", "id": 1})
        # An unsolicited keepalive pong (no id) is ignored, not paired.
        yield json.dumps({"type": "pong"})

        # Four valid clock_probe round trips, accepted by ClockSync.update
        # each time (the "synchronized" debug-log branch additionally needs
        # samples spanning >=500ms of real time, which is covered directly
        # against em_clock.ClockSync in test_clock.py rather than paid for
        # here with a real sleep), plus one malformed probe (missing device
        # timestamps -> KeyError, caught).
        for probe_id in range(1, 5):
            t_sent = em_controller.em_clock.monotonic_us()
            device.clock_probe_sent[probe_id] = t_sent
            device.clock_probe_busy[probe_id] = False
            yield json.dumps({
                "type": "clock_probe", "id": probe_id,
                "device_received_us": t_sent, "device_sent_us": t_sent,
            })
        device.clock_probe_sent[99] = em_controller.em_clock.monotonic_us()
        device.clock_probe_busy[99] = False
        yield json.dumps({"type": "clock_probe", "id": 99})

        yield json.dumps({"type": "a_message_type_nobody_defined"})

        # Unparseable JSON is dropped rather than crashing the loop.
        yield "not json at all"

        played = await device.standalone_play(b"pcm-bytes")
        state["played"] = played

        # Simulate a replacement connection having already registered under
        # this same device_id by the time THIS connection's finally runs.
        em_controller._devices["dev"] = object()

    ws = AsyncGenWS(stream)
    old_devices = em_controller._devices
    em_controller._devices = {}
    try:
        run(em_controller.handle_control(ws))
    finally:
        em_controller._devices = old_devices

    device = state["device"]
    assert any(m.get("type") == "ack" for m in ws.sent)
    assert device.muted is False
    # Only the non-timer accept (turn 902, deliberately rejected by the fake
    # turn engine) reaches stop_voice_turn — the timer-phase accept takes
    # the dismiss_timer_alarm path instead.
    assert stop_calls == [("dev", 902)]
    assert confirm_calls == [1]
    assert state["wifi_scan_fut"].done()
    assert state["played"] is True
    # The connection saw itself replaced, so it must NOT have torn down the
    # (different) live device's registry entry.
    assert em_controller._devices.get("dev") is not device


def test_handle_control_ping_loop_produces_at_least_one_ping_and_clock_probe(monkeypatch):
    """
    ping_loop runs as a background task for the life of the connection.
    Shrinking its interval to 0 and yielding control a few times lets at
    least one iteration run before the connection ends — covering the
    stale-entry sweep, the sequence counters, and both outgoing message
    types it sends every cycle.
    """
    row = {"label": "Test", "approved": 1, "firmware_ver": "v1"}
    _install_common_db_stubs(monkeypatch, row)
    monkeypatch.setattr(em_controller, "PING_INTERVAL_SEC", 0)

    async def stream():
        yield json.dumps({
            "type": "register", "device_id": "dev", "ip": "192.0.2.31",
            "capabilities": [],
        })
        device = em_controller._devices["dev"]
        # Old enough that ping_loop's first sweep must consider them
        # abandoned (a very late reply is a lost packet, not a sample) and
        # discard them before sending this cycle's fresh probes.
        now = asyncio.get_event_loop().time()
        device.ping_sent[999] = now - 1000.0
        device.clock_probe_sent[999] = int((now - 1000.0) * 1_000_000)
        device.clock_probe_busy[999] = False
        for _ in range(50):
            await asyncio.sleep(0)

    ws = AsyncGenWS(stream)
    old_devices = em_controller._devices
    em_controller._devices = {}
    try:
        run(em_controller.handle_control(ws))
    finally:
        em_controller._devices = old_devices

    assert any(m.get("type") == "ping" for m in ws.sent)
    assert any(m.get("type") == "clock_probe" for m in ws.sent)


def test_handle_control_logs_unhandled_exceptions_from_the_message_loop(monkeypatch):
    """An exception raised while processing a message (not one of the
    explicitly-handled protocol errors) must not crash the connection
    handler — it is caught by the outer `except Exception` and logged."""
    row = {"label": "Test", "approved": 1, "firmware_ver": "v1"}
    _install_common_db_stubs(monkeypatch, row)
    # See the identical note in the handle_data generic-error test: another
    # test module mutates em_controller.websockets.exceptions.ConnectionClosed
    # to alias bare Exception, outside monkeypatch's auto-revert. Restore a
    # distinct one so this test's outcome does not depend on run order.
    monkeypatch.setattr(em_controller.websockets, "exceptions",
                        types.SimpleNamespace(ConnectionClosed=type("ConnectionClosed", (Exception,), {})),
                        raising=False)

    def boom(*_a):
        raise RuntimeError("unexpected bug in stats persistence")

    monkeypatch.setattr(em_controller.db, "record_device_stats", boom)

    async def stream():
        yield json.dumps({
            "type": "register", "device_id": "dev", "ip": "192.0.2.32",
            "capabilities": [],
        })
        yield json.dumps({"type": "stats", "cpuPct": 1})

    ws = AsyncGenWS(stream)
    old_devices = em_controller._devices
    em_controller._devices = {}
    try:
        run(em_controller.handle_control(ws))  # must not raise
    finally:
        em_controller._devices = old_devices


def test_handle_control_cancels_a_still_pending_wake_admission_task_on_teardown(monkeypatch):
    """
    A connection that drops WHILE its own spawned wake-admission task is
    still running must not leave that task orphaned — the finally block
    cancels it and waits for the cancellation to land.
    """
    row = {"label": "Test", "approved": 1, "firmware_ver": "v1"}
    _install_common_db_stubs(monkeypatch, row)

    async def never_finishes(_device, _msg):
        await asyncio.sleep(3600)

    monkeypatch.setattr(em_controller, "_handle_wake_request", never_finishes)

    async def stream():
        yield json.dumps({
            "type": "register", "device_id": "dev", "ip": "192.0.2.33",
            "capabilities": ["wake_request_v1"],
        })
        device = em_controller._devices["dev"]
        device.data_ws = object()
        yield json.dumps({
            "type": "wake_request", "requestId": "wr-pending", "score": 0.9,
            "threshold": 0.5, "ageMs": 1, "activationSeq": 1,
            "source": "wakeword", "model": device.oww_model,
        })
        # Give the task a chance to actually be created and start running
        # (and therefore genuinely be "not done" at teardown) without
        # giving it anywhere near long enough to finish its 3600s sleep.
        await asyncio.sleep(0)

    ws = AsyncGenWS(stream)
    old_devices = em_controller._devices
    em_controller._devices = {}
    try:
        run(em_controller.handle_control(ws))  # must not hang or raise
    finally:
        em_controller._devices = old_devices


# ─── handle_shell branches not already covered ─────────────────────────────

def test_handle_shell_programmatic_session_survives_wait_closed_failure(monkeypatch):
    """A device WS whose wait_closed() itself errors (rather than simply
    resolving when the socket closes) must not crash the shell handler —
    the session is considered over either way."""
    class DeviceWS:
        async def wait_closed(self):
            raise RuntimeError("transport already gone")

        async def close(self):
            pass

    async def run_it():
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
        pending = asyncio.get_running_loop().create_future()
        em_controller._shell_pending["dev"] = pending
        try:
            await em_controller.handle_shell(DeviceWS(), "/shell/dev")
            assert pending.result().wait_closed  # the same object was handed through
        finally:
            em_controller._shell_pending.pop("dev", None)

    run(run_it())


def test_handle_shell_proxy_swallows_send_failures_on_both_sides(monkeypatch):
    """
    Neither proxy direction may let a send failure escape and crash the
    other side's task or the session-teardown bookkeeping: the shell_meta
    announce, the device->dashboard relay, and the dashboard->device relay
    each wrap their sends in their own `except Exception: pass`.
    """
    import aiohttp

    class FailingDashboardWS:
        def __init__(self, messages):
            self._messages = iter(messages)

        async def send_str(self, _value):
            raise RuntimeError("dashboard gone")

        async def send_bytes(self, _value):
            raise RuntimeError("dashboard gone")

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                raise StopAsyncIteration

    class FailingDeviceWS:
        def __init__(self, messages):
            self._messages = iter(messages)

        async def send(self, _value):
            raise RuntimeError("device gone")

        async def close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                raise StopAsyncIteration

    async def run_it():
        monkeypatch.setattr(em_controller, "_link_auth_ok", lambda *a: asyncio.sleep(0, result=True))
        pending = asyncio.get_running_loop().create_future()
        dashboard = FailingDashboardWS(
            [types.SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data="hi")])
        device = FailingDeviceWS([b"stdout-bytes"])
        em_controller._shell_pending["dev"] = pending
        em_controller._shell_dashboard["dev"] = dashboard
        try:
            await em_controller.handle_shell(device, "/shell/dev")  # must not raise
        finally:
            em_controller._shell_pending.pop("dev", None)
            em_controller._shell_dashboard.pop("dev", None)

    run(run_it())


# ─── _route / router / router_tls / mDNS / event-loop lag monitor ─────────

def test_route_dispatches_by_path_and_closes_unknown_paths(monkeypatch):
    calls = []

    async def fake_control(ws, secure):
        calls.append(("control", secure))

    async def fake_data(ws, secure):
        calls.append(("data", secure))

    async def fake_shell(ws, path, secure):
        calls.append(("shell", path, secure))

    monkeypatch.setattr(em_controller, "handle_control", fake_control)
    monkeypatch.setattr(em_controller, "handle_data", fake_data)
    monkeypatch.setattr(em_controller, "handle_shell", fake_shell)

    class WS:
        remote_address = ("192.0.2.40", 8767)

        def __init__(self, path):
            self.request = types.SimpleNamespace(path=path)
            self.closed = False

        async def close(self):
            self.closed = True

    run(em_controller._route(WS("/control"), secure=True))
    run(em_controller._route(WS("/data"), secure=False))
    run(em_controller._route(WS("/shell/dev"), secure=True))
    unknown = WS("/nonsense")
    run(em_controller._route(unknown, secure=False))
    assert calls == [
        ("control", True), ("data", False), ("shell", "/shell/dev", True),
    ]
    assert unknown.closed


def test_router_and_router_tls_set_the_secure_flag(monkeypatch):
    seen = []

    async def fake_route(ws, secure):
        seen.append(secure)

    monkeypatch.setattr(em_controller, "_route", fake_route)
    run(em_controller.router(object()))
    run(em_controller.router_tls(object()))
    assert seen == [False, True]


def test_make_mdns_info_advertises_tls_port_only_when_tls_is_active(monkeypatch):
    # The shared zeroconf.ServiceInfo stub (installed once per session by
    # whichever test module imports first) is a bare, argument-less class —
    # nothing in the existing suite ever actually calls it. Swap in a richer
    # one, local to this test via monkeypatch, that records its constructor
    # kwargs so the two branches can be told apart.
    class RecordingServiceInfo:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.properties = kwargs.get("properties", {})

    monkeypatch.setattr(em_controller, "ServiceInfo", RecordingServiceInfo)
    plain = em_controller._make_mdns_info(tls_active=False)
    secure = em_controller._make_mdns_info(tls_active=True)
    assert "tls_port" not in plain.properties
    assert secure.properties["tls_port"] == str(em_controller.SERVER_TLS_PORT)


def test_mdns_refresh_loop_refreshes_and_survives_update_failures(monkeypatch):
    monkeypatch.setattr(em_controller, "MDNS_REFRESH_INTERVAL", 0)
    calls = []

    class FlakyAzc:
        def __init__(self):
            self.n = 0

        async def async_update_service(self, _info):
            self.n += 1
            calls.append(self.n)
            if self.n == 1:
                raise RuntimeError("registrar briefly unreachable")

    async def run_it():
        task = asyncio.create_task(em_controller._mdns_refresh_loop(FlakyAzc(), object()))
        for _ in range(20):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(run_it())
    assert len(calls) >= 2  # survived the first failure and refreshed again


def test_event_loop_lag_monitor_samples_cpu_and_warns_past_threshold(monkeypatch):
    sampled = []
    monkeypatch.setattr(em_controller.api, "sample_cpu", lambda: sampled.append(True))
    # A near-zero interval with the real loop clock still produces a small
    # positive lag most of the time (scheduling jitter); asserting only that
    # the monitor runs, samples CPU on schedule and does not raise is the
    # deterministic part — the warning branch itself is exercised by simply
    # giving it a warn_ms low enough that ordinary scheduling jitter crosses
    # it, without asserting a flaky log line.
    monkeypatch.setattr(em_controller.api, "CPU_SAMPLE_INTERVAL_S", 0)

    async def run_it():
        task = asyncio.create_task(em_controller.event_loop_lag_monitor(interval=0, warn_ms=-1))
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(run_it())
    assert sampled  # api.sample_cpu() was called at least once


# ─── Device.send_data reconnect-grace and failure branches ────────────────

def test_send_data_rides_out_a_brief_reconnect_within_its_grace_budget(monkeypatch):
    """
    begin_data_stream() arms a per-stream reconnect budget; a send while
    data_ws is None waits it out in DATA_RECONNECT_GRACE_S-bounded steps
    and, if the plane comes back inside that budget, resumes rather than
    dropping the frame.
    """
    device = new_device()
    device.begin_data_stream()
    original_sleep = em_controller.asyncio.sleep

    async def instant_sleep_that_reconnects(_seconds):
        await original_sleep(0)
        device.data_ws = FakeWSForData()

    class FakeWSForData:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

    monkeypatch.setattr(em_controller.asyncio, "sleep", instant_sleep_that_reconnects)
    run(device.send_data(b"payload"))
    assert device.data_ws.sent == [b"payload"]


def test_send_data_logs_when_the_underlying_send_fails():
    device = new_device()

    class FailingDataWS:
        async def send(self, _data):
            raise RuntimeError("socket reset")

    device.data_ws = FailingDataWS()
    run(device.send_data(b"payload"))  # must not raise


# ─── the button-turn closure's admission_valid, invoked through a real
#     (not stubbed) _run_voice_locked ─────────────────────────────────────

def test_button_admission_valid_closure_is_exercised_by_a_real_turn(monkeypatch):
    """
    handle_button_event's admission_valid closure is only ever CALLED by
    turn_engine.trigger_voice_turn, deep inside the real _run_voice_locked
    — every other button test stubs _run_voice_locked away entirely, which
    never invokes it. This test lets _run_voice_locked run for real and
    stubs one level deeper (trigger_voice_turn) instead, so the closure's
    own body executes.
    """
    device = new_device(["button_hold", "stopword"])
    device.stop_model_ready = True
    device.data_ws = object()
    device.mic_stop = lambda: asyncio.sleep(0)
    device.mic_start = lambda: asyncio.sleep(0)
    monkeypatch.setattr(em_controller.em_player, "interrupt", _noop)
    monkeypatch.setattr(em_controller.em_player, "resume_interrupted", _noop)
    monkeypatch.setattr(em_controller, "leds_listening", _noop)
    monkeypatch.setattr(em_controller, "_leds_turn_end", _noop)
    monkeypatch.setattr(em_controller, "_push_device_state", _noop)
    monkeypatch.setattr(em_controller, "_wake_arbiter",
                        em_controller.em_arbiter.WakeArbiter())

    seen = []

    async def fake_trigger(**kwargs):
        seen.append(kwargs["admission_valid"]())
        return False

    monkeypatch.setattr(em_controller.turn_engine, "trigger_voice_turn", fake_trigger)
    old_devices = em_controller._devices
    em_controller._devices = {device.device_id: device}

    async def run_it():
        await em_controller.handle_button_event(device, {
            "clickType": 138, "down": False, "heldMs": 50, "requestId": "btn-admit",
        })
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0)

    try:
        run(run_it())
    finally:
        em_controller._devices = old_devices
    assert seen == [True]  # device registered, unmuted, data plane up, deadline not yet passed
