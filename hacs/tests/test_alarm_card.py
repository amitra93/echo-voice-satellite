"""Alarm-card routing and WebSocket registration tests (no HA runtime)."""

import sys
import types

from custom_components.echo_voice_satellite import alarm_card


class _Coord:
    def __init__(self, device_ids):
        self.data = {"devices": [{"device_id": d, "label": f"{d} label"} for d in device_ids]}


def _entry(client, device_ids):
    return {"client": client, "coordinator": _Coord(device_ids)}


def test_resolve_client_finds_owning_entry():
    c1, c2 = object(), object()
    entries = [_entry(c1, ["dev1", "dev2"]), _entry(c2, ["dev3"])]
    assert alarm_card.resolve_client(entries, "dev2") is c1
    assert alarm_card.resolve_client(entries, "dev3") is c2
    assert alarm_card.resolve_client(entries, "ghost") is None


def test_resolve_client_ignores_non_dict_and_empty_entries():
    c1 = object()
    entries = ["not a dict", {"client": None, "coordinator": None}, _entry(c1, ["dev1"])]
    assert alarm_card.resolve_client(entries, "dev1") is c1


def test_device_options_dedupes_and_names():
    c1, c2 = object(), object()
    entries = [_entry(c1, ["dev1", "dev2"]), _entry(c2, ["dev2", "dev3"])]
    opts = alarm_card.device_options(entries)
    ids = [o["device_id"] for o in opts]
    assert ids == ["dev1", "dev2", "dev3"]        # dev2 deduped
    assert {o["device_name"] for o in opts} == {"dev1 label", "dev2 label", "dev3 label"}


class _FakeWebsocketApi(types.ModuleType):
    def __init__(self):
        super().__init__("websocket_api")
        self.schemas = {}
        self.registered = []

    def websocket_command(self, schema):
        def decorate(fn):
            self.schemas[schema["type"]] = schema
            return fn

        return decorate

    def async_response(self, fn):
        return fn

    def async_register_command(self, _hass, fn):
        self.registered.append(fn)


def test_every_alarm_card_command_is_registered(monkeypatch):
    fake_api = _FakeWebsocketApi()
    components = types.ModuleType("homeassistant.components")
    components.websocket_api = fake_api
    monkeypatch.setitem(sys.modules, "homeassistant.components.websocket_api", fake_api)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    hass = types.SimpleNamespace(data={})

    alarm_card.async_register_alarm_ws_commands(hass)

    expected = {
        "echo_voice_satellite/alarms/list",
        "echo_voice_satellite/alarms/create",
        "echo_voice_satellite/alarms/cancel",
    }
    assert set(fake_api.schemas) == expected
    assert len(fake_api.registered) == len(expected)
    assert "alarm_id" in fake_api.schemas["echo_voice_satellite/alarms/cancel"]
    assert "id" not in fake_api.schemas["echo_voice_satellite/alarms/cancel"]
