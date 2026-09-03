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
pipeline = _module("homeassistant.components.assist_pipeline")
pipeline.PipelineEventType = _enum("PipelineEventType", {
    "STT_END": "stt_end", "INTENT_END": "intent_end", "TTS_END": "tts_end", "ERROR": "error",
})
pipeline.PipelineEvent = lambda type, data=None: types.SimpleNamespace(type=type, data=data or {})
pipeline.async_get_pipelines = lambda hass: []
components.assist_pipeline = pipeline

# ── homeassistant.components.stt ───────────────────────────────────────────
# Minimal surface for GeminiTranscribeEntity's tests. Sourced against HA
# core's real homeassistant/components/stt/__init__.py and models.py rather
# than guessed — only the members this suite (and the plan's stt.py) touch
# are modelled.

class SpeechAudioProcessing:
    """Mirrors homeassistant.components.stt.SpeechAudioProcessing.

    Only the fields the plan depends on are modelled; the base class's
    default has requires_external_vad=True, which is exactly why
    GeminiTranscribeEntity must override it to False (see docs/design/hacs-stt-plan.md
    "audio_processing" section).
    """

    def __init__(
        self,
        requires_external_vad: bool = True,
        prefers_auto_gain_enabled: bool = False,
        prefers_noise_reduction_enabled: bool = False,
    ):
        self.requires_external_vad = requires_external_vad
        self.prefers_auto_gain_enabled = prefers_auto_gain_enabled
        self.prefers_noise_reduction_enabled = prefers_noise_reduction_enabled


DEFAULT_AUDIO_PROCESSING = SpeechAudioProcessing(
    requires_external_vad=True,
    prefers_auto_gain_enabled=False,
    prefers_noise_reduction_enabled=False,
)


class _SpeechResultState(Enum):
    SUCCESS = "success"
    ERROR = "error"


class SpeechResult:
    def __init__(self, text: str | None, result_state: "_SpeechResultState"):
        self.text = text
        self.result_state = result_state


class SpeechToTextEntity(Entity):
    """Tiny base matching the real SpeechToTextEntity contract.

    Only what check_metadata() and the supported_* properties need is
    implemented — enough for the suite to import and subclass without
    installing the real distribution.
    """

    @property
    def supported_languages(self) -> list[str]:
        return []

    @property
    def supported_formats(self) -> list[str]:
        return ["wav"]

    @property
    def supported_codecs(self) -> list[str]:
        return ["pcm"]

    @property
    def supported_bit_rates(self) -> list[int]:
        return [16]

    @property
    def supported_sample_rates(self) -> list[int]:
        return [16000]

    @property
    def supported_channels(self) -> list[int]:
        return [1]

    @property
    def audio_processing(self) -> SpeechAudioProcessing:
        return DEFAULT_AUDIO_PROCESSING

    def check_metadata(self, metadata) -> None:
        language = getattr(metadata, "language", None)
        if language not in self.supported_languages:
            raise ValueError("stt-provider-unsupported-metadata")

    async def async_process_audio_stream(self, metadata, stream):
        raise NotImplementedError


stt = _module("homeassistant.components.stt")
stt.SpeechToTextEntity = SpeechToTextEntity
stt.SpeechResult = SpeechResult
stt.SpeechResultState = _SpeechResultState
stt.SpeechAudioProcessing = SpeechAudioProcessing
stt.DEFAULT_AUDIO_PROCESSING = DEFAULT_AUDIO_PROCESSING
# Some call sites import from ...stt.models; expose the same names there.
stt_models = _module("homeassistant.components.stt.models")
stt_models.SpeechToTextEntity = SpeechToTextEntity
stt_models.SpeechResult = SpeechResult
stt_models.SpeechResultState = _SpeechResultState
stt_models.SpeechAudioProcessing = SpeechAudioProcessing
stt_models.DEFAULT_AUDIO_PROCESSING = DEFAULT_AUDIO_PROCESSING
components.stt = stt

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


class OptionsFlow:
    def __init__(self, *args, **kwargs):
        self.config_entry = None
        self.hass = None

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}


config_entries = _module("homeassistant.config_entries")
config_entries.ConfigFlow = ConfigFlow
config_entries.OptionsFlow = OptionsFlow
ha.config_entries = config_entries

# ── homeassistant.helpers.selector ──────────────────────────────────────
selector_mod = _module("homeassistant.helpers.selector")
helpers.selector = selector_mod


class TextSelectorConfig:
    def __init__(self, multiple: bool = False, **kwargs):
        self.multiple = multiple
        for k, v in kwargs.items():
            setattr(self, k, v)


class TextSelector:
    def __init__(self, config=None):
        self.config = config

    def __call__(self, value):
        return value


selector_mod.TextSelector = TextSelector
selector_mod.TextSelectorConfig = TextSelectorConfig
# Some imports use `from homeassistant.components import ...` vs helpers;
# expose via components as well for safety.
selector_comp = _module("homeassistant.components.selector")
selector_comp.TextSelector = TextSelector
selector_comp.TextSelectorConfig = TextSelectorConfig


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


def _install_google_genai_stubs():
    """Fake google-genai SDK surface used by stt.py's tests.

    Mirrors the real ``google.genai`` + ``google.genai.types`` shape the plan
    specifies (ai.google.dev/gemini-api/docs/live-api/live-transcribe):

      client = genai.Client(api_key=...)
      async with client.aio.live.connect(model="gemini-3.5-transcribe-live", config=config) as session:
          await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="..."))
          await session.send_realtime_input(audio_stream_end=True)
          async for response in session.receive():
              response.server_content.interim_input_transcription.text
              response.server_content.input_transcription.text

    No real network I/O — ``receive()`` yields a canned
    interim/interim/final sequence by default, the same role
    test_stop_cancellation.py's fake PipelineEvent objects play.
    Tests that need a different sequence patch the Client or the session's
    ``_responses`` before or after construction.
    """

    google_mod = _module("google")
    genai_mod = _module("google.genai")
    types_mod = _module("google.genai.types")
    google_mod.genai = genai_mod
    genai_mod.types = types_mod

    class Blob:
        def __init__(self, data=None, mime_type: str | None = None, **kwargs):
            self.data = data
            self.mime_type = mime_type

    class AudioTranscriptionConfig:
        def __init__(self, language_codes=None, mode: str | None = None, custom_vocabulary=None, **kwargs):
            self.language_codes = language_codes if language_codes is not None else []
            self.mode = mode
            self.custom_vocabulary = custom_vocabulary if custom_vocabulary is not None else []

    class LiveConnectConfig:
        def __init__(
            self,
            response_modalities=None,
            input_audio_transcription=None,
            **kwargs,
        ):
            self.response_modalities = response_modalities
            self.input_audio_transcription = input_audio_transcription
            for k, v in kwargs.items():
                setattr(self, k, v)

    def _make_gemini_response(*, interim: str | None = None, final: str | None = None):
        interim_ns = types.SimpleNamespace(text=interim) if interim is not None else None
        final_ns = types.SimpleNamespace(text=final) if final is not None else None
        server_content = types.SimpleNamespace(
            interim_input_transcription=interim_ns,
            input_transcription=final_ns,
        )
        return types.SimpleNamespace(server_content=server_content)

    def _default_gemini_responses():
        return [
            _make_gemini_response(interim="hello"),
            _make_gemini_response(interim="hello world"),
            _make_gemini_response(final="hello world"),
        ]

    class FakeGeminiLiveSession:
        """Async context manager whose ``receive()`` yields interim/interim/final."""

        def __init__(self, model: str | None = None, config=None, responses=None, api_key: str = ""):
            self.model = model
            self.config = config
            self._responses = list(responses) if responses is not None else _default_gemini_responses()
            self._api_key = api_key
            self.sent: list[dict] = []

        async def __aenter__(self):
            # Blank/missing key must surface as a Gemini auth failure, not a
            # KeyError, so stt.py's .get() handling is pinned (see design doc).
            if not self._api_key or not self._api_key.strip():
                raise ValueError("missing gemini api key")
            if "invalid" in self._api_key:
                raise ValueError("invalid_gemini_key")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send_realtime_input(self, audio=None, audio_stream_end: bool = False, **kwargs):
            self.sent.append({"audio": audio, "audio_stream_end": audio_stream_end})

        async def receive(self):
            for response in self._responses:
                yield response

    class _FakeLiveNamespace:
        def __init__(self, api_key: str = "", responses=None):
            self._api_key = api_key
            self._responses = responses

        def connect(self, model=None, config=None):
            return FakeGeminiLiveSession(
                model=model, config=config, responses=self._responses, api_key=self._api_key
            )

    class _FakeModelsNamespace:
        def __init__(self, api_key: str = ""):
            self._api_key = api_key

        async def list(self, **kwargs):
            if not self._api_key or not self._api_key.strip():
                raise ValueError("missing gemini api key")
            if self._api_key == "invalid" or "invalid" in self._api_key:
                raise ValueError("invalid_gemini_key")
            return []

        def __call__(self, *args, **kwargs):
            return self.list(*args, **kwargs)

    class _FakeAioNamespace:
        def __init__(self, api_key: str = "", responses=None):
            self.live = _FakeLiveNamespace(api_key=api_key, responses=responses)
            self.models = _FakeModelsNamespace(api_key=api_key)

    class Client:
        def __init__(self, api_key: str = "", **kwargs):
            self.api_key = api_key
            self.aio = _FakeAioNamespace(api_key=api_key)
            # Sync surface some SDKs expose as client.models
            self.models = _FakeModelsNamespace(api_key=api_key)

    # Expose on the modules exactly as the real SDK does.
    types_mod.Blob = Blob
    types_mod.AudioTranscriptionConfig = AudioTranscriptionConfig
    types_mod.LiveConnectConfig = LiveConnectConfig
    types_mod.FakeGeminiLiveSession = FakeGeminiLiveSession
    types_mod._make_gemini_response = _make_gemini_response
    types_mod._default_gemini_responses = _default_gemini_responses
    genai_mod.Client = Client
    genai_mod.types = types_mod
    # ``from google import genai`` expects ``google.genai`` to be importable
    # and ``google.genai.types`` likewise.
    sys.modules["google"].genai = genai_mod


_install_external_stubs()
_install_google_genai_stubs()
entity_registry.async_get = lambda hass: types.SimpleNamespace(entities={}, async_remove=lambda entity_id: None)


vol = types.ModuleType("voluptuous")
vol.Required = lambda key, default=None, **kwargs: key
vol.Optional = lambda key, default=None, **kwargs: key
vol.In = lambda options: (lambda v: v)
vol.All = lambda *validators, **kwargs: (lambda v: v)
vol.Coerce = lambda type_fn: (lambda v: v)
vol.Range = lambda *a, **kw: (lambda v: v)
vol.Length = lambda *a, **kw: (lambda v: v)
vol.Schema = lambda schema, **kwargs: schema
vol.Invalid = type("Invalid", (Exception,), {})
vol.MultipleInvalid = type("MultipleInvalid", (Exception,), {})
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
