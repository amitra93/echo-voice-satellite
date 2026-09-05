"""Timer-card WebSocket schemas accept the card's real payloads.

`timer_card.async_register_websocket_commands` must stay importable
without Home Assistant installed, so this stubs `websocket_api`
(capturing each command's schema dict); `voluptuous` comes from the
shared conftest stub (markers as key-preserving identities) — the test
pins declared key NAMES, HA's own voluptuous validates semantics at
runtime. Every key the card sends must be declared: `websocket_command`
builds a strict schema, and an undeclared key is rejected with "extra
keys not allowed" before the handler runs — which is how
start/pause/resume/change/cancel/dismiss all silently broke while
list/subscribe (no extra keys) kept working.

Payload shapes mirror `www/echo-voice-timers-card.js`' `_call()` sites;
if the card gains a key, add it here AND to the schema.
"""

import sys
import types

from custom_components.echo_voice_satellite import timer_card


class _FakeWebsocketApi(types.ModuleType):
    def __init__(self):
        super().__init__("websocket_api")
        self.schemas = {}

    def websocket_command(self, schema):
        def decorate(fn):
            self.schemas[schema["type"]] = schema
            return fn

        return decorate

    def async_response(self, fn):
        return fn

    def callback(self, fn):
        return fn

    def async_register_command(self, hass, fn):
        pass


def _registered_schemas(monkeypatch):
    fake_api = _FakeWebsocketApi()
    components = types.ModuleType("homeassistant.components")
    components.websocket_api = fake_api
    monkeypatch.setitem(sys.modules, "homeassistant.components.websocket_api", fake_api)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    timer_card.async_register_websocket_commands(object())
    return fake_api.schemas


# What the card actually sends per action (see _call() sites in the JS).
CARD_PAYLOADS = {
    "echo_voice_satellite/timers/start": {"device_id", "minutes", "name"},
    "echo_voice_satellite/timers/pause": {"timer_id"},
    "echo_voice_satellite/timers/resume": {"timer_id"},
    "echo_voice_satellite/timers/change": {"timer_id", "seconds"},
    "echo_voice_satellite/timers/cancel": {"timer_id"},
    "echo_voice_satellite/timers/dismiss": {"timer_id"},
    "echo_voice_satellite/timers/list": set(),
    "echo_voice_satellite/timers/subscribe": set(),
}


def test_every_card_command_is_registered(monkeypatch):
    schemas = _registered_schemas(monkeypatch)
    assert set(schemas) == set(CARD_PAYLOADS)


def test_every_card_payload_key_is_declared(monkeypatch):
    schemas = _registered_schemas(monkeypatch)
    for command, payload_keys in CARD_PAYLOADS.items():
        declared = {key for key in schemas[command] if key != "type"}
        missing = payload_keys - declared
        assert not missing, f"{command} rejects card keys: {sorted(missing)}"
