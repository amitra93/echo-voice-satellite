"""Shared entity base + dynamic-add helper for coordinator-backed entities."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

DEVICE_MODEL = "Echo Dot Gen 2 (biscuit)"


def device_record(coordinator, device_id: str) -> dict[str, Any]:
    return next(
        (item for item in coordinator.data.get("devices", []) if item.get("device_id") == device_id),
        {},
    )


class EchoCoordinatorEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, device_id: str):
        super().__init__(coordinator)
        self.device_id = device_id
        self._observed = False

    @property
    def record(self) -> dict[str, Any]:
        return device_record(self.coordinator, self.device_id)

    @property
    def available(self) -> bool:
        return bool(
            self.coordinator.control_available
            and self.coordinator.last_update_success
            and self.record.get("connected", False)
        )

    @property
    def device_info(self):
        record = self.record
        return {
            "identifiers": {(DOMAIN, self.device_id)},
            "name": record.get("label") or self.device_id,
            "manufacturer": "EchoMuse",
            "model": DEVICE_MODEL,
            "sw_version": record.get("firmware_ver"),
        }

    def capability_seen(self, capability: str) -> bool:
        if capability in self.record.get("capabilities", []):
            self._observed = True
        return self._observed

    def capability_available(self, capability: str) -> bool:
        return self.capability_seen(capability) and capability in self.record.get("capabilities", [])


def add_dynamic_entities(
    coordinator,
    async_add_entities,
    factory: Callable[[dict[str, Any]], list[Any]],
    capability: str | None = None,
):
    """Add approved devices/capabilities discovered after setup.

    Capability entities remain registered after a later snapshot omits the
    capability (a device offline for a moment, or awaiting a firmware
    reconcile), preserving HA registry identities and user customization —
    see `known_capabilities`.
    """
    seen: set[str] = set()
    known_capabilities = getattr(coordinator, "known_capabilities", {})

    def add_current() -> None:
        additions: list[Any] = []
        for record in coordinator.data.get("devices", []):
            device_id = record.get("device_id")
            if not device_id:
                continue
            key = f"{device_id}:{capability or '*'}"
            known = bool(capability) and capability in known_capabilities.get(device_id, set())
            if key in seen or (
                capability and capability not in record.get("capabilities", []) and not known
            ):
                continue
            seen.add(key)
            additions.extend(factory(record))
        if additions:
            async_add_entities(additions)

    add_current()
    return coordinator.async_add_listener(add_current)
