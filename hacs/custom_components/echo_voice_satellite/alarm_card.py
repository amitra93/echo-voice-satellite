"""Backend for the `echo-voice-alarms-card` Lovelace card.

Unlike the timer card (which reaches into HA's TimerManager because HA owns
timers), alarms are controller-owned, so this is a thin proxy: the WebSocket
commands call the controller through ControllerClient and return what it says.
There is no second store and no TimerManager equivalent here.

Home Assistant imports are deferred (same discipline as the rest of the
package). The client-resolution logic is a pure function so it is unit-tested
without Home Assistant; the WebSocket glue is HA-runtime, validated on a
running Home Assistant (docs/alarm-validation.md).
"""

from __future__ import annotations

import logging
from typing import Any

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_WS_REGISTERED = f"{DOMAIN}_alarm_card_ws_registered"


def resolve_client(entries: list[dict[str, Any]], device_id: str):
    """The ControllerClient owning `device_id`, or None.

    `entries` is a list of hass.data[DOMAIN] entry dicts (each with a
    "client" and a "coordinator"). Kept pure so multi-controller routing is
    testable with plain fakes.
    """
    for entry_data in entries:
        if not isinstance(entry_data, dict):
            continue
        coordinator = entry_data.get("coordinator")
        client = entry_data.get("client")
        if client is None or coordinator is None:
            continue
        devices = (getattr(coordinator, "data", None) or {}).get("devices", [])
        if any(d.get("device_id") == device_id for d in devices):
            return client
    return None


def device_options(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The {device_id, device_name} list the card's create form offers."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry_data in entries:
        if not isinstance(entry_data, dict):
            continue
        coordinator = entry_data.get("coordinator")
        devices = (getattr(coordinator, "data", None) or {}).get("devices", [])
        for record in devices:
            device_id = record.get("device_id")
            if not device_id or device_id in seen:
                continue
            seen.add(device_id)
            out.append({
                "device_id": device_id,
                "device_name": record.get("label") or record.get("name") or device_id,
            })
    return out


def _entries(hass) -> list[dict[str, Any]]:
    return [d for d in hass.data.get(DOMAIN, {}).values() if isinstance(d, dict)]


def async_register_alarm_ws_commands(hass) -> None:
    """Register the alarm card's WebSocket command set (deferred HA import)."""
    if hass.data.get(_WS_REGISTERED):
        return
    from homeassistant.components import websocket_api
    import voluptuous as vol

    @websocket_api.websocket_command({"type": "echo_voice_satellite/alarms/list"})
    @websocket_api.async_response
    async def _list(hass, connection, msg):
        alarms: list[dict] = []
        for entry_data in _entries(hass):
            client = entry_data.get("client")
            if client is None:
                continue
            try:
                alarms.extend(await client.async_list_all_alarms())
            except Exception:  # noqa: BLE001 — one unreachable controller must not blank the card
                _LOGGER.debug("alarms/list: a controller was unreachable", exc_info=True)
        connection.send_result(
            msg["id"], {"alarms": alarms, "devices": device_options(_entries(hass))}
        )

    @websocket_api.websocket_command({
        "type": "echo_voice_satellite/alarms/create",
        vol.Required("device_id"): str,
        vol.Required("hour"): vol.Coerce(int),
        vol.Required("minute"): vol.Coerce(int),
        vol.Optional("recurrence"): str,
        vol.Optional("weekday_mask"): vol.Coerce(int),
        vol.Optional("date"): vol.Any(str, None),
        vol.Optional("label"): vol.Any(str, None),
        vol.Optional("tz"): vol.Any(str, None),
    })
    @websocket_api.async_response
    async def _create(hass, connection, msg):
        client = resolve_client(_entries(hass), msg["device_id"])
        if client is None:
            connection.send_error(msg["id"], "unknown_device", "No such EchoMuse device")
            return
        body = {k: msg[k] for k in (
            "hour", "minute", "recurrence", "weekday_mask", "date", "label", "tz")
            if k in msg and msg[k] is not None}
        try:
            reply = await client.async_create_alarm(msg["device_id"], body)
        except Exception as err:  # noqa: BLE001
            connection.send_error(msg["id"], "create_failed", str(err))
            return
        connection.send_result(msg["id"], reply.get("alarm", reply))

    @websocket_api.websocket_command({
        "type": "echo_voice_satellite/alarms/cancel",
        vol.Required("device_id"): str,
        vol.Required("alarm_id"): str,
    })
    @websocket_api.async_response
    async def _cancel(hass, connection, msg):
        client = resolve_client(_entries(hass), msg["device_id"])
        if client is None:
            connection.send_error(msg["id"], "unknown_device", "No such EchoMuse device")
            return
        try:
            reply = await client.async_delete_alarm(msg["device_id"], msg["alarm_id"])
        except Exception as err:  # noqa: BLE001
            connection.send_error(msg["id"], "cancel_failed", str(err))
            return
        connection.send_result(msg["id"], reply)

    websocket_api.async_register_command(hass, _list)
    websocket_api.async_register_command(hass, _create)
    websocket_api.async_register_command(hass, _cancel)
    hass.data[_WS_REGISTERED] = True
    _LOGGER.debug("Registered EchoMuse alarm card WebSocket commands")
