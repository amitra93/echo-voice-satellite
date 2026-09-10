import asyncio
import importlib
import inspect
import sys
import types

import pytest


from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.components.assist_satellite import (
    AssistSatelliteEntity,
    AssistSatelliteEntityFeature,
)

module = importlib.import_module("custom_components.echo_voice_satellite.assist_satellite")
from custom_components.echo_voice_satellite.client import ControllerError  # noqa: E402
from custom_components.echo_voice_satellite.tts_stream import TTSIncompatible  # noqa: E402


# ── Structural / contract tests (kept from the original pass) ──────────────

def test_only_announce_is_advertised_not_start_conversation():
    entity = module.EchoAssistSatellite
    instance = object.__new__(entity)
    assert instance.supported_features == AssistSatelliteEntityFeature.ANNOUNCE


def test_announce_and_pipeline_event_signatures_are_stable():
    entity = module.EchoAssistSatellite
    assert list(inspect.signature(entity.async_announce).parameters) == ["self", "announcement"]
    assert list(inspect.signature(entity.on_pipeline_event).parameters) == ["self", "event"]


def test_no_start_conversation_method_is_defined():
    assert "async_start_conversation" not in module.EchoAssistSatellite.__dict__


def test_timer_speech_offer_ends_pipeline_at_stt():
    source = inspect.getsource(module.EchoAssistSatellite._run_wake_pipeline)
    assert "timer_speech" in source
    assert "end_stage=PipelineStage.STT" in source


# ── Behavioural tests — object.__new__'d against the real AssistSatelliteEntity
# base class. super().async_accept_pipeline_from_satellite is monkeypatched on
# the base class for the duration of a test (never on the instance — it's
# invoked via super(), which resolves through the CLASS's MRO, not an
# instance attribute), auto-restored by pytest's monkeypatch fixture. ──────

class _FakeClient:
    def __init__(self):
        self.calls = []
        self.turn_action_error_on = set()  # {(turn_id, action)} to raise ControllerError
        self.created_turn = {"turn_id": 42, "data": {"audio_url": "ws://x"}}
        self.attached_channel = object()
        self.attach_should_fail = False

    async def async_turn_action(self, turn_id, action, data=None):
        self.calls.append(("turn_action", turn_id, action, data))
        if (turn_id, action) in self.turn_action_error_on:
            raise ControllerError("turn_not_found")
        return {"turn_id": turn_id, "action": action}

    async def async_create_turn(self, device_id, kind):
        self.calls.append(("create_turn", device_id, kind))
        return self.created_turn

    async def async_attach_audio(self, turn_id):
        self.calls.append(("attach_audio", turn_id))
        if self.attach_should_fail:
            raise ControllerError("audio_unavailable")
        return self.attached_channel

    async def async_timer_event(self, device_id, event):
        self.calls.append(("timer_event", device_id, event))
        return {"accepted": True}


class _FakeCoordinator:
    def __init__(self, client, muted=False, connected=True):
        self.client = client
        self._record = {"muted": muted, "connected": connected}
        self.control_available = True
        self.last_update_success = True
        self.data = {"devices": [{"device_id": "A", **self._record}]}

    def async_add_event_listener(self, callback):
        return lambda: None


class _FakeHass:
    def __init__(self):
        self.created_tasks = []
        self.data = {}

    def async_create_task(self, coro, name=None):
        task = asyncio.ensure_future(coro)
        self.created_tasks.append(task)
        return task


def _make_satellite(client=None, muted=False):
    client = client or _FakeClient()
    coordinator = _FakeCoordinator(client, muted=muted)
    entity = object.__new__(module.EchoAssistSatellite)
    entity.coordinator = coordinator
    entity.client = client
    entity.device_id = "A"
    entity.hass = _FakeHass()
    entity._active_turn_id = None
    entity._active_channel = None
    entity._active_conversation_id = None
    entity._active_turn_token = object()
    entity._tts_task = None
    entity._tts_turn_token = None
    entity._pipeline_task = None
    entity._offer_lock = asyncio.Lock()
    entity._transcript_sent = False
    entity._endpoint_sent = False
    entity._continue_conversation = False
    entity._tool_calls = {}
    entity._tool_call_sequence = 0
    entity._tool_trace_lock = asyncio.Lock()
    entity._timer_card_hub = None
    entity._timer_queue = asyncio.Queue()
    entity._timer_worker = None
    entity._timer_forwarding_closed = False
    entity._event_remove = lambda: None
    entity._chat_log_unsubscribe = None
    entity._attr_unique_id = "A_assist_satellite"
    entity.async_write_ha_state = lambda: None
    return entity, client, coordinator


def test_is_on_reflects_whether_a_turn_is_active():
    entity, _client, _coord = _make_satellite()
    assert entity.is_on is False
    entity._active_turn_id = 7
    assert entity.is_on is True


def test_available_is_false_while_muted():
    entity, _client, _coord = _make_satellite(muted=True)
    entity.last_update_success = True
    assert entity.available is False


def test_available_is_true_when_connected_and_not_muted():
    entity, _client, _coord = _make_satellite(muted=False)
    assert entity.available is True


def test_timer_event_is_forwarded_with_timer_metadata():
    entity, client, _coord = _make_satellite()
    timer = type("Timer", (), {
        "id": "01J", "device_id": "ha-device", "name": "pizza",
        "created_seconds": 600, "seconds_left": 425, "is_active": True,
    })()
    event = type("Event", (), {"value": "updated"})()

    asyncio.run(entity._async_forward_timer_event({
        "event": event.value, "timer_id": timer.id, "ha_device_id": timer.device_id,
        "name": timer.name, "total_seconds": timer.created_seconds,
        "seconds_left": timer.seconds_left, "is_active": timer.is_active,
    }))

    assert client.calls[-1] == (
        "timer_event", "A", {
            "event": "updated", "timer_id": "01J", "ha_device_id": "ha-device",
            "name": "pizza", "total_seconds": 600, "seconds_left": 425,
            "is_active": True,
        },
    )


@pytest.mark.parametrize("event_name", ["started", "updated", "cancelled", "finished"])
def test_all_timer_lifecycle_events_are_forwarded(event_name):
    entity, client, _coord = _make_satellite()
    timer = type("Timer", (), {
        "id": "01J", "device_id": "ha-device", "name": None,
        "created_seconds": 60, "seconds_left": 12, "is_active": event_name != "cancelled",
    })()
    event = type("Event", (), {"value": event_name})()

    asyncio.run(entity._async_forward_timer_event({
        "event": event.value, "timer_id": timer.id, "ha_device_id": timer.device_id,
        "name": timer.name, "total_seconds": timer.created_seconds,
        "seconds_left": timer.seconds_left, "is_active": timer.is_active,
    }))

    assert client.calls[-1][0:2] == ("timer_event", "A")
    assert client.calls[-1][2]["event"] == event_name


def test_delayed_command_timer_events_are_never_forwarded_to_the_controller():
    """A conversation-command timer ('in 5 minutes turn off the lights') is
    an HA-native automation, not an audible reminder. TimerManager still
    invokes the handler for it, so the filter has to live here, at the point
    closest to that call.
    """
    entity, client, _coord = _make_satellite()
    timer = type("Timer", (), {
        "id": "01J", "device_id": "ha-device", "name": None,
        "created_seconds": 300, "seconds_left": 0, "is_active": False,
        "conversation_command": "turn off the kitchen lights",
    })()
    event = type("Event", (), {"value": "finished"})()

    entity._timer_event(event, timer)

    assert entity.hass.created_tasks == []
    assert client.calls == []


def test_ordinary_timer_events_are_still_forwarded_via_timer_event():
    async def run():
        entity, client, _coord = _make_satellite()
        timer = type("Timer", (), {
            "id": "01J", "device_id": "ha-device", "name": "pizza",
            "created_seconds": 600, "seconds_left": 0, "is_active": False,
            "conversation_command": None,
        })()
        event = type("Event", (), {"value": "finished"})()

        entity._timer_event(event, timer)
        assert len(entity.hass.created_tasks) == 1
        await asyncio.sleep(0)
        await entity._async_stop_timer_forwarding()

        assert client.calls[-1][0:2] == ("timer_event", "A")
        assert client.calls[-1][2]["event"] == "finished"

    asyncio.run(run())


def test_timer_event_notifies_the_registered_timer_card_hub():
    """TimerManager has already mutated its own state by the time it calls
    _timer_event — that call IS the notification — so the card's
    subscribers should hear about it immediately, not only once the
    (separately timed) controller forwarding round trip completes."""
    async def run():
        from custom_components.echo_voice_satellite.const import DOMAIN

        entity, _client, _coord = _make_satellite()
        notified = []
        entity.hass.data[DOMAIN] = {"entry-1": {"timer_card": object()}}
        entity._timer_card_hub = types.SimpleNamespace(
            notify_manager_change=lambda: notified.append(True),
        )
        timer = type("Timer", (), {
            "id": "01J", "device_id": "ha-device", "name": "pizza",
            "created_seconds": 600, "seconds_left": 0, "is_active": False,
            "conversation_command": None,
        })()

        entity._timer_event(type("Event", (), {"value": "finished"})(), timer)
        await asyncio.sleep(0)
        await entity._async_stop_timer_forwarding()

        assert notified == [True]

    asyncio.run(run())


def test_delayed_command_timer_events_never_notify_the_timer_card_hub_either():
    from custom_components.echo_voice_satellite.const import DOMAIN

    entity, _client, _coord = _make_satellite()
    notified = []
    entity.hass.data[DOMAIN] = {"entry-1": {"timer_card": object()}}
    entity._timer_card_hub = types.SimpleNamespace(
        notify_manager_change=lambda: notified.append(True),
    )
    timer = type("Timer", (), {
        "id": "01J", "device_id": "ha-device", "name": None,
        "created_seconds": 300, "seconds_left": 0, "is_active": False,
        "conversation_command": "turn off the kitchen lights",
    })()

    entity._timer_event(type("Event", (), {"value": "finished"})(), timer)

    assert notified == []


def test_timer_event_forwarding_logs_controller_failure_without_raising(caplog):
    entity, client, _coord = _make_satellite()
    timer = type("Timer", (), {
        "id": "01J", "device_id": "ha-device", "name": "pizza",
        "created_seconds": 60, "seconds_left": 12, "is_active": True,
    })()
    client.async_timer_event = _raise_controller_error

    with caplog.at_level("ERROR"):
        asyncio.run(entity._async_forward_timer_event({
            "event": "updated", "timer_id": timer.id,
            "ha_device_id": timer.device_id, "name": timer.name,
            "total_seconds": timer.created_seconds,
            "seconds_left": timer.seconds_left, "is_active": timer.is_active,
        }))

    assert "Failed to forward timer 01J" in caplog.text


def test_timer_lifecycle_forwarding_is_fifo_and_snapshots_mutable_timer_state():
    async def run():
        entity, client, _coord = _make_satellite()
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        delivered = []
        first = True

        async def delayed_post(device_id, payload):
            nonlocal first
            if first:
                first = False
                first_started.set()
                await release_first.wait()
            delivered.append((device_id, payload))
            return {"accepted": True}

        client.async_timer_event = delayed_post
        timer = type("Timer", (), {
            "id": "01J", "device_id": "ha-device", "name": "pizza",
            "created_seconds": 60, "seconds_left": 60, "is_active": True,
            "conversation_command": None,
        })()

        entity._timer_event(type("Event", (), {"value": "started"})(), timer)
        await first_started.wait()
        timer.seconds_left, timer.is_active = 42, True
        entity._timer_event(type("Event", (), {"value": "updated"})(), timer)
        timer.seconds_left, timer.is_active = 0, False
        entity._timer_event(type("Event", (), {"value": "finished"})(), timer)

        release_first.set()
        for _ in range(10):
            if len(delivered) == 3:
                break
            await asyncio.sleep(0)
        await entity._async_stop_timer_forwarding()

        assert [payload["event"] for _device_id, payload in delivered] == [
            "started", "updated", "finished",
        ]
        assert [payload["seconds_left"] for _device_id, payload in delivered] == [60, 42, 0]
        assert [payload["is_active"] for _device_id, payload in delivered] == [True, True, False]

    asyncio.run(run())


def test_timer_forwarding_stops_before_entity_unload_finishes(monkeypatch):
    async def run():
        entity, client, _coord = _make_satellite()
        first_started = asyncio.Event()
        delivered = []

        async def blocked_post(_device_id, payload):
            first_started.set()
            await asyncio.Event().wait()
            delivered.append(payload)

        async def base_remove(_self):
            return None

        monkeypatch.setattr(
            AssistSatelliteEntity, "async_will_remove_from_hass", base_remove,
            raising=False,
        )
        client.async_timer_event = blocked_post
        timer = type("Timer", (), {
            "id": "01J", "device_id": "ha-device", "name": "pizza",
            "created_seconds": 60, "seconds_left": 60, "is_active": True,
            "conversation_command": None,
        })()
        entity._timer_event(type("Event", (), {"value": "started"})(), timer)
        await first_started.wait()
        entity._timer_event(type("Event", (), {"value": "updated"})(), timer)

        await entity.async_will_remove_from_hass()
        entity._timer_event(type("Event", (), {"value": "finished"})(), timer)
        await asyncio.sleep(0)

        assert entity._timer_forwarding_closed is True
        assert entity._timer_worker is None
        assert delivered == []

    asyncio.run(run())


def test_timer_event_notifies_only_its_own_controller_hub():
    async def run():
        entity, _client, _coord = _make_satellite()
        first, second = [], []
        entity._timer_card_hub = types.SimpleNamespace(
            notify_manager_change=lambda: first.append(True),
        )
        entity.hass.data["echo_voice_satellite"] = {
            "entry-1": {"timer_card": entity._timer_card_hub},
            "entry-2": {"timer_card": types.SimpleNamespace(
                notify_manager_change=lambda: second.append(True),
            )},
        }
        timer = type("Timer", (), {
            "id": "01J", "device_id": "ha-device", "name": "pizza",
            "created_seconds": 60, "seconds_left": 0, "is_active": False,
            "conversation_command": None,
        })()

        entity._timer_event(type("Event", (), {"value": "finished"})(), timer)
        await asyncio.sleep(0)
        await entity._async_stop_timer_forwarding()

        assert first == [True]
        assert second == []

    asyncio.run(run())


async def _raise_controller_error(*_args, **_kwargs):
    raise ControllerError("controller_unreachable")


def test_added_to_hass_registers_timer_handler_for_registry_device(monkeypatch):
    entity, _client, _coord = _make_satellite()
    registered = []
    removed = []

    async def base_added(_self):
        return None

    monkeypatch.setattr(AssistSatelliteEntity, "async_added_to_hass", base_added, raising=False)
    entity.async_on_remove = lambda callback: removed.append(callback)

    registry = types.SimpleNamespace(
        async_get_device=lambda identifiers: types.SimpleNamespace(id="ha-device-1")
    )
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.async_get = lambda hass: registry
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "device_registry", device_registry, raising=False)

    intent = types.ModuleType("homeassistant.components.intent")

    def register(hass, device_id, handler):
        registered.append((hass, device_id, handler))
        return lambda: removed.append("unregistered")

    intent.async_register_timer_handler = register
    monkeypatch.setitem(sys.modules, "homeassistant.components.intent", intent)

    asyncio.run(entity.async_added_to_hass())

    assert registered == [(entity.hass, "ha-device-1", entity._timer_event)]
    assert len(removed) == 1
    removed[0]()
    assert removed[-1] == "unregistered"


def test_tts_response_finished_reaches_the_base_class_and_returns_to_idle():
    """AssistSatelliteEntity.tts_response_finished() is what actually
    transitions state out of RESPONDING (_set_state(IDLE), @final on the
    base class). This override used to replace it with a bare
    async_write_ha_state() and drop that transition entirely — every
    real turn left the entity permanently reporting "responding" once the
    first TTS response finished, since nothing else in this module ever
    calls _set_state (observed live on hardware, 2026-08-18)."""
    from homeassistant.components.assist_satellite.entity import AssistSatelliteState

    entity, _client, _coord = _make_satellite()
    entity._set_state(AssistSatelliteState.RESPONDING)
    assert entity.state == AssistSatelliteState.RESPONDING  # sanity: fixture works

    entity.tts_response_finished()

    assert entity.state == AssistSatelliteState.IDLE


def test_gateway_event_ignores_wake_offer_for_a_different_device():
    entity, client, _coord = _make_satellite()
    handled = []

    async def fake_handle(offer):
        handled.append(offer)

    entity._handle_wake_offer = fake_handle

    async def run():
        await entity._async_gateway_event(
            {"type": "wake.offer", "device_id": "OTHER", "turn_id": 1, "trigger": "wakeword"}
        )
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert handled == []


def test_gateway_event_spawns_wake_offer_handling_for_this_device():
    entity, client, _coord = _make_satellite()
    handled = []

    async def fake_handle(offer):
        handled.append(offer)

    entity._handle_wake_offer = fake_handle
    offer = {"type": "wake.offer", "device_id": "A", "turn_id": 3, "trigger": "wakeword(0.9)"}

    async def run():
        await entity._async_gateway_event(offer)
        await asyncio.sleep(0.05)  # let the spawned task run

    asyncio.run(run())
    assert handled == [offer]


def test_turn_terminal_for_active_turn_cancels_tts_and_closes_channel():
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 5
    closed = []

    class FakeChannel:
        async def close(self):
            closed.append(True)

    entity._active_channel = FakeChannel()

    async def never_ends():
        await asyncio.sleep(100)

    async def run():
        task = asyncio.ensure_future(never_ends())
        entity._tts_task = task
        await entity._async_gateway_event({"type": "turn.terminal", "turn_id": 5, "device_id": "A"})
        return task

    task = asyncio.run(run())
    assert task.cancelled()
    assert entity._tts_task is None
    assert entity._active_channel is None
    assert entity._active_turn_id is None
    assert closed == [True]


@pytest.mark.parametrize("outcome", ["cancelled", "muted", "barged"])
def test_turn_terminal_ends_the_active_hacs_turn_for_each_early_end_cause(outcome):
    entity, _client, _coord = _make_satellite()
    entity._active_turn_id = 5
    closed = []

    class FakeChannel:
        async def close(self):
            closed.append(True)

    entity._active_channel = FakeChannel()
    asyncio.run(entity._async_gateway_event({
        "type": "turn.terminal", "turn_id": 5, "device_id": "A", "outcome": outcome,
    }))

    assert entity._active_turn_id is None
    assert entity._active_channel is None
    assert closed == [True]


def test_turn_terminal_for_a_different_turn_is_ignored():
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 5
    entity._active_channel = object()

    asyncio.run(entity._async_gateway_event(
        {"type": "turn.terminal", "turn_id": 999, "device_id": "A"}
    ))

    assert entity._active_turn_id == 5  # untouched


def test_handle_wake_offer_attaches_audio_and_spawns_pipeline(monkeypatch):
    entity, client, _coord = _make_satellite()
    started = []
    monkeypatch.setattr(
        asyncio, "create_task",
        lambda coro, **kw: started.append(coro) or coro.close(),
    )

    asyncio.run(entity._handle_wake_offer({"turn_id": 9, "device_id": "A"}))

    assert entity._active_turn_id == 9
    assert entity._active_channel is client.attached_channel
    assert ("attach_audio", 9) in client.calls
    assert any(call[:3] == ("turn_action", 9, "accept") for call in client.calls)
    assert len(started) == 1


def test_handle_wake_offer_swallows_attach_failure_without_setting_active_turn():
    client = _FakeClient()
    client.attach_should_fail = True
    entity, client, _coord = _make_satellite(client=client)

    asyncio.run(entity._handle_wake_offer({"turn_id": 9, "device_id": "A"}))

    assert entity._active_turn_id is None


def test_handle_wake_offer_rejects_a_second_turn_while_one_is_active():
    client = _FakeClient()
    entity, _client, _coordinator = _make_satellite(client)
    entity._active_turn_id = 5

    asyncio.run(entity._handle_wake_offer({"turn_id": 9, "device_id": "A"}))

    assert ("turn_action", 9, "reject", None) in client.calls
    assert not any(call[:2] == ("attach_audio", 9) for call in client.calls)
    assert entity._active_turn_id == 5
    assert entity._active_channel is None


# ── _run_wake_pipeline — the load-bearing "always resolve the TTS side"
# guarantee (see assist_satellite.py's module docstring). ──────────────────

class _FakeAudioChannel:
    """channel.mic_frames() must succeed — _run_wake_pipeline calls it eagerly
    before super().async_accept_pipeline_from_satellite(...) even runs, so a
    bare object() here would raise AttributeError and get silently swallowed
    by _run_wake_pipeline's own broad except — masking whether the method
    under test actually reached the code path being asserted on."""

    def mic_frames(self):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def test_run_wake_pipeline_no_transcript_endpoints_before_tts_end(monkeypatch):
    """Speech-start timeout has no STT_END, so finally must end the mic side."""
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 9
    channel = _FakeAudioChannel()
    entity._active_channel = channel

    async def fake_accept(self, mic_frames):
        return None  # pipeline ran and produced nothing spoken

    monkeypatch.setattr(AssistSatelliteEntity, "async_accept_pipeline_from_satellite", fake_accept)

    asyncio.run(entity._run_wake_pipeline(channel))

    assert client.calls == [
        ("turn_action", 9, "endpoint", None),
        ("turn_action", 9, "tts/end", None),
    ]


def test_run_wake_pipeline_awaits_the_tts_task_instead_of_sending_tts_end(monkeypatch):
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 9
    channel = _FakeAudioChannel()
    entity._active_channel = channel
    tts_ran = []

    async def fake_tts():
        tts_ran.append(True)

    async def fake_accept(self, mic_frames):
        entity._tts_task = asyncio.ensure_future(fake_tts())
        entity._tts_turn_token = entity._active_turn_token
        return None

    monkeypatch.setattr(AssistSatelliteEntity, "async_accept_pipeline_from_satellite", fake_accept)

    asyncio.run(entity._run_wake_pipeline(channel))

    assert tts_ran == [True]
    assert ("turn_action", 9, "tts/end", None) not in client.calls


def test_run_wake_pipeline_still_sends_tts_end_when_the_pipeline_raises(monkeypatch):
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 9
    channel = _FakeAudioChannel()
    entity._active_channel = channel

    async def fake_accept(self, mic_frames):
        raise RuntimeError("pipeline blew up")

    monkeypatch.setattr(AssistSatelliteEntity, "async_accept_pipeline_from_satellite", fake_accept)

    asyncio.run(entity._run_wake_pipeline(channel))  # must not raise

    assert ("turn_action", 9, "endpoint", None) in client.calls
    assert ("turn_action", 9, "tts/end", None) in client.calls


def test_run_wake_pipeline_does_nothing_extra_if_turn_was_never_marked_active(monkeypatch):
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = None  # e.g. attach already failed upstream

    async def fake_accept(self, mic_frames):
        return None

    monkeypatch.setattr(AssistSatelliteEntity, "async_accept_pipeline_from_satellite", fake_accept)

    asyncio.run(entity._run_wake_pipeline(_FakeAudioChannel()))

    assert client.calls == []


# ── _async_pipeline_event ────────────────────────────────────────────────

def test_pipeline_event_ignored_when_no_active_turn():
    entity, client, _coord = _make_satellite()
    asyncio.run(entity._async_pipeline_event(
        PipelineEvent(type=PipelineEventType.STT_END, data={"stt_output": {"text": "hi"}})
    ))
    assert client.calls == []


def test_stt_end_records_transcript_once_and_always_endpoints():
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 1
    entity._active_channel = object()
    event = PipelineEvent(type=PipelineEventType.STT_END, data={"stt_output": {"text": "turn off the lights"}})

    asyncio.run(entity._async_pipeline_event(event))
    asyncio.run(entity._async_pipeline_event(event))  # a second STT_END on the same turn

    transcript_calls = [c for c in client.calls if c[2] == "transcript"]
    endpoint_calls = [c for c in client.calls if c[2] == "endpoint"]
    assert len(transcript_calls) == 1
    assert transcript_calls[0][3] == {"text": "turn off the lights", "is_final": True}
    assert len(endpoint_calls) == 2


def test_stt_end_with_no_text_skips_transcript_but_still_endpoints():
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 1
    entity._active_channel = object()

    asyncio.run(entity._async_pipeline_event(
        PipelineEvent(type=PipelineEventType.STT_END, data={"stt_output": {}})
    ))

    assert [c[2] for c in client.calls] == ["endpoint"]


def test_intent_end_forwards_continue_conversation_flag():
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 1
    entity._active_channel = object()

    asyncio.run(entity._async_pipeline_event(PipelineEvent(
        type=PipelineEventType.INTENT_END,
        data={"intent_output": {
            "continue_conversation": True,
            "response": {"speech": {"plain": {"speech": "The answer."}}},
        }},
    )))

    assert entity._continue_conversation is True
    assert ("turn_action", 1, "pipeline-event",
             {"event": "intent_end", "continue_conversation": True}) in client.calls
    assert ("turn_action", 1, "tts-text", {"text": "The answer."}) in client.calls


def test_intent_progress_forwards_raw_tool_request_and_response():
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 1
    entity._active_channel = object()
    token = entity._active_turn_token

    asyncio.run(entity._async_pipeline_event(PipelineEvent(
        type=PipelineEventType.INTENT_PROGRESS,
        data={"chat_log_delta": {"tool_calls": [
            {"id": "call-1", "tool_name": "HassCancelTimer", "tool_args": {"name": "1:51 timer"}},
        ]}},
    )))
    asyncio.run(entity._async_pipeline_event(PipelineEvent(
        type=PipelineEventType.INTENT_PROGRESS,
        data={"chat_log_delta": {
            "role": "tool_result", "tool_call_id": "call-1", "tool_name": "HassCancelTimer",
            "tool_result": {"cancelled": ["timer-151"]},
        }},
    )))

    calls = [call for call in client.calls if call[2] == "tool-call"]
    assert calls == [
        ("turn_action", 1, "tool-call", {
            "sequence": 0, "call_id": "call-1", "name": "HassCancelTimer",
            "request": {"name": "1:51 timer"}, "response": None, "status": "pending",
        }),
        ("turn_action", 1, "tool-call", {
            "sequence": 0, "call_id": "call-1", "name": "HassCancelTimer",
            "request": {"name": "1:51 timer"}, "response": {"cancelled": ["timer-151"]}, "status": "ok",
        }),
    ]


def test_native_intent_chat_log_forwards_raw_tool_request_and_response():
    async def run():
        entity, client, _coord = _make_satellite()
        entity._active_turn_id = 1
        entity._active_channel = object()
        entity._active_conversation_id = "conversation-1"

        entity._on_chat_log_event("conversation-1", "content_added", {"content": {
            "role": "assistant", "tool_calls": [
                {"id": "call-1", "tool_name": "HassStartTimer", "tool_args": {"minutes": 1}},
            ],
        }})
        entity._on_chat_log_event("conversation-1", "content_added", {"content": {
            "role": "tool_result", "tool_call_id": "call-1", "tool_name": "HassStartTimer",
            "tool_result": {"response_type": "action_done"},
        }})
        await asyncio.gather(*entity.hass.created_tasks)

        calls = [call for call in client.calls if call[2] == "tool-call"]
        assert [call[3]["status"] for call in calls] == ["pending", "ok"]
        assert calls[-1][3]["request"] == {"minutes": 1}
        assert calls[-1][3]["response"] == {"response_type": "action_done"}

    asyncio.run(run())


def test_intent_start_binds_chat_log_subscription_to_the_active_turn():
    async def run():
        entity, _client, _coord = _make_satellite()
        entity._active_turn_id = 1
        entity._active_channel = object()
        entity.on_pipeline_event(PipelineEvent(
            type=PipelineEventType.INTENT_START,
            data={"conversation_id": "conversation-1"},
        ))
        assert entity._active_conversation_id == "conversation-1"
        await asyncio.gather(*entity.hass.created_tasks)

    asyncio.run(run())


def test_tts_end_spawns_exactly_one_tts_task_even_if_seen_twice(monkeypatch):
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 1
    entity._active_channel = object()
    spawned = []
    monkeypatch.setattr(
        entity, "_stream_pipeline_tts",
        lambda token, tid, turn_token, ch: spawned.append(token) or _noop(),
    )

    event = PipelineEvent(type=PipelineEventType.TTS_END, data={"tts_output": {"token": "tok-1"}})
    asyncio.run(entity._async_pipeline_event(event))
    asyncio.run(entity._async_pipeline_event(event))

    assert spawned == ["tok-1"]  # second TTS_END is a no-op: _tts_task already set


async def _noop():
    return None


def test_error_event_is_forwarded_as_a_pipeline_event():
    entity, client, _coord = _make_satellite()
    entity._active_turn_id = 1
    entity._active_channel = object()

    asyncio.run(entity._async_pipeline_event(PipelineEvent(type=PipelineEventType.ERROR)))

    assert ("turn_action", 1, "pipeline-event", {"event": "error"}) in client.calls


def test_pipeline_event_swallows_controller_errors():
    client = _FakeClient()
    client.turn_action_error_on = {(1, "endpoint")}
    entity, client, _coord = _make_satellite(client=client)
    entity._active_turn_id = 1
    entity._active_channel = object()

    # Must not raise even though the endpoint call fails server-side.
    asyncio.run(entity._async_pipeline_event(
        PipelineEvent(type=PipelineEventType.STT_END, data={"stt_output": {}})
    ))


# ── _stream_pipeline_tts ─────────────────────────────────────────────────
#
# tts.async_get_stream does not exist in the installed HA 2025.1 (confirmed:
# homeassistant.components.tts has no such attribute here) — it's the
# streaming-TTS-result API this package targets on 2026.8.0, which the fork
# validated end to end against a real 2026.8.2 container (SESSION_SUMMARY_2.md).
# monkeypatch.setattr(..., raising=False) below installs it as a test double
# regardless, so these tests validate THIS module's own call shape and
# control flow; they cannot by themselves confirm the real 2026.8 signature
# matches. Re-run against a real 2026.8 install (hacs/requirements-test.txt)
# before shipping if that hasn't been done since this was written.

class _FakeResultStream:
    async def async_stream_result(self):
        yield b"pcm"


def test_stream_pipeline_tts_happy_path(monkeypatch):
    entity, client, _coord = _make_satellite()
    channel = object()
    entity._active_turn_id = 1
    entity._active_channel = channel
    streamed = []

    async def fake_stream(result, channel):
        streamed.append((result, channel))
        return len(b"pcm")

    monkeypatch.setattr(module, "stream_result_to_audio", fake_stream)
    monkeypatch.setattr(module.tts, "async_get_stream", lambda hass, token: _FakeResultStream(), raising=False)

    asyncio.run(entity._stream_pipeline_tts("tok", 1, channel))

    assert ("turn_action", 1, "tts/start", None) in client.calls
    assert ("turn_action", 1, "tts/end", None) in client.calls
    assert len(streamed) == 1


def test_stream_pipeline_tts_sends_tts_end_when_ha_gives_no_result_stream(monkeypatch):
    entity, client, _coord = _make_satellite()
    channel = object()
    entity._active_turn_id = 1
    entity._active_channel = channel
    monkeypatch.setattr(module.tts, "async_get_stream", lambda hass, token: None, raising=False)

    asyncio.run(entity._stream_pipeline_tts("tok", 1, channel))

    assert ("turn_action", 1, "tts/end", None) in client.calls


def test_stream_pipeline_tts_ends_turn_when_provider_stream_fails(monkeypatch):
    entity, client, _coord = _make_satellite()
    channel = object()
    entity._active_turn_id = 1
    entity._active_channel = channel
    monkeypatch.setattr(module.tts, "async_get_stream", lambda hass, token: _FakeResultStream(), raising=False)

    async def quota_error(result, channel):
        raise RuntimeError("429 Resource has been exhausted")

    monkeypatch.setattr(module, "stream_result_to_audio", quota_error)

    asyncio.run(entity._stream_pipeline_tts("tok", 1, channel))

    assert ("turn_action", 1, "tts/start", None) in client.calls
    assert ("turn_action", 1, "tts/end", None) in client.calls


def test_stream_pipeline_tts_reraises_cancelled_error(monkeypatch):
    entity, client, _coord = _make_satellite()
    channel = object()
    entity._active_turn_id = 1
    entity._active_channel = channel
    monkeypatch.setattr(module.tts, "async_get_stream", lambda hass, token: _FakeResultStream(), raising=False)

    async def cancels(result, channel):
        raise asyncio.CancelledError()

    monkeypatch.setattr(module, "stream_result_to_audio", cancels)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(entity._stream_pipeline_tts("tok", 1, channel))


# ── async_announce ────────────────────────────────────────────────────────

class _Announcement:
    def __init__(self, media_id_source="tts", tts_token="tok-1"):
        self.media_id_source = media_id_source
        self.tts_token = tts_token


def test_announce_rejects_non_tts_sources():
    entity, client, _coord = _make_satellite()
    with pytest.raises(ValueError):
        asyncio.run(entity.async_announce(_Announcement(media_id_source="url")))


def test_announce_rejects_missing_tts_token():
    entity, client, _coord = _make_satellite()
    with pytest.raises(ValueError):
        asyncio.run(entity.async_announce(_Announcement(tts_token=None)))


def test_announce_creates_turn_streams_tts_and_signals_start_and_end(monkeypatch):
    entity, client, _coord = _make_satellite()
    monkeypatch.setattr(module.tts, "async_get_stream", lambda hass, token: _FakeResultStream(), raising=False)

    streamed = []

    async def fake_stream(result, channel):
        streamed.append(channel)
        return 3

    monkeypatch.setattr(module, "stream_result_to_audio", fake_stream)

    asyncio.run(entity.async_announce(_Announcement()))

    assert ("create_turn", "A", "announcement") in client.calls
    assert ("attach_audio", 42) in client.calls
    assert ("turn_action", 42, "tts/start", None) in client.calls
    assert ("turn_action", 42, "tts/end", None) in client.calls
    assert streamed == [client.attached_channel]


def test_announce_converts_controller_error_to_value_error():
    client = _FakeClient()
    client.turn_action_error_on = {(42, "tts/start")}
    entity, client, _coord = _make_satellite(client=client)

    with pytest.raises(ValueError):
        asyncio.run(entity.async_announce(_Announcement()))
