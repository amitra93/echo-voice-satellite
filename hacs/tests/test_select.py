import asyncio
import importlib
import types

import pytest


module = importlib.import_module("custom_components.echo_voice_satellite.select")


class _FakeCoordinator:
    def __init__(self, record):
        self.data = {"devices": [record]}
        self.control_available = True
        self.last_update_success = True


class _FakeEntry:
    def __init__(self, options=None):
        self.options = options or {}
        self.updates = []


class _FakeConfigEntries:
    def __init__(self, entry):
        self.entry = entry

    def async_update_entry(self, entry, options):
        entry.options = options


class _FakeHass:
    def __init__(self, entry):
        self.config_entries = _FakeConfigEntries(entry)


def _pipeline(name, pipeline_id):
    return types.SimpleNamespace(name=name, id=pipeline_id)


def _make(monkeypatch, pipelines, options=None, device_id="A"):
    monkeypatch.setattr(module.assist_pipeline, "async_get_pipelines", lambda hass: pipelines)
    entry = _FakeEntry(options=options)
    hass = _FakeHass(entry)
    coordinator = _FakeCoordinator({"device_id": device_id})
    entity = object.__new__(module.EchoAssistPipelineSelect)
    entity.coordinator = coordinator
    entity.hass = hass
    entity.entry = entry
    entity.device_id = device_id
    entity._observed = False
    entity._pipeline_ids = {}
    written = []
    entity.async_write_ha_state = lambda: written.append(True)
    return entity, entry, written


def test_options_lists_the_configured_pipeline_names(monkeypatch):
    entity, _entry, _w = _make(
        monkeypatch,
        [_pipeline("Home", "id-1"), _pipeline("Whisper / Piper", "id-2")],
    )
    assert entity.options == ["Home", "Whisper / Piper"]


def test_current_option_reads_the_saved_pipeline_for_this_device(monkeypatch):
    entity, _entry, _w = _make(
        monkeypatch,
        [_pipeline("Home", "id-1"), _pipeline("Other", "id-2")],
        options={"assist_pipeline_ids": {"A": "id-2"}},
    )
    assert entity.current_option == "Other"


def test_current_option_is_none_when_no_pipeline_is_configured(monkeypatch):
    entity, _entry, _w = _make(
        monkeypatch, [_pipeline("Home", "id-1")], options={},
    )
    assert entity.current_option is None


def test_current_option_is_none_for_a_stale_pipeline_id(monkeypatch):
    entity, _entry, _w = _make(
        monkeypatch, [_pipeline("Home", "id-1")],
        options={"assist_pipeline_ids": {"A": "id-deleted"}},
    )
    assert entity.current_option is None


def test_select_option_persists_to_entry_options_scoped_by_device(monkeypatch):
    entity, entry, written = _make(
        monkeypatch, [_pipeline("Home", "id-1")],
        options={"assist_pipeline_ids": {"OTHER_DEVICE": "id-9"}},
    )
    entity.options

    asyncio.run(entity.async_select_option("Home"))

    assert entry.options["assist_pipeline_ids"] == {"OTHER_DEVICE": "id-9", "A": "id-1"}
    assert written == [True]


def test_select_option_rejects_an_unknown_pipeline_name(monkeypatch):
    entity, _entry, _written = _make(monkeypatch, [_pipeline("Home", "id-1")])
    entity.options

    with pytest.raises(ValueError, match="Unknown Assist pipeline"):
        asyncio.run(entity.async_select_option("Nonexistent"))
