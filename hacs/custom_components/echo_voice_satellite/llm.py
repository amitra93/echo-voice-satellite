"""Assist LLM tools for controller-owned EchoMuse alarms.

Home Assistant natively exposes its timer intents to the Assist LLM API, but
it has no alarm concept. These tools intentionally use that same API rather
than sentence matching or custom IntentHandlers. They are available only for
turns originating from a registered EchoMuse satellite, so a generic chat has
no authority to choose a device for the caller.
"""

from __future__ import annotations

from typing import override

import voluptuous as vol

from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.llm import LLM_API_ASSIST, LLMContext, Tool, ToolInput
from homeassistant.util.json import JsonObjectType

from .alarm_tools import WEEKDAY_BITS, alarm_result, one_off_body, periodic_body
from .client import ControllerError
from .const import DOMAIN


def _resolve_echomuse_device(hass: HomeAssistant, ha_device_id: str | None):
    """Return (controller client, controller device id, timezone) for an Echo."""
    if not ha_device_id:
        return None
    device = dr.async_get(hass).async_get(ha_device_id)
    if device is None:
        return None
    echomuse_id = next(
        (identifier[1] for identifier in device.identifiers if identifier[0] == DOMAIN),
        None,
    )
    if echomuse_id is None:
        return None
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict):
            continue
        coordinator = entry_data.get("coordinator")
        client = entry_data.get("client")
        devices = (coordinator.data or {}).get("devices", []) if coordinator else []
        if client and any(row.get("device_id") == echomuse_id for row in devices):
            return client, echomuse_id, hass.config.time_zone
    return None


class _AlarmTool(Tool):
    """Shared live device resolution and controller-error shape."""

    async def _device(self, hass: HomeAssistant, llm_context: LLMContext):
        device = _resolve_echomuse_device(hass, llm_context.device_id)
        if device is None:
            return None, {
                "success": False,
                "error": "Alarm tools are available only from an EchoMuse satellite.",
            }
        return device, None


class EchomuseSetOneOffAlarm(_AlarmTool):
    name = "EchomuseSetOneOffAlarm"
    description = (
        "Set one non-repeating alarm on the calling EchoMuse satellite. "
        "Use 24-hour local time. Omit date to set the next occurrence of that time."
    )
    parameters = vol.Schema(
        {
            vol.Required("hour", description="24-hour local hour, 0 through 23"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=23)
            ),
            vol.Required("minute", description="Minute, 0 through 59"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=59)
            ),
            vol.Optional("date", description="Optional local date in YYYY-MM-DD format"): str,
            vol.Optional("label", description="Optional short alarm label"): str,
        }
    )

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> JsonObjectType:
        data = self.parameters(tool_input.tool_args)
        device, error = await self._device(hass, llm_context)
        if error:
            return error
        client, device_id, tz = device
        try:
            body = one_off_body(tz=tz, **data)
            response = await client.async_create_alarm(device_id, body)
        except (ControllerError, ValueError) as err:
            return {"success": False, "error": str(err)}
        return {"success": True, "result": alarm_result(response["alarm"])}


class EchomuseSetPeriodicAlarm(_AlarmTool):
    name = "EchomuseSetPeriodicAlarm"
    description = (
        "Set a daily or weekly recurring alarm on the calling EchoMuse satellite. "
        "Use weekdays only with weekly recurrence, as lowercase full weekday names."
    )
    parameters = vol.Schema(
        {
            vol.Required("hour", description="24-hour local hour, 0 through 23"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=23)
            ),
            vol.Required("minute", description="Minute, 0 through 59"): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=59)
            ),
            vol.Required("recurrence", description="daily or weekly"): vol.In(("daily", "weekly")),
            vol.Optional(
                "weekdays",
                description="Required for weekly recurrence; lowercase weekday names",
            ): [vol.In(tuple(WEEKDAY_BITS))],
            vol.Optional("label", description="Optional short alarm label"): str,
        }
    )

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> JsonObjectType:
        data = self.parameters(tool_input.tool_args)
        device, error = await self._device(hass, llm_context)
        if error:
            return error
        client, device_id, tz = device
        try:
            body = periodic_body(tz=tz, **data)
            response = await client.async_create_alarm(device_id, body)
        except (ControllerError, ValueError) as err:
            return {"success": False, "error": str(err)}
        return {"success": True, "result": alarm_result(response["alarm"])}


class EchomuseCancelAlarm(_AlarmTool):
    name = "EchomuseCancelAlarm"
    description = (
        "Cancel exactly one alarm on the calling EchoMuse satellite. "
        "Call EchomuseListAlarms first and pass its exact alarm id."
    )
    parameters = vol.Schema(
        {vol.Required("alarm_id", description="Exact id returned by EchomuseListAlarms"): str}
    )

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> JsonObjectType:
        data = self.parameters(tool_input.tool_args)
        device, error = await self._device(hass, llm_context)
        if error:
            return error
        client, device_id, _tz = device
        try:
            response = await client.async_delete_alarm(device_id, data["alarm_id"])
        except ControllerError as err:
            return {"success": False, "error": str(err)}
        return {"success": True, "result": {"id": response["id"], "cancelled": True}}


class EchomuseListAlarms(_AlarmTool):
    name = "EchomuseListAlarms"
    description = "List every alarm on the calling EchoMuse satellite."
    parameters = vol.Schema({})

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> JsonObjectType:
        self.parameters(tool_input.tool_args)
        device, error = await self._device(hass, llm_context)
        if error:
            return error
        client, device_id, _tz = device
        try:
            alarms = await client.async_list_alarms(device_id)
        except ControllerError as err:
            return {"success": False, "error": str(err)}
        return {"success": True, "result": {"alarms": [alarm_result(alarm) for alarm in alarms]}}


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    """Expose alarms only to a tool-capable Assist turn from an EchoMuse device."""
    if api_id != LLM_API_ASSIST:
        return None
    if _resolve_echomuse_device(hass, llm_context.device_id) is None:
        return None
    return LLMTools(
        tools=[
            EchomuseSetOneOffAlarm(),
            EchomuseSetPeriodicAlarm(),
            EchomuseCancelAlarm(),
            EchomuseListAlarms(),
        ]
    )
