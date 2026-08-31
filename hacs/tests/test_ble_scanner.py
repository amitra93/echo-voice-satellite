"""EchoMuseRemoteScanner against the real bleak/habluetooth model classes.

EchoMuseRemoteScanner is object.__new__'d rather than constructed normally:
BaseHaScanner.__init__ (Cython, habluetooth) requires a live HA Bluetooth
manager singleton (`get_manager()`), which only exists inside a fully running
Home Assistant with the bluetooth integration set up — well past what a
focused unit test should stand up. feed()'s own logic (the only method this
package overrides) only touches self.source / self._advertisement_callback,
both set directly here, so the real method body still runs against real
bleak/habluetooth model construction (BLEDevice, AdvertisementData,
BluetoothServiceInfoBleak.from_device_and_advertisement_data) — only the
scanner's own base-class registration machinery is bypassed.
"""

import base64
import importlib

import pytest


module = importlib.import_module("custom_components.echo_voice_satellite.ble_scanner")


def _advert(addr="AA:BB:CC:DD:EE:FF", rssi=-50, name="Beacon"):
    # A single Complete Local Name (0x09) AD structure — matches ble.py's
    # own test fixture shape.
    body = bytes([0x09]) + name.encode()
    raw = bytes([len(body)]) + body
    return {"addr": addr, "addrType": 0, "rssi": rssi, "data": base64.b64encode(raw).decode()}


def _make_scanner(source="test-source"):
    # object.__new__ is refused here: BaseHaScanner is a Cython extension
    # type with its own tp_new, which requires going through the type's own
    # __new__ rather than the plain object one.
    scanner = module.EchoMuseRemoteScanner.__new__(module.EchoMuseRemoteScanner)
    scanner.hass = object()
    scanner.source = source
    scanner._advertisement_callback = None
    return scanner


def test_feed_does_nothing_before_async_setup_has_run():
    scanner = _make_scanner()
    # No callback installed yet — must not raise, must not call anything.
    scanner.feed(_advert())


def test_feed_forwards_a_parsed_service_info_to_the_callback():
    scanner = _make_scanner()
    received = []
    scanner._advertisement_callback = lambda info: received.append(info)

    scanner.feed(_advert(addr="aa:bb:cc:dd:ee:ff", rssi=-42, name="Kitchen Sensor"))

    assert len(received) == 1
    info = received[0]
    assert info.address == "AA:BB:CC:DD:EE:FF"
    assert info.rssi == -42
    assert info.name == "Kitchen Sensor"
    assert info.source == "test-source"
    assert info.connectable is False


def test_feed_propagates_a_malformed_advert_as_a_value_error():
    scanner = _make_scanner()
    scanner._advertisement_callback = lambda info: None
    with pytest.raises(ValueError):
        scanner.feed({"addr": "not-an-address", "rssi": -1, "data": ""})


def test_register_scanner_wires_connection_slots_zero_and_source_domain(monkeypatch):
    created = []
    cancels = []

    class _FakeScanner:
        def __init__(self, hass, source):
            created.append((hass, source))
            self.source = source

    monkeypatch.setattr(module, "EchoMuseRemoteScanner", _FakeScanner)

    calls = []

    def fake_register(hass, scanner, *, connection_slots, source_domain):
        calls.append((hass, scanner, connection_slots, source_domain))
        return lambda: cancels.append(True)

    monkeypatch.setattr(module.bluetooth, "async_register_scanner", fake_register)

    hass = object()
    scanner, cancel = module.register_scanner(hass, "echomuse-ABC123")

    assert created == [(hass, "echomuse-ABC123")]
    assert calls[0][2] == 0  # connection_slots — passive proxy, no GATT
    assert calls[0][3] == "echo_voice_satellite"
    assert scanner.source == "echomuse-ABC123"

    cancel()
    assert cancels == [True]
