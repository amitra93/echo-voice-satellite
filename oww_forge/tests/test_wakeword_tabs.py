from pathlib import Path


PAGE = Path(__file__).parents[1] / "static" / "index.html"


def test_wakeword_tabs_preserve_the_selected_model_across_status_refreshes():
    source = PAGE.read_text()
    assert "em-forge-active-wakeword" in source
    assert "localStorage.getItem" in source
    assert "data-wake-tab" in source
    assert "activeWakeword" in source


def test_wakeword_tabs_render_only_the_selected_model_panel():
    source = PAGE.read_text()
    assert "state.wakewords.filter(w => w.name === activeWakeword)" in source
    assert 'role="tablist"' in source
    assert 'role="tabpanel"' in source
