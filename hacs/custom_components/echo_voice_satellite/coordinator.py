"""Device inventory + live event fan-out for the Echo Voice Satellite entities.

Simpler than a versioned-gateway coordinator would need to be, because there
is exactly one source of truth on this LAN (the controller) and no revision
negotiation across a fleet of clients: `/api/devices` (REST) and the
`/api/events` snapshot return the identical `_merge_device` shape, so both
paths just replace the device list. Live deltas (button presses, volume,
ambient light, wake-word changes, BLE adverts, turn lifecycle) arrive as
individual events and are merged in place.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import timedelta
from typing import Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import ControllerClient, ControllerError

_LOGGER = logging.getLogger(__name__)

# Event types that update a device record's live fields in place: (the
# key the event payload carries the value under, the record field to write
# it to). Anything not listed here still reaches entity listeners via
# _emit_event (turn lifecycle, button gestures, BLE adverts) — it just
# doesn't rewrite persistent device state.
_STATE_EVENT_FIELDS: dict[str, tuple[str, str]] = {
    "ambient_light": ("lux", "ambient_light_lux"),
    "volume_state": ("volume", "volume"),
    "capabilities": ("capabilities", "capabilities"),
    "wake_model": ("model_id", "wake_model_id"),
    "mute_state": ("muted", "muted"),
}


class EchoVoiceSatelliteCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass, client: ControllerClient, entry=None):
        self.client = client
        self.entry = entry
        stored_capabilities = entry.data.get("known_capabilities", {}) if entry else {}
        if not isinstance(stored_capabilities, dict):
            stored_capabilities = {}
        self.known_capabilities: dict[str, set[str]] = {
            device_id: set(capabilities)
            for device_id, capabilities in stored_capabilities.items()
            if isinstance(device_id, str) and isinstance(capabilities, list)
        }
        self.control_available = False
        self._reconnect_task: asyncio.Task | None = None
        self._event_listeners: list = []
        super().__init__(
            hass, logger=_LOGGER, name="echo_voice_satellite",
            update_interval=timedelta(seconds=60),
        )
        self.client.set_event_handler(self._async_event)
        self.client.set_disconnect_handler(self._async_control_disconnected)

    # ── Live events ─────────────────────────────────────────────────────────

    async def _async_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "snapshot":
            devices = event.get("devices", [])
            self._remember_capabilities(devices)
            self.async_set_updated_data({"devices": devices})
            return

        device_id = event.get("device_id")
        etype = event.get("type", "")

        # device_update carries a partial `state` dict (connected, muted,
        # speaking, listening, thinking — and sometimes stats/wifi). Merging
        # it into the record is the ONLY live signal the coordinator gets that
        # a device has (re)connected: the controller pushes it on device
        # connect (em_controller `_push_device_state`), and between snapshots
        # and REST polls nothing else updates record["connected"]. Without
        # this merge a controller restart left every entity's cached record
        # stuck at connected=False — captured in the reconnect-moment snapshot,
        # before the device itself reconnected a few seconds later — so the
        # entities sat unavailable until a REST poll eventually ran (up to
        # hours, since push events keep deferring that poll). device_disconnected
        # is its mirror (api.notify_device_disconnected), flipping connected off.
        if device_id and self.data and etype in ("device_update", "device_disconnected"):
            updated = copy.deepcopy(self.data)
            for item in updated.get("devices", []):
                if item.get("device_id") != device_id:
                    continue
                if etype == "device_disconnected":
                    item["connected"] = False
                else:
                    state = event.get("state")
                    if isinstance(state, dict):
                        item.update(state)
            self._remember_capabilities(updated.get("devices", []))
            self.async_set_updated_data(updated)
            await self._emit_event(event)
            return

        mapping = _STATE_EVENT_FIELDS.get(etype)
        if device_id and mapping and self.data:
            payload_key, record_field = mapping
            updated = copy.deepcopy(self.data)
            for item in updated.get("devices", []):
                if item.get("device_id") == device_id:
                    item[record_field] = event.get(payload_key)
            self._remember_capabilities(updated.get("devices", []))
            self.async_set_updated_data(updated)

        await self._emit_event(event)

    def async_add_event_listener(self, callback):
        self._event_listeners.append(callback)

        def remove() -> None:
            if callback in self._event_listeners:
                self._event_listeners.remove(callback)

        return remove

    async def _emit_event(self, event: dict[str, Any]) -> None:
        for callback in tuple(self._event_listeners):
            await callback(event)

    async def _async_control_disconnected(self) -> None:
        self.control_available = False
        self.async_update_listeners()
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(
                self._async_reconnect(), name="echo-voice-satellite-reconnect"
            )

    async def _async_reconnect(self) -> None:
        attempt = 0
        while not self.control_available:
            await asyncio.sleep(self.client.reconnect_delay(attempt))
            attempt += 1
            try:
                await self.client.async_close()
                await self.async_connect_control()
            except Exception:  # noqa: BLE001 — keep retrying regardless of cause
                continue

    # ── REST fallback poll ─────────────────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            devices = await self.client.async_get_devices()
        except (OSError, ControllerError) as exc:
            raise UpdateFailed(str(exc)) from exc
        self._remember_capabilities(devices)
        return self._merge_rest_devices(devices)

    def _remember_capabilities(self, devices: list[dict[str, Any]]) -> None:
        changed = False
        for record in devices:
            device_id = record.get("device_id")
            capabilities = record.get("capabilities", [])
            if not device_id or not isinstance(capabilities, list):
                continue
            known = self.known_capabilities.setdefault(device_id, set())
            before = len(known)
            known.update(value for value in capabilities if isinstance(value, str))
            changed |= len(known) != before
        if changed and self.entry is not None:
            serialized = {
                device_id: sorted(values) for device_id, values in self.known_capabilities.items()
            }
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, "known_capabilities": serialized}
            )

    def _merge_rest_devices(self, devices: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply a REST poll without rolling back a newer live event."""
        if self.data is None:
            return {"devices": devices}
        merged = copy.deepcopy(self.data)
        current_by_id = {item.get("device_id"): item for item in merged.get("devices", [])}
        for incoming in devices:
            device_id = incoming.get("device_id")
            if not device_id:
                continue
            current = current_by_id.get(device_id)
            if current is None:
                merged.setdefault("devices", []).append(copy.deepcopy(incoming))
            else:
                current.update(copy.deepcopy(incoming))
        merged["devices"] = [
            item for item in merged.get("devices", [])
            if item.get("device_id") in {d.get("device_id") for d in devices}
        ]
        return merged

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def async_connect_control(self) -> None:
        try:
            await self.client.async_connect()
        except ControllerError as exc:
            raise UpdateFailed(str(exc)) from exc
        try:
            await asyncio.wait_for(self.client.snapshot_ready.wait(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise UpdateFailed("controller did not send an /api/events snapshot") from exc
        snapshot = self.client.snapshot or {"devices": await self.client.async_get_devices()}
        # control_available MUST be set before async_set_updated_data, not
        # after. async_set_updated_data synchronously notifies every entity to
        # recompute EchoCoordinatorEntity.available, which reads
        # control_available — so setting it afterwards makes that notification
        # evaluate availability with the OLD (False) value, writing every
        # entity "unavailable", and then nothing re-notifies. On a fresh
        # setup/reload the entities are added after this whole method
        # completes, so they read True at add time and it looks fine; only a
        # live RECONNECT (this method called again while entities already
        # exist) exposed the ordering. That left every entity stuck
        # unavailable for 5.5 hours after a controller restart — the WS had
        # reconnected and voice turns worked (those ride _emit_event, not
        # async_set_updated_data), but no mapped state event happened to fire
        # to re-run the notification, so nothing ever corrected the stale
        # unavailable (observed 2026-08-19, fixed by a manual reload).
        self.control_available = True
        self.async_set_updated_data({"devices": snapshot.get("devices", [])})

    async def async_shutdown(self) -> None:
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            await asyncio.gather(self._reconnect_task, return_exceptions=True)
            self._reconnect_task = None
