"""HA-facing state and entity events for the custom integration."""

from __future__ import annotations

import asyncio

import em_player

_get_device = None


def init(get_device) -> None:
    global _get_device
    _get_device = get_device


def _emit(event: dict) -> None:
    async def send() -> None:
        import em_api
        await em_api._push_event(event)
    asyncio.create_task(send())


async def media_state(device_id: str) -> None:
    device = _get_device(device_id) if _get_device else None
    import em_api
    await em_api._push_event({
        "type": "media_state",
        "device_id": device_id,
        "state": em_player.reported_state(device_id),
        "volume": getattr(device, "volume", None),
        "muted": getattr(device, "muted", False),
    })


def button_event(device_id: str, gesture: str, held_ms: int = 0) -> None:
    _emit({
        "type": "button.event", "device_id": device_id,
        "gesture": gesture, "held_ms": int(held_ms or 0),
    })


def ambient_light(device_id: str, lux) -> None:
    _emit({"type": "ambient_light", "device_id": device_id, "lux": lux})


def volume(device_id: str, value: float) -> None:
    _emit({"type": "volume_state", "device_id": device_id, "volume": value})


def mute_state(device_id: str, muted: bool) -> None:
    """Pushed alongside (not instead of) the dashboard-facing device_update
    event em_controller.py already sends on every mute_state message — that
    one is dashboard.jsx's own live-state channel, general-purpose and
    nested under "state", not something HACS's coordinator reads. Without
    a dedicated flat event here the HACS mute entity only ever picked up a
    mute toggled at the device — whether by the hardware button or by
    HACS's own mute_toggle button — on the coordinator's 60s REST poll,
    which reads as a stuck switch for up to a minute after every press.
    """
    _emit({"type": "mute_state", "device_id": device_id, "muted": bool(muted)})


def capabilities(device_id: str, values: list[str]) -> None:
    _emit({"type": "capabilities", "device_id": device_id, "capabilities": list(values or [])})


def wake_model(device_id: str, model_id: str) -> None:
    _emit({"type": "wake_model", "device_id": device_id, "model_id": model_id})


def ble_adverts(device_id: str, adverts: list[dict]) -> None:
    """Forward raw passive BLE advertisements to the HACS integration."""
    _emit({"type": "ble.adverts", "device_id": device_id, "adverts": list(adverts or [])})
