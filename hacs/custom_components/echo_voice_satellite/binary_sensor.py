"""Connectivity.

Privacy mute used to live here as a read-only EchoMutedSensor, back when
the wire protocol had no way to command a mute remotely — see switch.py's
EchoMuteSwitch, which replaced it (and the momentary button.py button)
once the mute_toggle control message existed.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .const import DOMAIN
from .entities import EchoCoordinatorEntity, add_dynamic_entities


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entry.async_on_unload(add_dynamic_entities(
        coordinator, async_add_entities,
        lambda record: [EchoOnlineSensor(coordinator, record["device_id"])],
    ))


class EchoOnlineSensor(EchoCoordinatorEntity, BinarySensorEntity):
    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, device_id):
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_online"

    @property
    def is_on(self):
        return bool(self.record.get("connected"))
