"""EchoMuse Home Assistant integration package.

Sets up the controller client, the device coordinator, the entity platforms,
and — per docs/design/full-duplex-plan.md Phase 2b — one passive BLE remote scanner per
known device, fed from `ble.adverts` events. There is no ESPHome dependency
anywhere in this package: devices are wholly owned by this integration.

Imports of `homeassistant` (directly or via `.coordinator`/`.ble_scanner`)
are deferred into the function bodies below, deliberately: this file runs
whenever ANY submodule of this package is imported (`.client`, `.audio_frame`,
`.tts_stream`, `.ble` — the pure-logic modules with no HA dependency, tested
without a Home Assistant install), so a top-level `homeassistant` import here
would make even those imports fail outside a real HA environment.
"""

from __future__ import annotations

import logging

from .const import CONF_API_KEY, CONF_URL, DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    from .timer_card import async_register_websocket_commands

    try:
        async_register_websocket_commands(hass)
    except ImportError:
        # Keep the pure package setup usable by tooling that imports the
        # integration without Home Assistant's optional websocket module —
        # timer_card.py itself has no top-level homeassistant import, only
        # this call's deferred one.
        pass
    return True


def _remove_matching_entities(hass, entry, predicate) -> None:
    """Shared core for the one-time cleanup migrations below — each one
    exists because HA does not delete a previously-registered entity just
    because the integration stops providing it, so a device set up before
    a platform change keeps a stale, permanently-unavailable registry entry
    forever without an explicit removal.

    Iterates registry.entities directly rather than
    er.async_entries_for_config_entry(): that helper's secondary
    _config_entry_id_index lookup returned nothing for a real entity on
    real hardware whose own config_entry_id field matched — confirmed live,
    2026-08-18, while deploying the first of these migrations. Checking the
    field directly on each entry doesn't trust that index to be complete
    for every entity regardless of how or when it was created.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entity_entry in list(registry.entities.values()):
        if entity_entry.config_entry_id == entry.entry_id and predicate(entity_entry):
            registry.async_remove(entity_entry.entity_id)


def _remove_stale_button_entities(hass, entry) -> None:
    """The Action Button event entity (event.py) was removed from
    PLATFORMS. Matches on domain "event" for this config entry rather than
    unique_id, since that is a complete description of what event.py ever
    created and survives even if the unique_id naming had varied.
    """
    _remove_matching_entities(hass, entry, lambda e: e.domain == "event")


def _remove_stale_volume_number_entities(hass, entry) -> None:
    """The Volume number entity (number.py, a continuous slider) was
    replaced by a 9-level select (select.py, EchoVolumeSelect). The
    slider's send path was verified to have no floor and no snap-back
    (direct calls to the same controller endpoint landed anywhere from 0%
    to 100% and held, 2026-08-19) — the "stuck around 37%" users saw was an
    HA-frontend drag artefact, not something wrong with what the entity
    sent. A discrete select sidesteps that class of bug by sending one full
    request per press rather than a stream of intermediate drag values.
    Matches on domain "number" — nothing else in this package uses it.
    """
    _remove_matching_entities(hass, entry, lambda e: e.domain == "number")


def _remove_stale_mute_entities(hass, entry) -> None:
    """The momentary Privacy mute button (button.py, EchoMuteButton) and
    binary_sensor.py's read-only mute sensor were replaced by one toggle
    entity (switch.py, EchoMuteSwitch) once the wire protocol offered
    mute_toggle — a switch can both report state and drive it, so there is
    no longer a reason for those to be two separate entities.

    "button" is matched by whole domain — nothing else in this package uses
    it, same reasoning as the volume-number cleanup above. binary_sensor
    is matched by unique_id suffix instead, because that domain also has
    EchoOnlineSensor ("_online"), which must NOT be swept up alongside the
    old mute sensor ("_muted").
    """
    _remove_matching_entities(
        hass, entry,
        lambda e: e.domain == "button" or (
            e.domain == "binary_sensor" and (e.unique_id or "").endswith("_muted")
        ),
    )


def _remove_stale_sendspin_music_entities(hass, entry) -> None:
    """Remove the retired controller-provided Sendspin media player."""
    _remove_matching_entities(
        hass, entry,
        lambda e: e.domain == "media_player" and (e.unique_id or "").endswith("_sendspin_music"),
    )


def _remove_stale_diagnostic_sensor_entities(hass, entry) -> None:
    """Firmware version and wake word model (sensor.py's EchoFirmwareSensor
    / EchoWakeModelSensor) were removed — that information already lives on
    the EchoMuse controller's own dashboard, and duplicating it as HA
    entities just added clutter. Matched by unique_id suffix, not whole
    domain: sensor also has EchoAmbientLightSensor ("_ambient_light"),
    which must stay.
    """
    _remove_matching_entities(
        hass, entry,
        lambda e: e.domain == "sensor" and (e.unique_id or "").endswith(("_firmware", "_wake_model")),
    )


def _remove_stale_volume_select_entities(hass, entry) -> None:
    """Remove the retired EchoMuse volume entity. Matched by unique_id suffix.
    """
    _remove_matching_entities(
        hass, entry,
        lambda e: e.domain == "select" and (e.unique_id or "").endswith("_volume_level"),
    )


async def async_setup_entry(hass, entry) -> bool:
    from .ble_scanner import register_scanner
    from .client import ControllerClient
    from .coordinator import EchoVoiceSatelliteCoordinator
    from .timer_card import async_setup_timer_card

    _remove_stale_button_entities(hass, entry)
    _remove_stale_volume_number_entities(hass, entry)
    _remove_stale_volume_select_entities(hass, entry)
    _remove_stale_mute_entities(hass, entry)
    _remove_stale_sendspin_music_entities(hass, entry)
    _remove_stale_diagnostic_sensor_entities(hass, entry)

    client = ControllerClient(entry.data[CONF_URL], entry.data[CONF_API_KEY])
    coordinator = EchoVoiceSatelliteCoordinator(hass, client, entry)
    timer_card_hub = async_setup_timer_card(hass, entry, client)
    ble_scanners: dict[str, tuple] = {}

    def _sync_ble_scanners() -> None:
        for record in coordinator.data.get("devices", []) if coordinator.data else []:
            device_id = record.get("device_id")
            if device_id and device_id not in ble_scanners:
                ble_scanners[device_id] = register_scanner(hass, f"echomuse-{device_id}")

    async def _on_event(event: dict) -> None:
        if event.get("type") != "ble.adverts":
            return
        entry_scanner = ble_scanners.get(event.get("device_id"))
        if entry_scanner is None:
            return
        scanner, _cancel = entry_scanner
        for advert in event.get("adverts", []):
            try:
                scanner.feed(advert)
            except (KeyError, ValueError):
                _LOGGER.debug("Dropped malformed BLE advert from %s", event.get("device_id"))

    async def _on_timer_alarm(event: dict) -> None:
        if event.get("type") == "timer.alarm":
            timer_card_hub.notify_alarm_event(event)

    try:
        await coordinator.async_config_entry_first_refresh()
        await coordinator.async_connect_control()
    except Exception:
        await client.async_close()
        raise

    _sync_ble_scanners()
    coordinator.async_add_listener(_sync_ble_scanners)
    coordinator.async_add_event_listener(_on_event)
    coordinator.async_add_event_listener(_on_timer_alarm)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client, "coordinator": coordinator, "ble_scanners": ble_scanners,
        "timer_card": timer_card_hub,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    data = hass.data[DOMAIN].pop(entry.entry_id)
    for _scanner, cancel in data["ble_scanners"].values():
        cancel()
    await data["coordinator"].async_shutdown()
    await data["client"].async_close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
