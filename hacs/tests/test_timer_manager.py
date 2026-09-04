"""Pure timer_card.py logic: TimerManager row-building, actions, and the
ringing/queued presentation bridge — none of it touches Home Assistant, so
none of it needs a running one (see timer_card.py's module docstring)."""

from datetime import datetime, timezone

import pytest

from custom_components.echo_voice_satellite.timer_card import (
    ACTIVE, PAUSED, QUEUED, RINGING,
    AlarmPresence, apply_timer_action, build_snapshot, manager_rows, start_timer,
)


class Timer:
    def __init__(
        self, timer_id, *, conversation_command=None, is_active=True,
        seconds_left=42, created_seconds=60, device_id="ha-device",
    ):
        self.id = timer_id
        self.name = f"Timer {timer_id}"
        self.created_seconds = created_seconds
        self.seconds_left = seconds_left
        self.is_active = is_active
        self.device_id = device_id
        self.area_name = "Kitchen"
        self.conversation_command = conversation_command


class Manager:
    def __init__(self, timers=None):
        self.timers = timers if timers is not None else {
            "one": Timer("one"), "hidden": Timer("hidden", conversation_command="do thing"),
        }
        self.calls = []
        self.raise_on = set()

    def _maybe_raise(self, timer_id):
        if timer_id in self.raise_on:
            raise LookupError("no such timer")

    def pause_timer(self, timer_id):
        self._maybe_raise(timer_id)
        self.calls.append(("pause", timer_id))

    def unpause_timer(self, timer_id):
        self._maybe_raise(timer_id)
        self.calls.append(("resume", timer_id))

    def cancel_timer(self, timer_id):
        self._maybe_raise(timer_id)
        self.calls.append(("cancel", timer_id))

    def add_time(self, timer_id, seconds):
        self._maybe_raise(timer_id)
        self.calls.append(("change", timer_id, seconds))

    def start_timer(self, device_id, hours, minutes, seconds, language, name=None):
        if device_id is None:
            raise ValueError("device_id required")
        self.calls.append(("start", device_id, hours, minutes, seconds, language, name))
        return "new-timer-id"


def _name(device_id):
    return {"ha-device": "Kitchen Echo"}.get(device_id)


def _devices():
    return [{"device_id": "ha-device", "device_name": "Kitchen Echo"}]


# ── manager_rows ─────────────────────────────────────────────────────────────

def test_manager_rows_exposes_the_documented_data_contract_shape():
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    rows = manager_rows(Manager(), _name, now=now)
    assert rows == [{
        "id": "one", "device_id": "ha-device", "device_name": "Kitchen Echo",
        "name": "Timer one", "state": ACTIVE, "duration_seconds": 60,
        "remaining_seconds": 42, "finishes_at": "2026-08-29T12:00:42+00:00",
        "area_name": "Kitchen",
    }]


def test_manager_rows_excludes_delayed_command_timers():
    rows = manager_rows(Manager(), _name)
    assert [row["id"] for row in rows] == ["one"]


def test_manager_rows_omits_finishes_at_for_a_paused_timer():
    manager = Manager({"one": Timer("one", is_active=False, seconds_left=30)})
    rows = manager_rows(manager, _name)
    assert rows[0]["state"] == PAUSED
    assert rows[0]["finishes_at"] is None
    assert rows[0]["remaining_seconds"] == 30


# ── apply_timer_action ───────────────────────────────────────────────────────

@pytest.mark.parametrize("action", ["pause", "resume", "cancel"])
def test_actions_target_the_exact_timer_id(action):
    manager = Manager()
    assert apply_timer_action(manager, action, "one")
    assert manager.calls == [(action, "one")]


def test_change_action_adds_or_removes_time():
    manager = Manager()
    assert apply_timer_action(manager, "change", "one", seconds=-30)
    assert manager.calls == [("change", "one", -30)]


def test_change_action_without_seconds_is_rejected_without_touching_the_manager():
    manager = Manager()
    assert not apply_timer_action(manager, "change", "one")
    assert manager.calls == []


def test_unknown_action_does_not_mutate_timer_manager():
    manager = Manager()
    assert not apply_timer_action(manager, "delete_everything", "one")
    assert manager.calls == []


def test_action_on_an_unknown_timer_id_fails_closed_rather_than_raising():
    manager = Manager()
    manager.raise_on.add("missing")
    assert not apply_timer_action(manager, "pause", "missing")


# ── start_timer ──────────────────────────────────────────────────────────────

def test_start_timer_passes_through_to_the_manager_and_returns_its_id():
    manager = Manager()
    timer_id = start_timer(
        manager, "en", device_id="ha-device", minutes=5, name="pizza",
    )
    assert timer_id == "new-timer-id"
    assert manager.calls == [("start", "ha-device", None, 5, None, "en", "pizza")]


def test_start_timer_with_no_device_fails_closed_rather_than_raising():
    manager = Manager()
    assert start_timer(manager, "en", device_id=None) is None


# ── AlarmPresence ────────────────────────────────────────────────────────────

def _alarm_event(device_id="echomuse-1", *, current=None, queue=()):
    return {"type": "timer.alarm", "device_id": device_id, "current": current, "queue": list(queue)}


def test_alarm_presence_synthesizes_ringing_and_queued_rows():
    presence = AlarmPresence()
    presence.update(_alarm_event(
        current={"timer_id": "t1", "name": "pizza", "total_seconds": 600, "ha_device_id": "ha-device"},
        queue=[{"timer_id": "t2", "name": "pasta", "total_seconds": 300, "ha_device_id": "ha-device"}],
    ))

    rows = presence.rows(known_timer_ids=set())

    assert {row["id"]: row["state"] for row in rows} == {"t1": RINGING, "t2": QUEUED}
    assert rows[0]["remaining_seconds"] == 0
    assert rows[0]["finishes_at"] is None


def test_alarm_presence_rows_exclude_timers_timermanager_still_has():
    presence = AlarmPresence()
    presence.update(_alarm_event(current={"timer_id": "t1", "ha_device_id": "ha-device"}))
    assert presence.rows(known_timer_ids={"t1"}) == []


def test_alarm_presence_update_replaces_the_previous_snapshot_wholesale():
    presence = AlarmPresence()
    presence.update(_alarm_event(current={"timer_id": "t1", "ha_device_id": "ha-device"}))
    presence.update(_alarm_event(current=None, queue=[]))
    assert presence.rows(known_timer_ids=set()) == []


def test_alarm_presence_maps_a_ringing_timer_back_to_its_echomuse_device():
    presence = AlarmPresence()
    presence.update(_alarm_event("echomuse-1", current={"timer_id": "t1", "ha_device_id": "ha-device"}))
    presence.update(_alarm_event("echomuse-2", queue=[{"timer_id": "t2", "ha_device_id": "ha-device"}]))

    assert presence.echomuse_device_for_timer("t1") == "echomuse-1"
    assert presence.echomuse_device_for_timer("t2") == "echomuse-2"
    assert presence.echomuse_device_for_timer("unknown") is None


def test_alarm_presence_ignores_an_event_with_no_device_id():
    presence = AlarmPresence()
    presence.update({"type": "timer.alarm", "current": None, "queue": []})
    assert presence.rows(known_timer_ids=set()) == []


# ── build_snapshot ───────────────────────────────────────────────────────────

def test_build_snapshot_merges_timermanager_and_ringing_rows_plus_devices():
    manager = Manager({"one": Timer("one")})
    presence = AlarmPresence()
    presence.update(_alarm_event(current={"timer_id": "t1", "name": "pizza", "ha_device_id": "ha-device"}))

    snapshot = build_snapshot(manager, presence, _name, _devices)

    assert {row["id"] for row in snapshot["timers"]} == {"one", "t1"}
    ringing = next(row for row in snapshot["timers"] if row["id"] == "t1")
    assert ringing["state"] == RINGING
    assert ringing["device_name"] == "Kitchen Echo"
    assert snapshot["devices"] == [{"device_id": "ha-device", "device_name": "Kitchen Echo"}]


def test_build_snapshot_with_no_manager_still_reports_ringing_alarms():
    presence = AlarmPresence()
    presence.update(_alarm_event(current={"timer_id": "t1", "ha_device_id": "ha-device"}))
    snapshot = build_snapshot(None, presence, _name, _devices)
    assert [row["id"] for row in snapshot["timers"]] == ["t1"]
