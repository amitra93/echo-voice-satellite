"""Turn-correlated stop cancellation contract."""

import asyncio

from custom_components.echo_voice_satellite.assist_satellite import EchoAssistSatellite


class _Client:
    def __init__(self):
        self.calls = []

    async def async_turn_action(self, turn_id, action, data=None):
        self.calls.append((turn_id, action, data))


class _Channel:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _satellite():
    entity = object.__new__(EchoAssistSatellite)
    entity.device_id = "device-a"
    entity.client = _Client()
    entity._active_turn_id = 12
    entity._active_channel = _Channel()
    entity._active_turn_token = object()
    entity._pipeline_task = None
    entity._tts_task = None
    entity._tts_turn_token = None
    entity.tts_response_finished = lambda: None
    return entity


def test_cancel_only_stops_the_matching_device_and_turn():
    entity = _satellite()

    asyncio.run(entity._async_gateway_event({
        "type": "turn.cancel", "device_id": "other", "turn_id": 12, "reason": "stop",
    }))

    assert entity._active_turn_id == 12
    assert entity.client.calls == []


def test_cancel_closes_matching_channel_and_resolves_tts_rendezvous():
    entity = _satellite()
    channel = entity._active_channel

    asyncio.run(entity._async_gateway_event({
        "type": "turn.cancel", "device_id": "device-a", "turn_id": 12, "reason": "stop",
    }))

    assert entity._active_turn_id is None
    assert channel.closed
    assert entity.client.calls == [(12, "tts/end", None)]


def test_cancel_awaits_both_owned_tasks():
    entity = _satellite()

    async def never_finishes():
        await asyncio.sleep(60)

    async def run():
        pipeline_task = asyncio.create_task(never_finishes())
        tts_task = asyncio.create_task(never_finishes())
        entity._pipeline_task = pipeline_task
        entity._tts_task = tts_task
        await entity._async_gateway_event({
            "type": "turn.cancel", "device_id": "device-a", "turn_id": 12, "reason": "stop",
        })
        return pipeline_task, tts_task

    pipeline_task, tts_task = asyncio.run(run())

    assert pipeline_task.cancelled()
    assert tts_task.cancelled()


def test_late_pipeline_event_cannot_touch_a_replaced_turn():
    entity = _satellite()
    old_token = entity._active_turn_token
    old_channel = entity._active_channel
    entity._active_turn_id = 13
    entity._active_turn_token = object()
    entity._active_channel = _Channel()

    from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType

    asyncio.run(entity._async_pipeline_event(
        PipelineEvent(PipelineEventType.ERROR), 12, old_token, old_channel
    ))

    assert entity.client.calls == []
