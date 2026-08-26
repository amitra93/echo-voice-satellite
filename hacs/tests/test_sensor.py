import importlib

import pytest


module = importlib.import_module("custom_components.echo_voice_satellite.sensor")


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


def test_sensor_module_no_longer_defines_firmware_or_wake_model_entities():
    # Both were diagnostic-category sensors duplicating info already on the
    # EchoMuse controller dashboard — removed. See __init__.py's
    # _remove_stale_diagnostic_sensor_entities for the registry cleanup.
    assert not hasattr(module, "EchoFirmwareSensor")
    assert not hasattr(module, "EchoWakeModelSensor")


def test_ambient_light_sensor_reads_lux_and_gates_on_capability():
    entity = _make(module.EchoAmbientLightSensor, {
        "device_id": "A", "connected": True, "ambient_light_lux": 42,
        "capabilities": ["ambient_light"],
    })
    assert entity.native_value == 42
    assert entity.available is True


def test_ambient_light_sensor_unavailable_without_the_capability():
    entity = _make(module.EchoAmbientLightSensor, {
        "device_id": "A", "connected": True, "ambient_light_lux": 42, "capabilities": [],
    })
    assert entity.available is False
