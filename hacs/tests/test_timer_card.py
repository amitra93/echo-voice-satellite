from pathlib import Path


CARD = Path(__file__).parents[1] / "www" / "echo-voice-timers-card.js"


def test_timer_card_is_registered_and_uses_native_timer_services():
    source = CARD.read_text()
    assert 'customElements.define("echo-voice-timers-card"' in source
    assert 'type: "echo_voice_satellite/timers"' in source
    for service in ("pause", "resume", "cancel", "finish"):
        assert f'"{service}"' in source


def test_timer_card_refreshes_from_the_hacs_timer_transport_not_timer_entities():
    source = CARD.read_text()
    assert "callWS" in source
    assert "setInterval(() => this._loadTimers(), 1000)" in source
    assert "this._hass.states" not in source


def test_timer_card_has_no_second_persistence_store_or_controller_transport():
    source = CARD.read_text()
    assert "localStorage" not in source
    assert "fetch(" not in source
    assert "timer_id" in source
