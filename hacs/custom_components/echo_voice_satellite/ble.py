"""Pure conversion of EchoMuse raw BLE advertisements to HA fields."""

from __future__ import annotations

import base64


def _uuid16(value: bytes) -> str:
    return f"{int.from_bytes(value, 'little'):04x}"


def _uuid128(value: bytes) -> str:
    hex_value = value[::-1].hex()
    return f"{hex_value[:8]}-{hex_value[8:12]}-{hex_value[12:16]}-{hex_value[16:20]}-{hex_value[20:]}"


def parse_advertisement(raw: bytes) -> dict:
    """Parse Bluetooth LE AD structures into HA AdvertisementData fields."""
    local_name = None
    manufacturer_data: dict[int, bytes] = {}
    service_data: dict[str, bytes] = {}
    service_uuids: list[str] = []
    offset = 0
    while offset < len(raw):
        length = raw[offset]
        offset += 1
        if length == 0:
            break
        end = offset + length
        if end > len(raw):
            raise ValueError("truncated BLE AD structure")
        ad_type = raw[offset]
        value = raw[offset + 1:end]
        offset = end
        if ad_type in (0x08, 0x09):
            local_name = value.decode("utf-8", errors="replace")
        elif ad_type in (0x02, 0x03):
            for index in range(0, len(value) - 1, 2):
                service_uuids.append(_uuid16(value[index:index + 2]))
        elif ad_type in (0x06, 0x07):
            for index in range(0, len(value) - 15, 16):
                service_uuids.append(_uuid128(value[index:index + 16]))
        elif ad_type == 0x16 and len(value) >= 2:
            service_data[_uuid16(value[:2])] = value[2:]
        elif ad_type == 0x20 and len(value) >= 2:
            service_data[f"{int.from_bytes(value[:2], 'little'):08x}"] = value[2:]
        elif ad_type == 0x21 and len(value) >= 16:
            service_data[_uuid128(value[:16])] = value[16:]
        elif ad_type == 0xFF and len(value) >= 2:
            company = int.from_bytes(value[:2], "little")
            manufacturer_data[company] = value[2:]
    return {
        "local_name": local_name,
        "manufacturer_data": manufacturer_data,
        "service_data": service_data,
        "service_uuids": service_uuids,
    }


def decode_raw_advert(advert: dict) -> dict:
    """Validate and normalize the controller's JSON advert shape."""
    address = str(advert["addr"]).upper()
    if len(address.split(":")) != 6:
        raise ValueError("invalid BLE address")
    data = base64.b64decode(advert.get("data") or "", validate=True)
    parsed = parse_advertisement(data)
    parsed.update({
        "address": address,
        "rssi": int(advert["rssi"]),
        "connectable": False,
        "address_type": int(advert.get("addrType", 0)),
        "raw": data,
    })
    return parsed
