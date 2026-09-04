"""TimerCardHub orchestration and the WebSocket command wiring around it.

The hub itself takes every hass-touching lookup as an injected callable
(see timer_card.py's module docstring), so the first half of this file
exercises it with plain fakes and needs no Home Assistant at all. The
second half locally stubs `homeassistant.components.websocket_api` — the
same per-test sys.modules stubbing pattern test_assist_satellite.py uses
for `homeassistant.components.intent` — to prove the registration wiring
itself calls the hub correctly against a faithful fake of the real API
shape (introspected against a live Home Assistant install; see
docs/design/timers-implementation-update.md).
"""

import asyncio
import sys
import types

import pytest

from custom_components.echo_voice_satellite.timer_card import TimerCardHub


class FakeClient:
    def __init__(self, dismiss_result=True):
        self.dismiss_result = dismiss_result
        self.dismiss_calls = []

    async def async_dismiss_timer_alarm(self, device_id):
        self.dismiss_calls.append(device_id)
        return {"device_id": device_id, "dismissed": self.dismiss_result}


class FakeManager:
    def __init__(self):
        self.timers = {}
        self.calls = []

    def add_time(self, timer_id, seconds):
        self.calls.append(("change", timer_id, seconds))

    def start_timer(self, device_id, hours, minutes, seconds, language, name=None):
        self.calls.append(("start", device_id))
        return "new-timer-id"


def _hub(client=None, manager=None):
    manager = manager if manager is not None else FakeManager()
    return TimerCardHub(
        client or FakeClient(),
        manager_getter=lambda: manager,
        device_name_resolver=lambda device_id: f"name-{device_id}" if device_id else None,
        known_devices_getter=lambda: [{"device_id": "ha-device", "device_name": "Kitchen Echo"}],
        language="en",
    ), manager


# ── TimerCardHub ─────────────────────────────────────────────────────────────

def test_snapshot_includes_known_devices_even_with_no_timers():
    hub, _manager = _hub()
    assert hub.snapshot() == {"timers": [], "devices": [{"device_id": "ha-device", "device_name": "Kitchen Echo"}]}


def test_subscribe_pushes_the_current_snapshot_immediately():
    hub, _manager = _hub()
    pushes = []
    hub.subscribe(pushes.append)
    assert len(pushes) == 1
    assert pushes[0] == hub.snapshot()


def test_notify_manager_change_pushes_to_every_subscriber():
    hub, _manager = _hub()
    a, b = [], []
    hub.subscribe(a.append)
    hub.subscribe(b.append)
    hub.notify_manager_change()
    assert len(a) == 2 and len(b) == 2


def test_unsubscribe_stops_further_pushes():
    hub, _manager = _hub()
    pushes = []
    unsubscribe = hub.subscribe(pushes.append)
    unsubscribe()
    hub.notify_manager_change()
    assert len(pushes) == 1


def test_notify_alarm_event_updates_presence_and_pushes():
    hub, _manager = _hub()
    pushes = []
    hub.subscribe(pushes.append)
    hub.notify_alarm_event({
        "type": "timer.alarm", "device_id": "echomuse-1",
        "current": {"timer_id": "t1", "name": "pizza", "ha_device_id": "ha-device"},
        "queue": [],
    })
    assert len(pushes) == 2
    assert [row["id"] for row in pushes[-1]["timers"]] == ["t1"]


def test_action_pushes_only_when_the_manager_action_was_applied():
    hub, manager = _hub()
    pushes = []
    hub.subscribe(pushes.append)

    assert not hub.action("bogus-action", "t1")
    assert len(pushes) == 1  # no push for a rejected action

    assert hub.action("change", "t1", seconds=30)
    assert len(pushes) == 2
    assert manager.calls == [("change", "t1", 30)]


def test_action_with_no_manager_returns_false_without_pushing():
    hub = TimerCardHub(
        FakeClient(), manager_getter=lambda: None,
        device_name_resolver=lambda _id: None, known_devices_getter=list,
    )
    pushes = []
    hub.subscribe(pushes.append)
    assert not hub.action("pause", "t1")
    assert len(pushes) == 1


def test_start_pushes_only_on_success():
    hub, manager = _hub()
    pushes = []
    hub.subscribe(pushes.append)

    timer_id = hub.start(device_id="ha-device", minutes=5, name="pizza")

    assert timer_id == "new-timer-id"
    assert len(pushes) == 2
    assert manager.calls == [("start", "ha-device")]


def test_dismiss_calls_the_client_for_the_owning_echomuse_device():
    async def run():
        client = FakeClient(dismiss_result=True)
        hub, _manager = _hub(client=client)
        hub.notify_alarm_event({
            "type": "timer.alarm", "device_id": "echomuse-1",
            "current": {"timer_id": "t1", "ha_device_id": "ha-device"}, "queue": [],
        })
        pushes = []
        hub.subscribe(pushes.append)

        dismissed = await hub.dismiss("t1")

        assert dismissed is True
        assert client.dismiss_calls == ["echomuse-1"]
        assert len(pushes) == 2  # initial subscribe push + the dismiss push

    asyncio.run(run())


def test_dismiss_returns_false_and_never_calls_the_client_for_an_unknown_timer():
    async def run():
        client = FakeClient()
        hub, _manager = _hub(client=client)
        assert await hub.dismiss("no-such-timer") is False
        assert client.dismiss_calls == []

    asyncio.run(run())


def test_dismiss_does_not_push_when_the_controller_reports_nothing_dismissed():
    async def run():
        client = FakeClient(dismiss_result=False)
        hub, _manager = _hub(client=client)
        hub.notify_alarm_event({
            "type": "timer.alarm", "device_id": "echomuse-1",
            "current": {"timer_id": "t1", "ha_device_id": "ha-device"}, "queue": [],
        })
        pushes = []
        hub.subscribe(pushes.append)

        assert await hub.dismiss("t1") is False
        assert len(pushes) == 1

    asyncio.run(run())


# ── WebSocket command wiring ─────────────────────────────────────────────────

class FakeConnection:
    def __init__(self):
        self.results = []
        self.errors = []
        self.events = []
        self.subscriptions = {}

    def send_result(self, msg_id, result=None):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))

    def send_event(self, msg_id, event):
        self.events.append((msg_id, event))


@pytest.fixture
def fake_websocket_api(monkeypatch):
    """Mirrors the real homeassistant.components.websocket_api surface this
    module depends on (websocket_command/async_response/callback as
    decorators carrying `_ws_command`, async_register_command storing by
    that command name) — verified against a live Home Assistant install;
    see timer_card.py's docstring and docs/design/timers-implementation-update.md."""
    ws_mod = types.ModuleType("homeassistant.components.websocket_api")
    registered: dict[str, object] = {}

    def websocket_command(schema):
        command = schema["type"]

        def decorate(func):
            func._ws_command = command
            func._ws_schema = schema
            return func

        return decorate

    def async_response(func):
        return func

    def callback(func):
        return func

    def async_register_command(hass, handler):
        registered[handler._ws_command] = handler

    ws_mod.websocket_command = websocket_command
    ws_mod.async_response = async_response
    ws_mod.callback = callback
    ws_mod.async_register_command = async_register_command
    monkeypatch.setitem(sys.modules, "homeassistant.components.websocket_api", ws_mod)
    return registered


def _fake_hass_with_hub(hub):
    from custom_components.echo_voice_satellite.const import DOMAIN
    return types.SimpleNamespace(data={DOMAIN: {"entry-1": {"timer_card": hub}}})


def test_registered_commands_cover_the_documented_set(fake_websocket_api):
    from custom_components.echo_voice_satellite.timer_card import (
        async_register_websocket_commands,
    )

    async_register_websocket_commands(_fake_hass_with_hub(_hub()[0]))

    assert set(fake_websocket_api) == {
        "echo_voice_satellite/timers/list",
        "echo_voice_satellite/timers/subscribe",
        "echo_voice_satellite/timers/start",
        "echo_voice_satellite/timers/pause",
        "echo_voice_satellite/timers/resume",
        "echo_voice_satellite/timers/change",
        "echo_voice_satellite/timers/cancel",
        "echo_voice_satellite/timers/dismiss",
    }


def test_list_command_returns_the_hub_snapshot(fake_websocket_api):
    from custom_components.echo_voice_satellite.timer_card import (
        async_register_websocket_commands,
    )

    hub, _manager = _hub()
    hass = _fake_hass_with_hub(hub)
    async_register_websocket_commands(hass)
    connection = FakeConnection()

    asyncio.run(fake_websocket_api["echo_voice_satellite/timers/list"](hass, connection, {"id": 1}))

    assert connection.results == [(1, hub.snapshot())]


def test_subscribe_command_pushes_immediately_and_on_later_changes(fake_websocket_api):
    from custom_components.echo_voice_satellite.timer_card import (
        async_register_websocket_commands,
    )

    hub, _manager = _hub()
    hass = _fake_hass_with_hub(hub)
    async_register_websocket_commands(hass)
    connection = FakeConnection()

    fake_websocket_api["echo_voice_satellite/timers/subscribe"](hass, connection, {"id": 7})

    assert connection.results == [(7, None)]
    assert len(connection.events) == 1
    assert 7 in connection.subscriptions

    hub.notify_manager_change()
    assert len(connection.events) == 2

    connection.subscriptions[7]()  # unsubscribe, as HA calls on disconnect
    hub.notify_manager_change()
    assert len(connection.events) == 2


def test_pause_command_reports_timer_not_found_for_an_unknown_timer(fake_websocket_api):
    from custom_components.echo_voice_satellite.timer_card import (
        async_register_websocket_commands,
    )

    hub, _manager = _hub()
    hass = _fake_hass_with_hub(hub)
    async_register_websocket_commands(hass)
    connection = FakeConnection()

    asyncio.run(fake_websocket_api["echo_voice_satellite/timers/pause"](
        hass, connection, {"id": 2, "timer_id": "missing"},
    ))

    assert connection.results == []
    assert connection.errors == [(2, "timer_not_found", "Unknown timer")]


def test_dismiss_command_delegates_to_the_hub(fake_websocket_api):
    from custom_components.echo_voice_satellite.timer_card import (
        async_register_websocket_commands,
    )

    client = FakeClient(dismiss_result=True)
    hub, _manager = _hub(client=client)
    hub.notify_alarm_event({
        "type": "timer.alarm", "device_id": "echomuse-1",
        "current": {"timer_id": "t1", "ha_device_id": "ha-device"}, "queue": [],
    })
    hass = _fake_hass_with_hub(hub)
    async_register_websocket_commands(hass)
    connection = FakeConnection()

    asyncio.run(fake_websocket_api["echo_voice_satellite/timers/dismiss"](
        hass, connection, {"id": 3, "timer_id": "t1"},
    ))

    assert client.dismiss_calls == ["echomuse-1"]
    assert connection.results == [(3, {"dismissed": True})]


def test_start_command_reports_start_failed_with_no_device(fake_websocket_api):
    from custom_components.echo_voice_satellite.timer_card import (
        async_register_websocket_commands,
    )

    hub, _manager = _hub()
    hass = _fake_hass_with_hub(hub)
    async_register_websocket_commands(hass)
    connection = FakeConnection()

    asyncio.run(fake_websocket_api["echo_voice_satellite/timers/start"](
        hass, connection, {"id": 4},
    ))

    assert connection.errors == [(4, "invalid_request", "Missing device_id")]


def test_commands_degrade_gracefully_with_no_hub_registered(fake_websocket_api):
    from custom_components.echo_voice_satellite.timer_card import (
        async_register_websocket_commands,
    )
    from custom_components.echo_voice_satellite.const import DOMAIN

    hass = types.SimpleNamespace(data={DOMAIN: {}})
    async_register_websocket_commands(hass)
    connection = FakeConnection()

    asyncio.run(fake_websocket_api["echo_voice_satellite/timers/list"](hass, connection, {"id": 5}))

    assert connection.results == [(5, {"timers": [], "devices": []})]
