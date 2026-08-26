"""Small Home Assistant surface used by the integration tests.

The integration's logic is intentionally testable without installing the
large Home Assistant distribution.  These objects model only the base-class
contracts exercised by this test suite.
"""

from __future__ import annotations

import sys
import types
import asyncio
import inspect
from enum import Enum
from enum import IntFlag
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))


def _module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


ha = _module("homeassistant")
components = _module("homeassistant.components")
helpers = _module("homeassistant.helpers")
update = _module("homeassistant.helpers.update_coordinator")
entity_registry = _module("homeassistant.helpers.entity_registry")
const = _module("homeassistant.const")
const.LIGHT_LUX = "lx"


class CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


class DataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, **kwargs):
        self.hass = hass
        self.data = None
        self.last_update_success = True

    def async_set_updated_data(self, data):
        self.data = data

    def async_update_listeners(self):
        return None


class UpdateFailed(Exception):
    pass


update.CoordinatorEntity = CoordinatorEntity
update.DataUpdateCoordinator = DataUpdateCoordinator
update.UpdateFailed = UpdateFailed
helpers.update_coordinator = update


class Entity:
    def async_write_ha_state(self):
        return None


class SensorEntity(Entity):
    pass


class BinarySensorEntity(Entity):
    pass


class SwitchEntity(Entity):
    pass


class SelectEntity(Entity):
    pass


class MediaPlayerEntity(Entity):
    pass


def _enum(name, values):
    return Enum(name, values)


sensor = _module("homeassistant.components.sensor")
sensor.SensorEntity = SensorEntity
sensor.SensorDeviceClass = _enum("SensorDeviceClass", {"ILLUMINANCE": "illuminance"})
sensor.SensorStateClass = _enum("SensorStateClass", {"MEASUREMENT": "measurement"})
binary = _module("homeassistant.components.binary_sensor")
binary.BinarySensorEntity = BinarySensorEntity
binary.BinarySensorDeviceClass = _enum("BinarySensorDeviceClass", {"CONNECTIVITY": "connectivity"})
switch = _module("homeassistant.components.switch")
switch.SwitchEntity = SwitchEntity
select = _module("homeassistant.components.select")
select.SelectEntity = SelectEntity
media = _module("homeassistant.components.media_player")
media.MediaPlayerEntity = MediaPlayerEntity
media.MediaPlayerDeviceClass = _enum("MediaPlayerDeviceClass", {"SPEAKER": "speaker"})
class MediaPlayerEntityFeature(IntFlag):
    PLAY = 1
    PAUSE = 2
    STOP = 512
    VOLUME_SET = 4
    VOLUME_MUTE = 8
    NEXT_TRACK = 16
    PREVIOUS_TRACK = 32
    SEEK = 64
    PLAY_MEDIA = 128
    BROWSE_MEDIA = 256


media.MediaPlayerEntityFeature = MediaPlayerEntityFeature
media.MediaPlayerState = _enum("MediaPlayerState", {
    "IDLE": "idle", "PLAYING": "playing", "PAUSED": "paused",
})


pipeline = _module("homeassistant.components.assist_pipeline")
pipeline.PipelineEventType = _enum("PipelineEventType", {
    "STT_END": "stt_end", "INTENT_END": "intent_end", "TTS_END": "tts_end", "ERROR": "error",
})
pipeline.PipelineEvent = lambda type, data=None: types.SimpleNamespace(type=type, data=data or {})
pipeline.async_get_pipelines = lambda hass: []
components.assist_pipeline = pipeline

tts = _module("homeassistant.components.tts")
components.tts = tts


class AssistSatelliteEntity(Entity):
    def __init__(self, *args, **kwargs):
        self.state = None

    async def async_accept_pipeline_from_satellite(self, mic_frames):
        return None

    def _set_state(self, state):
        self.state = state

    @property
    def supported_features(self):
        return getattr(self, "_attr_supported_features", 0)

    def tts_response_finished(self):
        self._set_state(AssistSatelliteState.IDLE)


class AssistSatelliteEntityFeature:
    ANNOUNCE = 1


class AssistSatelliteState:
    IDLE = "idle"
    RESPONDING = "responding"


assist_sat = _module("homeassistant.components.assist_satellite")
assist_sat.AssistSatelliteEntity = AssistSatelliteEntity
assist_sat.AssistSatelliteEntityFeature = AssistSatelliteEntityFeature
assist_sat.AssistSatelliteState = AssistSatelliteState
assist_sat.AssistSatelliteAnnouncement = type("AssistSatelliteAnnouncement", (), {})
assist_sat.AssistSatelliteConfiguration = type("AssistSatelliteConfiguration", (), {})
assist_sat.AssistSatelliteWakeWord = type("AssistSatelliteWakeWord", (), {})
assist_sat_entity = _module("homeassistant.components.assist_satellite.entity")
assist_sat_entity.AssistSatelliteState = AssistSatelliteState
components.assist_satellite = assist_sat


class ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        return super().__init_subclass__()

    async def async_set_unique_id(self, value):
        self.unique_id = value

    def _abort_if_unique_id_configured(self):
        return None

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}


config_entries = _module("homeassistant.config_entries")
config_entries.ConfigFlow = ConfigFlow
ha.config_entries = config_entries


bluetooth = _module("homeassistant.components.bluetooth")
bluetooth.BaseHaRemoteScanner = type("BaseHaRemoteScanner", (), {
    "__init__": lambda self, *args: None,
    "async_setup": lambda self: _done(),
})
bluetooth.async_get_advertisement_callback = lambda hass: (lambda info: None)
bluetooth.async_register_scanner = lambda hass, scanner, **kwargs: (lambda: None)
components.bluetooth = bluetooth


async def _done():
    return None


def _install_external_stubs():
    bleak = _module("bleak")
    bleak_backends = _module("bleak.backends")
    bleak_device = _module("bleak.backends.device")
    bleak_scanner = _module("bleak.backends.scanner")
    bleak_device.BLEDevice = type("BLEDevice", (), {
        "__init__": lambda self, address, name, details: setattr(self, "address", address),
    })
    bleak_scanner.AdvertisementData = type("AdvertisementData", (), {
        "__init__": lambda self, *args: setattr(self, "args", args),
    })
    habluetooth = _module("habluetooth")
    models = _module("habluetooth.models")
    models.BluetoothServiceInfoBleak = type("BluetoothServiceInfoBleak", (), {
        "from_device_and_advertisement_data": staticmethod(
            lambda device, advertisement, *args: types.SimpleNamespace(
                address=device.address,
                name=getattr(advertisement, "args", [None, None])[0],
                rssi=getattr(advertisement, "args", [None] * 6)[5],
                source=args[0],
                connectable=args[2],
                advertisement=advertisement,
            )
        ),
    })


_install_external_stubs()
entity_registry.async_get = lambda hass: types.SimpleNamespace(entities={}, async_remove=lambda entity_id: None)


vol = types.ModuleType("voluptuous")
vol.Required = lambda key, default=None: key
vol.Schema = lambda value: value
vol.__getattr__ = lambda name: str
sys.modules["voluptuous"] = vol


def pytest_pyfunc_call(pyfuncitem):
    """Run the few async-marked tests without pytest-asyncio."""
    test = pyfuncitem.obj
    if inspect.iscoroutinefunction(test):
        asyncio.run(test(**{name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}))
        return True
    return None


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run a coroutine test with asyncio.run")
