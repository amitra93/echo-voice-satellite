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
discriminator) were pinning source that no longer exists. Only
`test_barge_serialises_in_both_phases` survives from that section: it checks
`em_controller.py`'s `_barge_watcher`, which is unchanged and still calls
`turn_engine.abort_ha_run`/`cancel_voice_turn(abort_ha=True)` on a barge —
those calls are today a documented no-op on the HA side (same gap), but the
controller's own intent to serialise is still real and still worth pinning.
"""

from pathlib import Path

from em_runbarrier import RunBarrier

CONTROLLER_SRC = (Path(__file__).resolve().parents[1] / "em_controller.py").read_text()


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


def test_barge_serialises_in_both_phases():
    """
    Thinking starts another turn on this connection, so HA's run must be
    aborted first. Playback must serialise too: RUN_END follows TTS_END, so a
    barge in the first milliseconds of audio can beat it.
    """
    watcher = CONTROLLER_SRC[CONTROLLER_SRC.index("async def _barge_watcher"):]
    watcher = watcher[: watcher.index("\nasync def ", 10)]
    assert "abort_ha=True" in watcher, (
        "barge during thinking must abort HA's pipeline before the "
        "interrupting turn starts"
    )
    assert "abort_ha_run" in watcher, (
        "barge during playback must serialise against the interrupting turn"
    )
