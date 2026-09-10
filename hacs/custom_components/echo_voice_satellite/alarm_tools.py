"""Pure request and response shaping for EchoMuse alarm LLM tools."""

from __future__ import annotations

from datetime import date as date_type

WEEKDAY_BITS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _time(hour: int, minute: int) -> tuple[int, int]:
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ValueError("hour must be an integer from 0 through 23")
    if not isinstance(minute, int) or not 0 <= minute <= 59:
        raise ValueError("minute must be an integer from 0 through 59")
    return hour, minute


def _label(label: str | None) -> str | None:
    if label is None:
        return None
    if not isinstance(label, str):
        raise ValueError("label must be text")
    return label.strip() or None


def one_off_body(
    *, hour: int, minute: int, date: str | None = None, label: str | None = None, tz: str
) -> dict:
    """Build a controller request for a one-off wall-clock alarm."""
    hour, minute = _time(hour, minute)
    body = {"hour": hour, "minute": minute, "recurrence": "once", "tz": tz}
    if date is not None:
        if not isinstance(date, str):
            raise ValueError("date must be YYYY-MM-DD")
        try:
            # Keep the string form strict so the controller does not receive a
            # date format that is valid Python but ambiguous to the LLM/user.
            if date_type.fromisoformat(date).isoformat() != date:
                raise ValueError
        except ValueError as err:
            raise ValueError("date must be YYYY-MM-DD") from err
        body["date"] = date
    if (clean_label := _label(label)) is not None:
        body["label"] = clean_label
    return body


def periodic_body(
    *,
    hour: int,
    minute: int,
    recurrence: str,
    weekdays: list[str] | None = None,
    label: str | None = None,
    tz: str,
) -> dict:
    """Build a controller request for a daily or weekly alarm."""
    hour, minute = _time(hour, minute)
    if recurrence not in ("daily", "weekly"):
        raise ValueError("recurrence must be daily or weekly")
    body = {"hour": hour, "minute": minute, "recurrence": recurrence, "tz": tz}
    if recurrence == "weekly":
        if not weekdays:
            raise ValueError("weekly alarms require one or more weekdays")
        mask = 0
        for weekday in weekdays:
            if weekday not in WEEKDAY_BITS:
                raise ValueError(f"unknown weekday: {weekday}")
            mask |= 1 << WEEKDAY_BITS[weekday]
        body["weekday_mask"] = mask
    elif weekdays:
        raise ValueError("daily alarms must not specify weekdays")
    if (clean_label := _label(label)) is not None:
        body["label"] = clean_label
    return body


def alarm_result(alarm: dict) -> dict:
    """Allowlist a compact, LLM-useful alarm representation."""
    weekday_mask = int(alarm.get("weekday_mask", 0))
    return {
        "id": alarm["id"],
        "label": alarm.get("label"),
        "hour": alarm["hour"],
        "minute": alarm["minute"],
        "recurrence": alarm["recurrence"],
        "weekdays": [
            weekday for weekday, bit in WEEKDAY_BITS.items() if weekday_mask & (1 << bit)
        ] if alarm["recurrence"] == "weekly" else [],
        "date": alarm.get("date"),
        "timezone": alarm.get("tz"),
        "enabled": bool(alarm.get("enabled", True)),
        "next_fire_utc": alarm.get("next_fire_utc"),
    }
