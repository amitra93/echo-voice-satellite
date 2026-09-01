import em_stop


def test_accepts_one_matching_detection_only():
    state = em_stop.StopState()
    assert state.arm(42, 1, "thinking", 20).action == "armed"
    assert state.detected(42, 1, "thinking", 10).action == "accept"
    assert state.detected(42, 1, "thinking", 10).reason == "duplicate detection"


def test_rejects_stale_generation_without_touching_new_arm():
    state = em_stop.StopState()
    state.arm(1, 1, "thinking", 20)
    state.arm(2, 2, "playback", 30)
    assert state.detected(1, 1, "thinking", 10).reason == "stale generation"
    assert state.arm_state.turn_id == 2


def test_rejects_wrong_turn_or_phase_and_expires_arm():
    state = em_stop.StopState()
    state.arm(1, 2, "playback", 20)
    assert state.detected(2, 2, "playback", 10).reason == "wrong turn"
    assert state.detected(1, 2, "thinking", 10).reason == "wrong phase"
    assert state.detected(1, 2, "playback", 20).reason == "arm expired"
    assert state.arm_state is None


def test_disarm_cannot_clear_newer_generation():
    state = em_stop.StopState()
    state.arm(1, 3, "timer", 20)
    assert state.disarm(2).reason == "stale generation"
    assert state.arm_state is not None
    assert state.disarm(3).action == "disarmed"
