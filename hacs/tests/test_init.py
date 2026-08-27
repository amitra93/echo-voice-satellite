"""async_setup / async_setup_entry / async_unload_entry wiring.

__init__.py defers every homeassistant-touching import into the function
bodies (see its module docstring) — importing THIS test module itself needs
no more than `custom_components.echo_voice_satellite` (the package). The test
suite supplies small Home Assistant-compatible fakes in conftest.py, so setup
and unload behavior also runs without Home Assistant installed.
"""

import asyncio
import importlib

import pytest


module = importlib.import_module("custom_components.echo_voice_satellite")
from custom_components.echo_voice_satellite.const import CONF_API_KEY, CONF_URL, DOMAIN, PLATFORMS  # noqa: E402


class _FakeClient:
    instances = []

    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key
        self.closed = False
        type(self).instances.append(self)

    async def async_close(self):
        self.closed = True


class _FakeCoordinator:
    instances = []

    def __init__(self, hass, client, entry):
        self.hass = hass
        self.client = client
        self.entry = entry
        self.data = {"devices": []}
        self.shutdown_called = False
        self._listeners = []
        self._event_listeners = []
        type(self).instances.append(self)

    async def async_config_entry_first_refresh(self):
        pass

    async def async_connect_control(self):
        pass

    async def async_shutdown(self):
        self.shutdown_called = True

    def async_add_listener(self, callback):
        self._listeners.append(callback)
        return lambda: None

    def async_add_event_listener(self, callback):
        self._event_listeners.append(callback)
        return lambda: None

    async def emit(self, event):
        for cb in self._event_listeners:
            await cb(event)


class _FakeScanner:
    def __init__(self, source):
        self.source = source
        self.fed = []

    def feed(self, advert):
        if advert.get("addr") == "bad":
            raise ValueError("malformed")
        self.fed.append(advert)


class _FakeConfigEntries:
    def __init__(self):
        self.forwarded = []
        self.unloaded = []

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded.append((entry, tuple(platforms)))

    async def async_unload_platforms(self, entry, platforms):
        self.unloaded.append((entry, tuple(platforms)))
        return True


class _FakeHass:
    def __init__(self):
        self.data = {}
        self.config_entries = _FakeConfigEntries()


class _FakeEntry:
    def __init__(self, entry_id="entry-1"):
        self.entry_id = entry_id
        self.data = {CONF_URL: "http://controller:8768", CONF_API_KEY: "em_key"}


def _patch_collaborators(monkeypatch, *, connect_error=None, register_scanner=None):
    _FakeClient.instances = []
    _FakeCoordinator.instances = []

    import custom_components.echo_voice_satellite.client as client_mod
    import custom_components.echo_voice_satellite.coordinator as coordinator_mod
    import custom_components.echo_voice_satellite.ble_scanner as ble_mod

    monkeypatch.setattr(client_mod, "ControllerClient", _FakeClient)

    class FailingCoordinator(_FakeCoordinator):
        async def async_connect_control(self):
            raise connect_error

    coordinator_cls = FailingCoordinator if connect_error is not None else _FakeCoordinator
    monkeypatch.setattr(coordinator_mod, "EchoVoiceSatelliteCoordinator", coordinator_cls)

    registered = []

    def default_register(hass, source):
        scanner = _FakeScanner(source)
        registered.append(scanner)
        return scanner, lambda: registered.remove(scanner)

    monkeypatch.setattr(ble_mod, "register_scanner", register_scanner or default_register)
    # The entity-registry cleanup migration (_remove_stale_button_entities)
    # needs a real hass.config/entity_registry that these lightweight fakes
    # deliberately don't provide — it's covered on its own in
    # test_remove_stale_button_entities.py instead, against the real
    # registry. These tests are about client/coordinator/scanner wiring.
    monkeypatch.setattr(module, "_remove_stale_button_entities", lambda hass, entry: None)
    monkeypatch.setattr(module, "_remove_stale_volume_number_entities", lambda hass, entry: None)
    monkeypatch.setattr(module, "_remove_stale_mute_entities", lambda hass, entry: None)
    monkeypatch.setattr(module, "_remove_stale_sendspin_music_entities", lambda hass, entry: None)
    monkeypatch.setattr(module, "_remove_stale_diagnostic_sensor_entities", lambda hass, entry: None)
    return registered


def test_async_setup_initializes_domain_bucket():
    hass = _FakeHass()
    asyncio.run(module.async_setup(hass, {}))
    assert hass.data[DOMAIN] == {}


def test_async_setup_entry_wires_client_coordinator_and_forwards_platforms(monkeypatch):
    _patch_collaborators(monkeypatch)
    hass = _FakeHass()
    entry = _FakeEntry()

    asyncio.run(module.async_setup_entry(hass, entry))

    client = _FakeClient.instances[0]
    assert client.base_url == "http://controller:8768"
    assert client.api_key == "em_key"

    stored = hass.data[DOMAIN][entry.entry_id]
    assert stored["client"] is client
    assert isinstance(stored["coordinator"], _FakeCoordinator)
    assert hass.config_entries.forwarded == [(entry, tuple(PLATFORMS))]


def test_async_setup_entry_closes_client_and_reraises_on_connect_failure(monkeypatch):
    _patch_collaborators(monkeypatch, connect_error=RuntimeError("no controller"))
    hass = _FakeHass()
    entry = _FakeEntry()

    with pytest.raises(RuntimeError):
        asyncio.run(module.async_setup_entry(hass, entry))

    assert _FakeClient.instances[0].closed is True
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


def test_async_setup_entry_registers_one_ble_scanner_per_known_device(monkeypatch):
    registered = _patch_collaborators(monkeypatch)
    hass = _FakeHass()
    entry = _FakeEntry()

    asyncio.run(module.async_setup_entry(hass, entry))
    coordinator = _FakeCoordinator.instances[0]
    coordinator.data = {"devices": [{"device_id": "A"}, {"device_id": "B"}]}
    for listener in coordinator._listeners:
        listener()  # simulate the coordinator's own data-refresh callback

    assert sorted(s.source for s in registered) == ["echomuse-A", "echomuse-B"]

    # A second refresh with the same devices must not double-register.
    for listener in coordinator._listeners:
        listener()
    assert len(registered) == 2


def test_ble_adverts_event_feeds_the_matching_scanner_only(monkeypatch):
    registered = _patch_collaborators(monkeypatch)
    hass = _FakeHass()
    entry = _FakeEntry()
    asyncio.run(module.async_setup_entry(hass, entry))
    coordinator = _FakeCoordinator.instances[0]
    coordinator.data = {"devices": [{"device_id": "A"}]}
    for listener in coordinator._listeners:
        listener()
    scanner = registered[0]

    async def run():
        await coordinator.emit({
            "type": "ble.adverts", "device_id": "A",
            "adverts": [{"addr": "aa:bb:cc:dd:ee:ff", "rssi": -50}],
        })
        # Unknown device — silently ignored, not an error.
        await coordinator.emit({"type": "ble.adverts", "device_id": "UNKNOWN", "adverts": [{}]})
        # Non-BLE event — ignored.
        await coordinator.emit({"type": "turn.state", "device_id": "A"})
        # A malformed advert inside a real batch must not crash the handler.
        await coordinator.emit({"type": "ble.adverts", "device_id": "A", "adverts": [{"addr": "bad"}]})

    asyncio.run(run())

    assert scanner.fed == [{"addr": "aa:bb:cc:dd:ee:ff", "rssi": -50}]


# ── _remove_stale_button_entities ───────────────────────────────────────────
#
# Monkeypatches homeassistant.helpers.entity_registry.async_get directly
# rather than constructing a real EntityRegistry: the real one needs
# hass.bus/hass.config plus async_load() (storage) before it's usable at
# all, which is real-HA-harness territory this test suite deliberately
# stays out of everywhere else. The function under test only ever reads
# registry.entities (a plain Mapping) and calls registry.async_remove(), so
# faking those is a complete, honest test of its own logic.
#
# Iterates registry.entities directly rather than going through
# er.async_entries_for_config_entry(), deliberately: that helper's
# secondary _config_entry_id_index lookup returned nothing for a real
# entity on real hardware whose own config_entry_id field matched — found
# live, 2026-08-18, while deploying this fix (event.study_action_button
# survived a first attempt built on that helper). Filtering the entries
# directly checks the field on each entry instead of trusting an index to
# be complete for every entity regardless of how or when it was created.

class _FakeEntityEntry:
    def __init__(self, entity_id, domain, config_entry_id="entry-1", unique_id=None):
        self.entity_id = entity_id
        self.domain = domain
        self.config_entry_id = config_entry_id
        self.unique_id = unique_id


class _FakeEntityRegistry:
    def __init__(self, entries):
        self.entities = {e.entity_id: e for e in entries}
        self.removed = []

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


def test_remove_stale_button_entities_removes_only_event_domain_entries(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry("event.a_action_button", "event"),
        _FakeEntityEntry("sensor.a_firmware_version", "sensor"),
        _FakeEntityEntry("number.a_volume", "number"),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_button_entities(_FakeHass(), _FakeEntry("entry-1"))

    assert registry.removed == ["event.a_action_button"]


def test_remove_stale_button_entities_ignores_a_different_config_entry(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry("event.other_action_button", "event", config_entry_id="entry-OTHER"),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_button_entities(_FakeHass(), _FakeEntry("entry-1"))

    assert registry.removed == []


def test_remove_stale_button_entities_is_a_quiet_no_op_when_nothing_matches(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([_FakeEntityEntry("sensor.a_firmware_version", "sensor")])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_button_entities(_FakeHass(), _FakeEntry())  # must not raise

    assert registry.removed == []


# ── _remove_stale_volume_number_entities ────────────────────────────────────
# Same fakes and same reasoning as _remove_stale_button_entities above — the
# Volume number entity (number.py) was replaced by a 9-level select.

def test_remove_stale_volume_number_entities_removes_only_number_domain_entries(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry("number.a_volume", "number"),
        _FakeEntityEntry("sensor.a_firmware_version", "sensor"),
        _FakeEntityEntry("select.a_volume_level", "select"),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_volume_number_entities(_FakeHass(), _FakeEntry("entry-1"))

    assert registry.removed == ["number.a_volume"]


def test_remove_stale_volume_number_entities_ignores_a_different_config_entry(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry("number.other_volume", "number", config_entry_id="entry-OTHER"),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_volume_number_entities(_FakeHass(), _FakeEntry("entry-1"))

    assert registry.removed == []


def test_remove_stale_volume_number_entities_is_a_quiet_no_op_when_nothing_matches(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([_FakeEntityEntry("sensor.a_firmware_version", "sensor")])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_volume_number_entities(_FakeHass(), _FakeEntry())  # must not raise

    assert registry.removed == []


# ── _remove_stale_mute_entities ─────────────────────────────────────────────
# The momentary button.py button and binary_sensor.py's old read-only mute
# sensor were replaced by one switch.py toggle entity. "button" is matched
# by whole domain (nothing else uses it); "binary_sensor" is matched by
# unique_id suffix so EchoOnlineSensor ("_online") is never touched.

def test_remove_stale_mute_entities_removes_the_button_and_the_mute_binary_sensor(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry("button.a_privacy_mute", "button", unique_id="A_mute_button"),
        _FakeEntityEntry("binary_sensor.a_privacy_mute", "binary_sensor", unique_id="A_muted"),
        _FakeEntityEntry("binary_sensor.a_online", "binary_sensor", unique_id="A_online"),
        _FakeEntityEntry("sensor.a_firmware_version", "sensor", unique_id="A_firmware"),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_mute_entities(_FakeHass(), _FakeEntry("entry-1"))

    assert sorted(registry.removed) == ["binary_sensor.a_privacy_mute", "button.a_privacy_mute"]


def test_remove_stale_mute_entities_ignores_a_different_config_entry(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry(
            "button.other_privacy_mute", "button",
            config_entry_id="entry-OTHER", unique_id="B_mute_button",
        ),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_mute_entities(_FakeHass(), _FakeEntry("entry-1"))

    assert registry.removed == []


def test_remove_stale_mute_entities_is_a_quiet_no_op_when_nothing_matches(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry("binary_sensor.a_online", "binary_sensor", unique_id="A_online"),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_mute_entities(_FakeHass(), _FakeEntry())  # must not raise

    assert registry.removed == []


def test_remove_stale_sendspin_music_entities_removes_only_the_retired_player(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry("media_player.a_music", "media_player", unique_id="A_sendspin_music"),
        _FakeEntityEntry("media_player.a_other", "media_player", unique_id="A_other"),
        _FakeEntityEntry(
            "media_player.other_music", "media_player", config_entry_id="entry-OTHER",
            unique_id="B_sendspin_music",
        ),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_sendspin_music_entities(_FakeHass(), _FakeEntry("entry-1"))

    assert registry.removed == ["media_player.a_music"]


# ── _remove_stale_diagnostic_sensor_entities ────────────────────────────────
# Firmware version / wake word model sensors were removed — matched by
# unique_id suffix so EchoAmbientLightSensor ("_ambient_light") stays.

def test_remove_stale_diagnostic_sensor_entities_removes_firmware_and_wake_model(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry("sensor.a_firmware_version", "sensor", unique_id="A_firmware"),
        _FakeEntityEntry("sensor.a_wake_word_model", "sensor", unique_id="A_wake_model"),
        _FakeEntityEntry("sensor.a_ambient_light", "sensor", unique_id="A_ambient_light"),
        _FakeEntityEntry("binary_sensor.a_online", "binary_sensor", unique_id="A_online"),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_diagnostic_sensor_entities(_FakeHass(), _FakeEntry("entry-1"))

    assert sorted(registry.removed) == ["sensor.a_firmware_version", "sensor.a_wake_word_model"]


def test_remove_stale_diagnostic_sensor_entities_ignores_a_different_config_entry(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry(
            "sensor.other_firmware_version", "sensor",
            config_entry_id="entry-OTHER", unique_id="B_firmware",
        ),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_diagnostic_sensor_entities(_FakeHass(), _FakeEntry("entry-1"))

    assert registry.removed == []


def test_remove_stale_diagnostic_sensor_entities_is_a_quiet_no_op_when_nothing_matches(monkeypatch):
    import homeassistant.helpers.entity_registry as er

    registry = _FakeEntityRegistry([
        _FakeEntityEntry("sensor.a_ambient_light", "sensor", unique_id="A_ambient_light"),
    ])
    monkeypatch.setattr(er, "async_get", lambda hass: registry)

    module._remove_stale_diagnostic_sensor_entities(_FakeHass(), _FakeEntry())  # must not raise

    assert registry.removed == []


def test_async_unload_entry_cancels_scanners_shuts_down_and_closes_client(monkeypatch):
    registered = _patch_collaborators(monkeypatch)
    hass = _FakeHass()
    entry = _FakeEntry()
    asyncio.run(module.async_setup_entry(hass, entry))
    coordinator = _FakeCoordinator.instances[0]
    client = _FakeClient.instances[0]
    coordinator.data = {"devices": [{"device_id": "A"}]}
    for listener in coordinator._listeners:
        listener()
    assert len(registered) == 1

    result = asyncio.run(module.async_unload_entry(hass, entry))

    assert result is True
    assert registered == []  # cancel() removed it
    assert coordinator.shutdown_called is True
    assert client.closed is True
    assert entry.entry_id not in hass.data[DOMAIN]
    assert hass.config_entries.unloaded == [(entry, tuple(PLATFORMS))]
