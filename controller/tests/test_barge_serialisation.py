"""
The ESPHome voice protocol used to serialise pipeline runs at the SATELLITE
or not at all — `VoiceAssistantEventResponse` carried no run identifier, so a
client structurally could not attribute an event to a particular run, and
Home Assistant's `handle_pipeline_start` overwrote `_pipeline_task` without
cancelling the previous one. Barge-in was the one place two runs could
overlap, and it did: measured 2026-08-17, five barge-ins, five interrupting
turns dead in 4-17ms with zero audio captured, because the aborted run's
RUN_END arrived ~4ms after the new turn started and the "HA ended a run it
never started" branch read it as terminal.

`em_runbarrier.RunBarrier` is the state machine that fixed it, split out of
`em_esphome` (now deleted, docs/design/full-duplex-plan.md's Phase 4 cutover) for the
reason `em_linkauth.decide` was: the suite could not import `em_esphome` (it
pulled in zeroconf, aiohttp and the database), so logic that lived there had
no coverage. `em_turn_engine.py` — the ESPHome-era satellite's replacement —
does not manually parse RUN_START/RUN_END at all; it drives HA's pipeline
through `AssistSatelliteEntity.async_accept_pipeline_from_satellite` (in
`hacs/`'s `assist_satellite.py`), which owns run-boundary bookkeeping
internally instead of us doing it by hand over raw protocol events. The
RunBarrier CLASS's own logic below is still correct and still kept — it is
exactly the tool the barge-in-abort follow-up in `em_turn_engine.py`'s module
docstring will need — but nothing currently instantiates it, and the wiring
tests that used to pin the ESPHome-specific call sites (`self._barrier.
begin_turn()`, `VoiceAssistantRequest(start=False)`, the RUN_START/RUN_END
discriminator) were pinning source that no longer exists. Device wake
admission now owns the barge path: it calls `turn_engine.admit_barge`, which
cancels the exact active turn before routing the replacement request.
"""

from pathlib import Path

from em_runbarrier import RunBarrier

CONTROLLER_SRC = (Path(__file__).resolve().parents[1] / "em_controller.py").read_text()
TURN_ENGINE_SRC = (Path(__file__).resolve().parents[1] / "em_turn_engine.py").read_text()


# ── The barrier ──────────────────────────────────────────────────────────────


def test_an_untouched_barrier_discards_nothing():
    """
    The overwhelmingly common case: no barge, no abort, every event delivered.
    """
    b = RunBarrier()
    b.begin_turn()
    assert b.discards(is_run_start=False) is False
    assert b.discards(is_run_start=True) is False


def test_an_abort_arms_the_NEXT_turn_not_the_current_one():
    """
    The abort happens during the turn being abandoned, and that turn still has
    tearing-down of its own to do. It is the turn AFTER it that must not see
    the old run's tail.
    """
    b = RunBarrier()
    b.begin_turn()
    b.abort()
    assert b.discards(is_run_start=False) is False, "armed the turn doing the aborting"
    b.end_turn()

    b.begin_turn()
    assert b.discards(is_run_start=False) is True


def test_the_stale_tail_is_discarded_until_run_start():
    """
    The measured failure. RUN_END, STT_VAD_END and the orphan's eventual ERROR
    all arrived on the new turn; each one alone is enough to kill it.
    """
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=False) is True   # stale RUN_END
    assert b.discards(is_run_start=False) is True   # stale STT_VAD_END
    assert b.discards(is_run_start=False) is True   # stale ERROR


def test_run_start_releases_the_barrier_and_is_itself_delivered():
    """
    RUN_START is the release AND a real event. Swallowing it would leave
    `_run_started` False and re-arm the very bug this fixes, for the turn's
    own terminal RUN_END.
    """
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=True) is False
    assert b.active is False


def test_everything_after_run_start_is_delivered():
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    b.discards(is_run_start=False)                  # stale, dropped
    b.discards(is_run_start=True)                   # ours, releases
    assert b.discards(is_run_start=False) is False
    assert b.discards(is_run_start=False) is False


def test_the_barrier_does_not_outlive_its_turn():
    """
    If HA never sends the RUN_START we are waiting for — connection dropped,
    pipeline failed to start — the barrier must come down when the turn ends.
    Events are dispatched whether or not a turn is in progress, so a barrier
    left standing discards them indefinitely.

    `begin_turn` would also clear it, but only once another turn starts; that
    is not a bound, because the turn that would clear it is one whose events
    are being discarded.
    """
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=False) is True
    b.end_turn()                                    # RUN_START never came
    assert b.active is False
    assert b.discards(is_run_start=False) is False


def test_the_barrier_is_bounded_to_one_turn():
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=False) is True
    b.end_turn()

    b.begin_turn()
    assert b.discards(is_run_start=False) is False


def test_an_arm_is_consumed_not_merely_read():
    """
    One abort protects exactly one turn. Leaving `armed` set would re-arm
    every subsequent turn off a single barge.
    """
    b = RunBarrier()
    b.abort()
    b.begin_turn()
    assert b.armed is False
    b.discards(is_run_start=True)
    b.end_turn()

    b.begin_turn()
    assert b.discards(is_run_start=False) is False


def test_two_aborts_before_a_turn_still_protect_one_turn():
    b = RunBarrier()
    b.abort()
    b.abort()
    b.begin_turn()
    assert b.discards(is_run_start=False) is True
    b.end_turn()
    b.begin_turn()
    assert b.discards(is_run_start=False) is False


# ── The wiring, pinned against the source ────────────────────────────────────


def test_barge_uses_device_wake_admission():
    assert "async def admit_barge" in TURN_ENGINE_SRC
    assert "wake_request" in CONTROLLER_SRC
    assert "turn.cancel" in TURN_ENGINE_SRC
    assert "speaker_flush" in TURN_ENGINE_SRC
