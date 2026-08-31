from pathlib import Path


DESIGN = Path(__file__).parents[2] / "docs" / "design" / "timers-design.md"


def test_timer_name_instruction_is_documented_for_the_selected_llm_agent():
    text = DESIGN.read_text()
    assert "When calling HassStartTimer, always provide a concise, non-empty `name`." in text
    assert '"1 minute timer"' in text
    assert 'Do not use a generic name such as "Timer".' in text
