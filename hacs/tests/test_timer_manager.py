import pytest

from custom_components.echo_voice_satellite import _timer_action, _timer_snapshot


class Timer:
    def __init__(self, timer_id, *, conversation_command=None):
        self.id = timer_id
        self.name = f"Timer {timer_id}"
        self.created_seconds = 60
        self.seconds_left = 42
        self.is_active = True
        self.device_id = "ha-device"
        self.area_name = "Kitchen"
        self.conversation_command = conversation_command


class Manager:
    def __init__(self):
        self.timers = {"one": Timer("one"), "hidden": Timer("hidden", conversation_command="do thing")}
        self.calls = []

    def pause_timer(self, timer_id): self.calls.append(("pause", timer_id))
    def unpause_timer(self, timer_id): self.calls.append(("resume", timer_id))
    def cancel_timer(self, timer_id): self.calls.append(("cancel", timer_id))
    def _timer_finished(self, timer_id): self.calls.append(("finish", timer_id))


def test_timer_snapshot_exposes_native_timer_data_but_not_delayed_commands():
    assert _timer_snapshot(Manager()) == [{
        "id": "one", "name": "Timer one", "seconds": 60,
        "seconds_left": 42, "is_active": True, "device_id": "ha-device",
        "area_name": "Kitchen",
    }]


@pytest.mark.parametrize("action", ["pause", "resume", "cancel", "finish"])
def test_timer_actions_target_the_exact_native_timer_id(action):
    manager = Manager()
    assert _timer_action(manager, action, "one")
    assert manager.calls == [(action, "one")]


def test_unknown_timer_action_does_not_mutate_timer_manager():
    manager = Manager()
    assert not _timer_action(manager, "delete_everything", "one")
    assert manager.calls == []
