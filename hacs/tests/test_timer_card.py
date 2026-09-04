"""echo-voice-timers-card.js — source-shape assertions.

The card has no build step and this repo has no JS test runner, so —
matching the established pattern for this asset — these tests read the
shipped source rather than executing it. `node --check` (run in CI
alongside these) already proves it parses; these pin the behavioural
properties docs/design/timers-design.md's Data Contract and Frontend card
test sections require.
"""

from pathlib import Path


CARD = Path(__file__).parents[1] / "www" / "echo-voice-timers-card.js"


def _source() -> str:
    return CARD.read_text()


def test_timer_card_is_registered():
    assert 'customElements.define("echo-voice-timers-card"' in _source()


def test_timer_card_uses_every_documented_websocket_command():
    source = _source()
    assert '"echo_voice_satellite/timers/subscribe"' in source
    # _call() builds the command type from a template literal
    # (`echo_voice_satellite/timers/${type}`), not a literal string per
    # action — assert every documented action is actually invoked through it.
    assert "`echo_voice_satellite/timers/${type}`" in source
    for action in ("start", "change", "cancel", "dismiss"):
        assert f'this._call("{action}"' in source
    # pause/resume share one call site keyed off state, not two literals.
    assert 'this._call(timer.state === "active" ? "pause" : "resume"' in source


def test_timer_card_subscribes_instead_of_polling():
    source = _source()
    assert "subscribeMessage" in source
    assert "setInterval(() => this._loadTimers()" not in source
    # The countdown redraw interval must never itself fetch — only
    # subscribeMessage's push callback may assign _timersData/_devices.
    assert "setInterval(() => this._render()" in source


def test_timer_card_has_no_second_persistence_store_and_reads_no_timer_entities():
    source = _source()
    assert "localStorage" not in source
    assert "fetch(" not in source
    assert "this._hass.states" not in source


def test_timer_card_distinguishes_all_four_presentation_states():
    source = _source()
    for state in ("ringing", "queued", "active", "paused"):
        assert f'"{state}"' in source


def test_dismiss_control_is_only_offered_for_a_ringing_timer():
    source = _source()
    action_fn = source[source.index("_actionsFor(timer)"):source.index("_renderCreationForm")]
    assert '"dismiss"' in action_fn
    assert action_fn.index('state === "ringing"') < action_fn.index('"dismiss"')


def test_queued_timer_offers_no_controls():
    source = _source()
    action_fn = source[source.index("_actionsFor(timer)"):source.index("_renderCreationForm")]
    assert 'state === "queued"' in action_fn


def test_active_and_paused_offer_pause_resume_change_and_cancel():
    source = _source()
    action_fn = source[source.index("_actionsFor(timer)"):source.index("_renderCreationForm")]
    for action in ("pause", "resume", "change", "cancel"):
        assert f'"{action}"' in action_fn


def test_empty_state_offers_a_duration_and_name_creation_form():
    source = _source()
    assert "_renderCreationForm" in source
    assert 'type = "number"' in source
    assert 'placeholder = "Name' in source
    assert '"start"' in source


def test_card_config_device_id_filters_the_visible_timer_list():
    source = _source()
    assert "_visibleTimers" in source
    fn = source[source.index("_visibleTimers()"):source.index("_remainingText")]
    assert "this._config.device_id" in fn
    assert "timer.device_id === filterDeviceId" in fn


def test_countdown_is_computed_from_finishes_at_not_a_backend_tick():
    source = _source()
    fn = source[source.index("_remainingText(timer)"):source.index("_durationText(totalSeconds)")]
    assert "timer.finishes_at" in fn
    assert "Date.now()" in fn
