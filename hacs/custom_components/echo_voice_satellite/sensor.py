"""Sensors: ambient light.

Firmware version and wake word model used to live here as diagnostic-
category sensors (EchoFirmwareSensor / EchoWakeModelSensor) — removed, since
that information is already on the EchoMuse controller's own dashboard and
duplicating it as HA entities just adds clutter nobody acts on from here.
See __init__.py's _remove_stale_diagnostic_sensor_entities for the registry
cleanup on already-provisioned devices.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import LIGHT_LUX

from .const import CAP_AMBIENT_LIGHT, DOMAIN
from .entities import EchoCoordinatorEntity, add_dynamic_entities


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entry.async_on_unload(add_dynamic_entities(
        coordinator, async_add_entities,
        lambda record: [EchoAmbientLightSensor(coordinator, record["device_id"])],
        capability=CAP_AMBIENT_LIGHT,
    ))


class EchoAmbientLightSensor(EchoCoordinatorEntity, SensorEntity):
    _attr_name = "Ambient light"
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device_id):
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_ambient_light"

    @property
    def available(self):
        return super().available and self.capability_available(CAP_AMBIENT_LIGHT)

    @property
    def native_value(self):
        return self.record.get("ambient_light_lux")
