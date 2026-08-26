"""EchoVoiceSatelliteCoordinator against the real homeassistant base class.

Instances are built with object.__new__() rather than the normal
DataUpdateCoordinator.__init__() — that constructor wires refresh scheduling,
a Debouncer, and shutdown hooks through a fully running `hass`, which is more
machinery than this integration's own logic needs to exercise. Every method
under test still runs the REAL method body inherited from
homeassistant.helpers.update_coordinator, so a renamed/removed HA API still
surfaces here; only __init__ is bypassed, the same pattern the fork used for
EchoAssistSatellite (see the git history this package was adapted from).
"""

import asyncio
import importlib

import pytest


from homeassistant.helpers.update_coordinator import UpdateFailed

coordinator_module = importlib.import_module("custom_components.echo_voice_satellite.coordinator")
EchoVoiceSatelliteCoordinator = coordinator_module.EchoVoiceSatelliteCoordinator
entities_module = importlib.import_module("custom_components.echo_voice_satellite.entities")


def _make_entity(coordinator, device_id="A"):
    # Same object.__new__ pattern test_entities.py uses to skip
    # CoordinatorEntity.__init__'s listener wiring — everything this needs
    # (.available, .record) is the real EchoCoordinatorEntity method body.
    entity = object.__new__(entities_module.EchoCoordinatorEntity)
    entity.coordinator = coordinator
    entity.device_id = device_id
    entity._observed = False
    return entity


class _FakeClient:
    def __init__(self):
        self.event_handler = None
        self.disconnect_handler = None
        self.connect_calls = 0
        self.close_calls = 0
        self.snapshot_ready = asyncio.Event()
        self.snapshot = None
        self.devices_reply = []
        self.connect_should_fail = False

    def set_event_handler(self, cb):
        self.event_handler = cb

    def set_disconnect_handler(self, cb):
        self.disconnect_handler = cb

    async def async_connect(self):
        self.connect_calls += 1
        if self.connect_should_fail:
            raise coordinator_module.ControllerError("controller_unreachable")

    async def async_close(self):
        self.close_calls += 1

    async def async_get_devices(self):
        return self.devices_reply

    @staticmethod
    def reconnect_delay(attempt):
        return 0.0  # instant, for fast reconnect-loop tests


class _FakeEntry:
    def __init__(self, data=None, options=None):
        self.data = data or {}
        self.options = options or {}


class _FakeConfigEntries:
    def __init__(self):
        self.updates = []

    def async_update_entry(self, entry, data):
        self.updates.append(data)
        entry.data = data


class _FakeHass:
    def __init__(self):
        self.config_entries = _FakeConfigEntries()


def _make_coordinator(client=None, entry=None):
    coordinator = object.__new__(EchoVoiceSatelliteCoordinator)
    coordinator.client = client or _FakeClient()
    coordinator.entry = entry
    coordinator.hass = _FakeHass()
    coordinator.known_capabilities = {}
    coordinator.control_available = False
    coordinator._reconnect_task = None
    coordinator._event_listeners = []
    coordinator.data = None
    coordinator.last_update_success = True
    coordinator.logger = __import__("logging").getLogger("test")
    # async_set_updated_data / async_update_listeners in the real base class
    # touch the listener/scheduling machinery this test doesn't set up —
    # replace with the minimal behaviour this module's own code depends on.
    updated = []

    def set_updated_data(data):
        coordinator.data = data
        updated.append(data)

    coordinator.async_set_updated_data = set_updated_data
    coordinator.async_update_listeners = lambda: updated.append(coordinator.data)
    return coordinator, updated


def test_snapshot_event_replaces_devices_and_remembers_capabilities():
    coordinator, updated = _make_coordinator()

    asyncio.run(coordinator._async_event({
        "type": "snapshot",
        "devices": [{"device_id": "A", "capabilities": ["ambient_light"]}],
    }))

    assert coordinator.data == {"devices": [{"device_id": "A", "capabilities": ["ambient_light"]}]}
    assert coordinator.known_capabilities["A"] == {"ambient_light"}
    assert updated  # async_set_updated_data was called


@pytest.mark.parametrize("event, field, value", [
    ({"type": "ambient_light", "device_id": "A", "lux": 42}, "ambient_light_lux", 42),
    ({"type": "volume_state", "device_id": "A", "volume": 0.6}, "volume", 0.6),
    ({"type": "wake_model", "device_id": "A", "model_id": "hey_jarvis"}, "wake_model_id", "hey_jarvis"),
    ({"type": "mute_state", "device_id": "A", "muted": True}, "muted", True),
])
def test_state_events_merge_the_mapped_field_in_place(event, field, value):
    coordinator, _ = _make_coordinator()
    coordinator.data = {"devices": [{"device_id": "A"}, {"device_id": "B"}]}

    asyncio.run(coordinator._async_event(event))

    devices = {d["device_id"]: d for d in coordinator.data["devices"]}
    assert devices["A"][field] == value
    assert field not in devices["B"]


def test_device_update_merges_its_state_dict_into_the_record():
    # The controller pushes device_update{state:{connected,muted,...}} on
    # device connect and on transient-state changes. Merging it into the
    # record is what lets a reconnected device clear "unavailable" live,
    # rather than waiting on a REST poll (the bug that stuck entities
    # unavailable for hours after a controller restart, 2026-08-19).
    coordinator, updated = _make_coordinator()
    coordinator.data = {"devices": [
        {"device_id": "A", "connected": False, "muted": False},
        {"device_id": "B", "connected": True},
    ]}

    asyncio.run(coordinator._async_event({
        "type": "device_update", "device_id": "A",
        "state": {"connected": True, "muted": True, "speaking": True},
    }))

    devices = {d["device_id"]: d for d in coordinator.data["devices"]}
    assert devices["A"]["connected"] is True
    assert devices["A"]["muted"] is True
    assert devices["A"]["speaking"] is True
    assert devices["B"] == {"device_id": "B", "connected": True}  # untouched
    assert updated  # entities were notified


def test_device_update_with_no_state_dict_is_harmless():
    coordinator, _ = _make_coordinator()
    coordinator.data = {"devices": [{"device_id": "A", "connected": True}]}

    # e.g. a device_update carrying only a non-dict payload — must not raise
    # or corrupt the record.
    asyncio.run(coordinator._async_event(
        {"type": "device_update", "device_id": "A", "state": None}
    ))

    assert coordinator.data["devices"][0] == {"device_id": "A", "connected": True}


def test_device_disconnected_flips_connected_off():
    coordinator, updated = _make_coordinator()
    coordinator.data = {"devices": [
        {"device_id": "A", "connected": True},
        {"device_id": "B", "connected": True},
    ]}

    asyncio.run(coordinator._async_event(
        {"type": "device_disconnected", "device_id": "A"}
    ))

    devices = {d["device_id"]: d for d in coordinator.data["devices"]}
    assert devices["A"]["connected"] is False
    assert devices["B"]["connected"] is True  # untouched
    assert updated


def test_device_update_still_reaches_event_listeners():
    coordinator, _ = _make_coordinator()
    coordinator.data = {"devices": [{"device_id": "A", "connected": False}]}
    received = []

    async def collect(event):
        received.append(event)

    coordinator.async_add_event_listener(collect)
    event = {"type": "device_update", "device_id": "A", "state": {"connected": True}}
    asyncio.run(coordinator._async_event(event))

    assert received == [event]


def test_capabilities_event_updates_record_and_known_capabilities():
    coordinator, _ = _make_coordinator()
    coordinator.data = {"devices": [{"device_id": "A", "capabilities": []}]}

    asyncio.run(coordinator._async_event(
        {"type": "capabilities", "device_id": "A", "capabilities": ["button_hold"]}
    ))

    assert coordinator.data["devices"][0]["capabilities"] == ["button_hold"]
    assert coordinator.known_capabilities["A"] == {"button_hold"}


def test_unmapped_events_reach_listeners_without_touching_device_data():
    coordinator, _ = _make_coordinator()
    coordinator.data = {"devices": [{"device_id": "A"}]}
    received = []

    async def collect(event):
        received.append(event)

    coordinator.async_add_event_listener(collect)
    event = {"type": "wake.offer", "device_id": "A", "turn_id": 5, "trigger": "wakeword"}
    asyncio.run(coordinator._async_event(event))

    assert received == [event]
    assert coordinator.data == {"devices": [{"device_id": "A"}]}  # unchanged


def test_event_listener_can_be_removed():
    coordinator, _ = _make_coordinator()
    seen = []

    async def cb(event):
        seen.append(event)

    remove = coordinator.async_add_event_listener(cb)
    asyncio.run(coordinator._emit_event({"type": "x"}))
    remove()
    asyncio.run(coordinator._emit_event({"type": "y"}))

    assert seen == [{"type": "x"}]


def test_remember_capabilities_persists_to_the_config_entry_only_on_change():
    entry = _FakeEntry(data={"known_capabilities": {}})
    coordinator, _ = _make_coordinator(entry=entry)

    coordinator._remember_capabilities([{"device_id": "A", "capabilities": ["mic"]}])
    assert coordinator.hass.config_entries.updates  # persisted once
    persisted_count = len(coordinator.hass.config_entries.updates)

    coordinator._remember_capabilities([{"device_id": "A", "capabilities": ["mic"]}])
    assert len(coordinator.hass.config_entries.updates) == persisted_count  # no-op, unchanged


def test_remember_capabilities_survives_a_missing_config_entry():
    coordinator, _ = _make_coordinator(entry=None)
    # Must not raise even though there's nowhere to persist to.
    coordinator._remember_capabilities([{"device_id": "A", "capabilities": ["mic"]}])
    assert coordinator.known_capabilities["A"] == {"mic"}


def test_merge_rest_devices_seeds_data_on_first_poll():
    coordinator, _ = _make_coordinator()
    assert coordinator.data is None

    merged = coordinator._merge_rest_devices([{"device_id": "A"}])
    assert merged == {"devices": [{"device_id": "A"}]}


def test_merge_rest_devices_updates_existing_and_appends_new():
    coordinator, _ = _make_coordinator()
    coordinator.data = {"devices": [{"device_id": "A", "label": "Old"}]}

    merged = coordinator._merge_rest_devices([
        {"device_id": "A", "label": "New"},
        {"device_id": "B", "label": "Fresh"},
    ])

    by_id = {d["device_id"]: d for d in merged["devices"]}
    assert by_id["A"]["label"] == "New"
    assert by_id["B"]["label"] == "Fresh"


def test_merge_rest_devices_drops_a_device_absent_from_the_poll():
    # A device present in the last snapshot but absent from a REST poll is
    # gone from the registry (deprovisioned) — never merely offline, since
    # db.get_all_devices() (what /api/devices serves) returns every known
    # device regardless of connection state.
    coordinator, _ = _make_coordinator()
    coordinator.data = {"devices": [{"device_id": "A"}, {"device_id": "B"}]}

    merged = coordinator._merge_rest_devices([{"device_id": "A"}])

    assert [d["device_id"] for d in merged["devices"]] == ["A"]


def test_async_update_data_wraps_controller_error_as_update_failed():
    client = _FakeClient()
    coordinator, _ = _make_coordinator(client=client)

    async def failing_get_devices():
        raise coordinator_module.ControllerError("controller_unreachable")

    client.async_get_devices = failing_get_devices

    with pytest.raises(UpdateFailed):
        asyncio.run(coordinator._async_update_data())


def test_async_update_data_returns_merged_devices_on_success():
    client = _FakeClient()
    client.devices_reply = [{"device_id": "A"}]
    coordinator, _ = _make_coordinator(client=client)

    result = asyncio.run(coordinator._async_update_data())
    assert result == {"devices": [{"device_id": "A"}]}


def test_async_connect_control_success_sets_control_available():
    client = _FakeClient()
    coordinator, _ = _make_coordinator(client=client)

    async def run():
        client.snapshot = {"devices": [{"device_id": "A"}]}
        client.snapshot_ready.set()
        await coordinator.async_connect_control()

    asyncio.run(run())
    assert coordinator.control_available is True
    assert coordinator.data == {"devices": [{"device_id": "A"}]}
    assert client.connect_calls == 1


def test_async_connect_control_sets_control_available_before_notifying_entities():
    """Regression: on a live WS reconnect, control_available must be True at
    the instant async_set_updated_data fires its listener notification.

    async_set_updated_data synchronously re-evaluates every entity's
    `available` (which reads coordinator.control_available). Setting the flag
    AFTER that call left every entity written "unavailable" with nothing to
    re-notify them, so they stayed stuck unavailable for hours after a
    controller restart even though the WS had reconnected and voice turns
    worked (those ride _emit_event, not async_set_updated_data). A fresh
    setup/reload hid it because entities are added after this completes; only
    a reconnect exposed it. See coordinator.py's comment at the reorder.
    """
    client = _FakeClient()
    coordinator, _ = _make_coordinator(client=client)

    seen_control_available = []
    real_set = coordinator.async_set_updated_data

    def capturing_set(data):
        seen_control_available.append(coordinator.control_available)
        real_set(data)

    coordinator.async_set_updated_data = capturing_set

    async def run():
        client.snapshot = {"devices": [{"device_id": "A", "connected": True}]}
        client.snapshot_ready.set()
        await coordinator.async_connect_control()

    asyncio.run(run())
    assert seen_control_available == [True], (
        "control_available was still False when entities were notified — they "
        "would have recomputed available=False and stuck there"
    )


# ── End-to-end reconnect scenarios ──────────────────────────────────────────
#
# The tests above check coordinator-internal state (control_available,
# coordinator.data) in isolation. These two drive a real EchoCoordinatorEntity
# through the same sequence a live reconnect produces and assert on
# `entity.available` itself — the actual thing a user sees flip between
# "unavailable" and a real state — reproducing both halves of the
# 2026-08-19 incident (5.5 hours stuck unavailable after a controller
# restart, fixed by control_available ordering + a live device_update signal)
# end to end rather than one internal field at a time.

def test_entity_is_available_at_the_exact_notification_instant_on_reconnect():
    """
    Bug 1 (ordering), at entity level rather than the raw control_available
    flag: captures `entity.available` — not just `coordinator.control_available`
    — at the instant async_set_updated_data notifies listeners, which is what
    a real CoordinatorEntity's _handle_coordinator_update actually reads
    before calling async_write_ha_state().

    This distinction matters: an EARLIER version of this test read
    entity.available only AFTER async_connect_control() had fully returned,
    which passed regardless of the ordering bug — `available` is a live
    property, so by the time the whole async method has returned,
    control_available is already True either way. Only a value captured
    AT the notification moment (mirroring the narrow
    test_async_connect_control_sets_control_available_before_notifying_entities
    above, which does the same for the raw flag) can actually distinguish
    the fixed ordering from the reintroduced bug — confirmed by deliberately
    reverting the fix and watching this test fail before writing it this way.
    """
    client = _FakeClient()
    coordinator, _ = _make_coordinator(client=client)
    entity = _make_entity(coordinator)

    seen_available = []
    real_set = coordinator.async_set_updated_data

    def capturing_set(data):
        # AFTER, unlike the narrow control_available test's BEFORE: the real
        # base class assigns self.data (which entity.record/.available both
        # read) inside async_set_updated_data itself, then synchronously
        # fires listeners — so the moment a real CoordinatorEntity reads
        # `.available` is after data lands, not before. Checking before here
        # would see coordinator.data still None regardless of the ordering
        # fix, for an unrelated reason, and the test would be meaningless.
        real_set(data)
        seen_available.append(entity.available)

    coordinator.async_set_updated_data = capturing_set

    async def run():
        client.snapshot = {"devices": [{"device_id": "A", "connected": True}]}
        client.snapshot_ready.set()
        await coordinator.async_connect_control()

    asyncio.run(run())
    assert seen_available == [True], (
        "entity.available was False at the moment entities were notified — a "
        "real entity would have written 'unavailable' to HA's state machine "
        "at exactly this point, then had nothing re-notify it"
    )


def test_entity_recovers_via_device_update_when_the_device_reconnects_after_the_snapshot():
    """
    Bug 2 end to end: on a real controller restart the HACS WS reconnects
    before the Echo device does, so the reconnect snapshot legitimately
    shows connected=False — the entity is correctly unavailable at that
    point, not a bug. The bug was that nothing corrected it once the device
    DID reconnect a few seconds later, short of a REST poll that push events
    kept deferring. em_controller now announces the device reconnect via a
    device_update event (_push_device_state); this proves that alone is
    enough to bring the entity back, with no poll and no reload.
    """
    client = _FakeClient()
    coordinator, _ = _make_coordinator(client=client)
    entity = _make_entity(coordinator)

    async def run():
        client.snapshot = {"devices": [{"device_id": "A", "connected": False}]}
        client.snapshot_ready.set()
        await coordinator.async_connect_control()
        assert entity.available is False  # device not back yet — correct, not stuck

        await coordinator._async_event({
            "type": "device_update", "device_id": "A",
            "state": {"connected": True},
        })

    asyncio.run(run())
    assert entity.available is True


def test_async_connect_control_raises_update_failed_on_connect_error():
    client = _FakeClient()
    client.connect_should_fail = True
    coordinator, _ = _make_coordinator(client=client)

    with pytest.raises(UpdateFailed):
        asyncio.run(coordinator.async_connect_control())
    assert coordinator.control_available is False


def test_async_connect_control_times_out_if_no_snapshot_arrives():
    client = _FakeClient()
    coordinator, _ = _make_coordinator(client=client)
    # snapshot_ready is never set — the real 10s timeout would be too slow
    # for a test, so shrink it via a patched asyncio.wait_for is overkill;
    # instead assert against a coordinator whose wait is bounded by a
    # pre-cancelled event scenario is equivalent: exercise the code path
    # with a short monkeypatched timeout instead.
    import asyncio as _asyncio

    real_wait_for = _asyncio.wait_for

    async def short_wait_for(aw, timeout):
        return await real_wait_for(aw, timeout=0.05)

    coordinator_module.asyncio.wait_for = short_wait_for
    try:
        with pytest.raises(UpdateFailed, match="snapshot"):
            asyncio.run(coordinator.async_connect_control())
    finally:
        coordinator_module.asyncio.wait_for = real_wait_for


def test_control_disconnected_marks_unavailable_and_schedules_reconnect():
    client = _FakeClient()
    coordinator, updated = _make_coordinator(client=client)
    coordinator.control_available = True

    async def run():
        await coordinator._async_control_disconnected()
        assert coordinator.control_available is False
        assert coordinator._reconnect_task is not None
        # Let the reconnect loop actually attempt at least once before
        # flipping control_available back — setting it first would let the
        # loop's own `while not self.control_available` guard exit before
        # the task ever got scheduled, and this assertion would test nothing.
        for _ in range(50):
            if client.close_calls >= 1:
                break
            await asyncio.sleep(0.01)
        assert client.close_calls >= 1
        coordinator.control_available = True
        await coordinator.async_shutdown()

    asyncio.run(run())


def test_async_reconnect_retries_until_connect_succeeds():
    client = _FakeClient()
    coordinator, _ = _make_coordinator(client=client)
    attempts = {"n": 0}

    async def flaky_connect():
        # Real async_connect_control() is what sets control_available=True
        # on success — the loop's exit condition — so the stub must too.
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise coordinator_module.ControllerError("controller_unreachable")
        coordinator.control_available = True

    coordinator.async_connect_control = flaky_connect

    asyncio.run(asyncio.wait_for(coordinator._async_reconnect(), timeout=2.0))

    assert attempts["n"] == 3
    assert coordinator.control_available is True


def test_async_shutdown_cancels_pending_reconnect_task():
    coordinator, _ = _make_coordinator()

    async def run():
        async def never_finishes():
            await asyncio.sleep(100)

        coordinator._reconnect_task = asyncio.create_task(never_finishes())
        await coordinator.async_shutdown()
        assert coordinator._reconnect_task is None

    asyncio.run(run())
