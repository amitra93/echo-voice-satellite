import base64

import pytest

from custom_components.echo_voice_satellite.ble import decode_raw_advert, parse_advertisement


def ad(ad_type, value):
    body = bytes([ad_type]) + value
    return bytes([len(body)]) + body


def test_parse_common_ble_ad_structures():
    raw = b"".join([
        ad(0x09, b"Beacon"),
        ad(0x03, b"\x0d\x18"),
        ad(0x16, b"\xaa\xfehello"),
        ad(0xff, b"\x4c\x00payload"),
    ])
    parsed = parse_advertisement(raw)
    assert parsed["local_name"] == "Beacon"
    assert parsed["service_uuids"] == ["180d"]
    assert parsed["service_data"]["feaa"] == b"hello"
    assert parsed["manufacturer_data"][0x004c] == b"payload"


def test_decode_raw_advert_normalizes_controller_shape():
    raw = ad(0x09, b"Beacon")
    result = decode_raw_advert({
        "addr": "aa:bb:cc:dd:ee:ff",
        "addrType": 1,
        "rssi": -62,
        "data": base64.b64encode(raw).decode(),
    })
    assert result["address"] == "AA:BB:CC:DD:EE:FF"
    assert result["rssi"] == -62
    assert result["connectable"] is False
    assert result["raw"] == raw


def test_decode_rejects_truncated_or_invalid_advert():
    with pytest.raises(ValueError):
        parse_advertisement(b"\x05\x09abc")
    with pytest.raises(ValueError):
        decode_raw_advert({
            "addr": "not-an-address", "rssi": -1, "data": "!!!",
        })
