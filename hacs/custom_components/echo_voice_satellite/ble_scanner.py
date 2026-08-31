"""EchoMuse passive BLE scanner for Home Assistant."""

from __future__ import annotations

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth.models import BluetoothServiceInfoBleak
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BaseHaRemoteScanner

from .ble import decode_raw_advert


class EchoMuseRemoteScanner(BaseHaRemoteScanner):
    """Feed passive EchoMuse advertisements into HA's Bluetooth manager."""

    def __init__(self, hass, source: str):
        super().__init__(source, source, None, False)
        self.hass = hass
        self.source = source
        self._advertisement_callback = None

    async def async_setup(self) -> None:
        await super().async_setup()
        self._advertisement_callback = bluetooth.async_get_advertisement_callback(self.hass)

    def feed(self, advert: dict) -> None:
        if self._advertisement_callback is None:
            return
        parsed = decode_raw_advert(advert)
        device = BLEDevice(parsed["address"], parsed["local_name"], {"source": self.source})
        advertisement = AdvertisementData(
            parsed["local_name"], parsed["manufacturer_data"],
            parsed["service_data"], parsed["service_uuids"], None,
            parsed["rssi"], (),
        )
        info = BluetoothServiceInfoBleak.from_device_and_advertisement_data(
            device, advertisement, self.source, 0.0, False,
        )
        self._advertisement_callback(info)


def register_scanner(hass, source: str):
    """Register one EchoMuse scanner and return HA's unregister callback."""
    scanner = EchoMuseRemoteScanner(hass, source)
    cancel = bluetooth.async_register_scanner(
        hass, scanner, connection_slots=0,
        source_domain="echo_voice_satellite",
    )
    return scanner, cancel
