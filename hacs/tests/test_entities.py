import importlib

import pytest


entities = importlib.import_module("custom_components.echo_voice_satellite.entities")


class _FakeCoordinator:
    def __init__(self, devices, known_capabilities=None):
        self.data = {"devices": devices}
        self.known_capabilities = known_capabilities or {}

    def async_add_listener(self, callback):
        callback()
        return lambda: None


def test_device_record_finds_by_device_id():
    coordinator = _FakeCoordinator([{"device_id": "A"}, {"device_id": "B", "label": "Lounge"}])
    assert entities.device_record(coordinator, "B") == {"device_id": "B", "label": "Lounge"}
    assert entities.device_record(coordinator, "missing") == {}


def test_add_dynamic_entities_gates_on_capability():
    coordinator = _FakeCoordinator([
        {"device_id": "A", "capabilities": ["ambient_light"]},
        {"device_id": "B", "capabilities": []},
    ])
    added = []
    entities.add_dynamic_entities(
        coordinator, added.extend, lambda record: [record["device_id"]], capability="ambient_light",
    )
    assert added == ["A"]


def test_add_dynamic_entities_keeps_known_capability_after_a_transient_drop():
    coordinator = _FakeCoordinator(
        [{"device_id": "A", "capabilities": []}],
        known_capabilities={"A": {"ambient_light"}},
    )
    added = []
    entities.add_dynamic_entities(
        coordinator, added.extend, lambda record: [record["device_id"]], capability="ambient_light",
    )
    assert added == ["A"]


def test_add_dynamic_entities_without_capability_adds_every_device_once():
    coordinator = _FakeCoordinator([{"device_id": "A"}, {"device_id": "B"}])
    added = []
    entities.add_dynamic_entities(coordinator, added.extend, lambda record: [record["device_id"]])
    assert sorted(added) == ["A", "B"]


def test_add_dynamic_entities_never_re_adds_an_already_seen_device():
    coordinator = _FakeCoordinator([{"device_id": "A"}])
    added = []
    remove = entities.add_dynamic_entities(coordinator, added.extend, lambda record: [record["device_id"]])
    # A later listener invocation (e.g. the coordinator's own async_add_listener
    # firing add_current again on a data refresh) must not duplicate.
    coordinator.async_add_listener(lambda: None)
    assert added == ["A"]
    remove()  # returned callable must be callable and require no arguments


# ── EchoCoordinatorEntity — the real base class, object.__new__'d to skip
# CoordinatorEntity.__init__'s listener wiring (which needs a live
# coordinator with working async_add_listener update-tracking, not needed to
# exercise this subclass's own overridden properties). ────────────────────

def _make_entity(coordinator, device_id="A"):
    entity = object.__new__(entities.EchoCoordinatorEntity)
    entity.coordinator = coordinator
    entity.device_id = device_id
    entity._observed = False
    return entity


class _AvailabilityCoordinator(_FakeCoordinator):
    def __init__(self, devices, control_available=True, last_update_success=True):
        super().__init__(devices)
        self.control_available = control_available
        self.last_update_success = last_update_success


def test_entity_record_is_this_devices_row():
    coordinator = _AvailabilityCoordinator([{"device_id": "A", "label": "Lounge"}])
    entity = _make_entity(coordinator)
    assert entity.record == {"device_id": "A", "label": "Lounge"}


def test_entity_available_requires_control_link_refresh_and_connection():
    coordinator = _AvailabilityCoordinator([{"device_id": "A", "connected": True}])
    entity = _make_entity(coordinator)
    assert entity.available is True

    coordinator.control_available = False
    assert entity.available is False

    coordinator.control_available = True
    coordinator.last_update_success = False
    assert entity.available is False

    coordinator.last_update_success = True
    coordinator.data = {"devices": [{"device_id": "A", "connected": False}]}
    assert entity.available is False


def test_entity_device_info_falls_back_to_device_id_with_no_label():
    coordinator = _AvailabilityCoordinator([{"device_id": "A"}])
    entity = _make_entity(coordinator)
    info = entity.device_info
    assert info["identifiers"] == {(entities.DOMAIN, "A")}
    assert info["name"] == "A"
    assert info["manufacturer"] == "EchoMuse"
    assert info["model"] == entities.DEVICE_MODEL


def test_entity_device_info_prefers_label_and_carries_firmware_version():
    coordinator = _AvailabilityCoordinator([
        {"device_id": "A", "label": "Lounge", "firmware_ver": "v2.20.0"}
    ])
    entity = _make_entity(coordinator)
    info = entity.device_info
    assert info["name"] == "Lounge"
    assert info["sw_version"] == "v2.20.0"


def test_capability_seen_latches_true_and_survives_a_later_omission():
    coordinator = _AvailabilityCoordinator([{"device_id": "A", "capabilities": ["mic"]}])
    entity = _make_entity(coordinator)
    assert entity.capability_seen("mic") is True

    coordinator.data = {"devices": [{"device_id": "A", "capabilities": []}]}
    # Once observed, capability_seen never reports False again for this
    # entity instance — that's the "latch" add_dynamic_entities relies on
    # to keep a capability entity registered through a transient drop.
    assert entity.capability_seen("mic") is True


def test_capability_available_requires_both_latched_and_currently_present():
    coordinator = _AvailabilityCoordinator([{"device_id": "A", "capabilities": ["mic"]}])
    entity = _make_entity(coordinator)
    assert entity.capability_available("mic") is True  # latches AND present

    coordinator.data = {"devices": [{"device_id": "A", "capabilities": []}]}
    assert entity.capability_available("mic") is False  # latched but not present now

    never_seen = _make_entity(coordinator)
    assert never_seen.capability_available("mic") is False  # never latched at all
