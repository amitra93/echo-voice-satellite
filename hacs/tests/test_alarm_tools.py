"""Pure tests for the controller payloads returned by the alarm LLM tools."""

from pathlib import Path

import pytest

from custom_components.echo_voice_satellite import alarm_tools as tools


def test_one_off_body_uses_24_hour_time_and_optional_date():
    assert tools.one_off_body(
        hour=7, minute=30, date="2026-10-25", label="gym", tz="America/New_York"
    ) == {
        "hour": 7,
        "minute": 30,
        "recurrence": "once",
        "date": "2026-10-25",
        "label": "gym",
        "tz": "America/New_York",
    }


@pytest.mark.parametrize("date", ["tomorrow", "2026/10/25", "2026-02-30"])
def test_one_off_body_rejects_non_iso_dates(date):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        tools.one_off_body(hour=7, minute=0, date=date, tz="UTC")


@pytest.mark.parametrize("hour,minute", [(-1, 0), (24, 0), (7, -1), (7, 60)])
def test_alarm_bodies_reject_invalid_24_hour_time(hour, minute):
    with pytest.raises(ValueError):
        tools.one_off_body(hour=hour, minute=minute, tz="UTC")


def test_periodic_body_daily_and_weekly():
    assert tools.periodic_body(
        hour=6, minute=0, recurrence="daily", tz="UTC"
    ) == {"hour": 6, "minute": 0, "recurrence": "daily", "tz": "UTC"}
    assert tools.periodic_body(
        hour=7,
        minute=15,
        recurrence="weekly",
        weekdays=["monday", "thursday"],
        label="office",
        tz="UTC",
    ) == {
        "hour": 7,
        "minute": 15,
        "recurrence": "weekly",
        "weekday_mask": 0b0001001,
        "label": "office",
        "tz": "UTC",
    }


def test_periodic_body_requires_weekdays_only_for_weekly():
    with pytest.raises(ValueError, match="require"):
        tools.periodic_body(hour=7, minute=0, recurrence="weekly", tz="UTC")
    with pytest.raises(ValueError, match="must not"):
        tools.periodic_body(
            hour=7, minute=0, recurrence="daily", weekdays=["monday"], tz="UTC"
        )
    with pytest.raises(ValueError, match="unknown weekday"):
        tools.periodic_body(
            hour=7, minute=0, recurrence="weekly", weekdays=["moonday"], tz="UTC"
        )


def test_alarm_result_is_allowlisted_for_the_llm():
    result = tools.alarm_result(
        {
            "id": "a1",
            "device_id": "secret-device-id",
            "label": "Gym",
            "hour": 7,
            "minute": 0,
            "recurrence": "weekly",
            "weekday_mask": 31,
            "date": None,
            "tz": "America/New_York",
            "enabled": 1,
            "next_fire_utc": 123,
            "created_by": "integration",
        }
    )
    assert result == {
        "id": "a1",
        "label": "Gym",
        "hour": 7,
        "minute": 0,
        "recurrence": "weekly",
        "weekdays": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        "date": None,
        "timezone": "America/New_York",
        "enabled": True,
        "next_fire_utc": 123,
    }


def test_alarm_llm_platform_has_only_the_four_named_tools():
    component = Path(__file__).resolve().parents[1] / "custom_components" / "echo_voice_satellite"
    source = (component / "llm.py").read_text()
    assert [
        "EchomuseSetOneOffAlarm",
        "EchomuseSetPeriodicAlarm",
        "EchomuseCancelAlarm",
        "EchomuseListAlarms",
    ] == [
        name for name in (
            "EchomuseSetOneOffAlarm",
            "EchomuseSetPeriodicAlarm",
            "EchomuseCancelAlarm",
            "EchomuseListAlarms",
        ) if f'name = "{name}"' in source
    ]
    for retired in ("alarm_intents.py", "alarm_voice.py", "alarm_sentences.yaml"):
        assert not (component / retired).exists()
