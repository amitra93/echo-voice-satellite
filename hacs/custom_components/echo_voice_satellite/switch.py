"""Privacy mute as a single toggle entity.

Replaces the earlier button.py (EchoMuteButton, a momentary press with no
state of its own) plus binary_sensor.py's read-only EchoMutedSensor — two
entities for one concept. A switch is both: `is_on` reads the device's live
mute state (updated promptly — see em_ha_sidechannels.mute_state, added
alongside this) and turning it on/off drives the device.

Mute is device-sovereign (CLAUDE.md "Volume / mute persistence") and the
only primitive the wire protocol offers is `mute_toggle` — there is no
`mute_set`/`unmute` message, because the device routes it straight into the
same MuteToggle() the hardware button calls, which only knows how to flip.
So `async_turn_on`/`async_turn_off` must check current state first and only
send the toggle when it would actually change something: firing mute_toggle
unconditionally on turn_on() would UNMUTE an already-muted device half the
time — the opposite of what was asked, and with no error to notice it by.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .client import ControllerError
from .const import DOMAIN
from .entities import EchoCoordinatorEntity, add_dynamic_entities


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    entry.async_on_unload(add_dynamic_entities(
        data["coordinator"], async_add_entities,
        lambda record: [EchoMuteSwitch(data["coordinator"], data["client"], record["device_id"])],
    ))


class EchoMuteSwitch(EchoCoordinatorEntity, SwitchEntity):
    _attr_name = "Privacy mute"

    def __init__(self, coordinator, client, device_id):
        super().__init__(coordinator, device_id)
        self.client = client
        self._attr_unique_id = f"{device_id}_privacy_mute"

    @property
    def is_on(self):
        return bool(self.record.get("muted"))

    async def async_turn_on(self, **kwargs) -> None:
        if not self.is_on:
            await self._toggle()

    async def async_turn_off(self, **kwargs) -> None:
        if self.is_on:
            await self._toggle()

    async def _toggle(self) -> None:
        try:
            await self.client.async_media_command(self.device_id, {"command": "mute_toggle"})
        except ControllerError as exc:
            raise RuntimeError(str(exc)) from exc
