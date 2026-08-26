import asyncio
import importlib

import pytest


module = importlib.import_module("custom_components.echo_voice_satellite.switch")
from custom_components.echo_voice_satellite.client import ControllerError  # noqa: E402


class _FakeCoordinator:
    def __init__(self, record):
        self.data = {"devices": [record]}
        self.control_available = True
        self.last_update_success = True


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.should_fail = False

    async def async_media_command(self, device_id, body):
        self.calls.append((device_id, body))
        if self.should_fail:
            raise ControllerError("media_command_failed")


def _make(record, client=None):
    coordinator = _FakeCoordinator(record)
    client = client or _FakeClient()
    entity = object.__new__(module.EchoMuteSwitch)
    entity.coordinator = coordinator
    entity.client = client
    entity.device_id = record["device_id"]
    entity._observed = False
    return entity, client


@pytest.mark.parametrize("muted", [True, False])
def test_is_on_mirrors_the_live_muted_flag(muted):
    entity, _client = _make({"device_id": "A", "connected": True, "muted": muted})
    assert entity.is_on is muted


def test_is_on_defaults_false_when_never_reported():
    entity, _client = _make({"device_id": "A"})
    assert entity.is_on is False


def test_available_does_not_depend_on_mute_state():
    # Must keep reporting/be controllable while muted — the whole point of
    # a mute switch is being able to turn it back off. Inherits the base
    # EchoCoordinatorEntity.available, which has no mute check at all
    # (matches the binary_sensor.py entity this replaced).
    entity, _client = _make({"device_id": "A", "connected": True, "muted": True})
    assert entity.available is True


# ── turn_on / turn_off — the wire only offers a toggle, never a set ────────

def test_turn_on_sends_toggle_when_currently_unmuted():
    entity, client = _make({"device_id": "A", "muted": False})
    asyncio.run(entity.async_turn_on())
    assert client.calls == [("A", {"command": "mute_toggle"})]


def test_turn_on_is_a_no_op_when_already_muted():
    # A toggle sent unconditionally would UNMUTE an already-muted device —
    # the opposite of what turn_on() asked for.
    entity, client = _make({"device_id": "A", "muted": True})
    asyncio.run(entity.async_turn_on())
    assert client.calls == []


def test_turn_off_sends_toggle_when_currently_muted():
    entity, client = _make({"device_id": "A", "muted": True})
    asyncio.run(entity.async_turn_off())
    assert client.calls == [("A", {"command": "mute_toggle"})]


def test_turn_off_is_a_no_op_when_already_unmuted():
    entity, client = _make({"device_id": "A", "muted": False})
    asyncio.run(entity.async_turn_off())
    assert client.calls == []


def test_turn_on_converts_controller_error_to_runtime_error():
    client = _FakeClient()
    client.should_fail = True
    entity, client = _make({"device_id": "A", "muted": False}, client=client)
    with pytest.raises(RuntimeError):
        asyncio.run(entity.async_turn_on())


def test_turn_off_converts_controller_error_to_runtime_error():
    client = _FakeClient()
    client.should_fail = True
    entity, client = _make({"device_id": "A", "muted": True}, client=client)
    with pytest.raises(RuntimeError):
        asyncio.run(entity.async_turn_off())
