import importlib

import pytest


module = importlib.import_module("custom_components.echo_voice_satellite.binary_sensor")


class _FakeCoordinator:
    def __init__(self, record):
        self.data = {"devices": [record]}
        self.control_available = True
        self.last_update_success = True


def _make(cls, record):
    coordinator = _FakeCoordinator(record)
    entity = object.__new__(cls)
    entity.coordinator = coordinator
    entity.device_id = record["device_id"]
    entity._observed = False
    return entity


@pytest.mark.parametrize("connected", [True, False])
def test_online_sensor_mirrors_connected_flag(connected):
    entity = _make(module.EchoOnlineSensor, {"device_id": "A", "connected": connected})
    assert entity.is_on is connected


def test_binary_sensor_module_no_longer_defines_a_mute_entity():
    # Privacy mute moved to switch.py's EchoMuteSwitch once mute_toggle
    # existed on the wire — a switch replaces this read-only sensor AND
    # the momentary button.py button with one entity.
    assert not hasattr(module, "EchoMutedSensor")


def test_binary_sensor_setup_adds_entities_for_all_devices():
    import asyncio
    added = []
    coordinator = type("Coordinator", (), {
        "data": {"devices": [{"device_id": "A"}]},
        "known_capabilities": {},
        "async_add_listener": lambda self, callback: (callback() or (lambda: None)),
    })()
    entry = type("Entry", (), {"entry_id": "e", "async_on_unload": lambda self, remove: None})()
    hass = type("Hass", (), {"data": {module.DOMAIN: {"e": {"coordinator": coordinator}}}})()
    asyncio.run(module.async_setup_entry(hass, entry, added.extend))
    assert len(added) == 1
    assert added[0].is_on is False
