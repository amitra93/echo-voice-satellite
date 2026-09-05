"""Backend for the `echo-voice-timers-card` Lovelace card.

Home Assistant's `TimerManager` is authoritative for every active and
paused timer (docs/design/timers-design.md "Data Contract") — this module
is the one deliberate boundary that reaches into its internals for
ID-addressed UI actions, kept in its own module with its own tests per that
same design note.

It layers two things `TimerManager` cannot provide on its own:

  - **Ringing/queued presentation state.** `FINISHED` removes a timer from
    `TimerManager` the instant it fires, before the EchoMuse controller has
    even started the physical alarm — so a card that only reads
    `TimerManager.timers` shows nothing for the entire time a timer is
    actually ringing. The controller pushes `timer.alarm` events describing
    that state (`em_api._post_timer_event` / `em_timers.AlarmSession`);
    `AlarmPresence` holds the latest one per device so the card can still
    show, and dismiss, a timer that finished seconds or minutes ago.
  - **A path back to the EchoMuse device.** Dismissing a ringing alarm has
    to reach the controller, which knows nothing about HA timer IDs and
    addresses devices by its own device id — never the HA device registry
    id `TimerManager` uses. Every `timer.alarm` record carries that EchoMuse
    device id as its top-level `device_id` (see `em_api._post_timer_event`),
    so `AlarmPresence` can map a ringing timer id back to it.

Every hass/registry touch below is behind an injected accessor
(`TimerCardHub`'s constructor arguments), so the orchestration logic is
fully testable without a running Home Assistant — see
`hacs/tests/test_timer_card.py`. `async_setup_timer_card()` is the one
place that wires the real accessors; `async_register_websocket_commands()`
is the thin, deferred-import glue that exposes it over the WebSocket API.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

# Card-visible timer states. TimerManager itself only ever reports "active"
# or "paused" (TimerInfo.is_active); "ringing" and "queued" are synthesized
# entirely from the controller's timer.alarm presentation state below —
# TimerManager has no concept of either.
ACTIVE = "active"
PAUSED = "paused"
RINGING = "ringing"
QUEUED = "queued"

DeviceNameResolver = Callable[[str | None], "str | None"]


class AlarmPresence:
    """Per-device store of the latest `timer.alarm` snapshot.

    One event replaces the previous snapshot wholesale (the controller
    always sends the full current/queue state, never a delta), so this is
    just a dict keyed by the EchoMuse device id — no lifecycle logic lives
    here, that is `em_timers.AlarmSession`'s job on the controller side.
    """

    def __init__(self) -> None:
        self._by_device: dict[str, dict[str, Any]] = {}

    def update(self, event: dict[str, Any]) -> None:
        device_id = event.get("device_id")
        if not device_id:
            return
        self._by_device[device_id] = {
            "current": event.get("current"),
            "queue": event.get("queue") or [],
        }

    def rows(self, known_timer_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Ringing/queued rows not already present in `TimerManager`.

        `known_timer_ids` is `manager.timers.keys()`: a timer TimerManager
        still has active or paused is never duplicated here. In practice a
        timer this store remembers as ringing has always already left
        TimerManager (FINISHED pops it before the handler that feeds this
        store ever runs), so the filter mostly guards against a stale
        presence entry surviving past a controller restart that reset the
        alarm queue without telling HACS.
        """
        known = set(known_timer_ids)
        rows: list[dict[str, Any]] = []
        for record in self._by_device.values():
            current = record.get("current")
            if current and current.get("timer_id") not in known:
                rows.append(_alarm_row(current, RINGING))
            for queued in record.get("queue") or []:
                if queued.get("timer_id") not in known:
                    rows.append(_alarm_row(queued, QUEUED))
        return rows

    def echomuse_device_for_timer(self, timer_id: str) -> str | None:
        """The EchoMuse device id owning a ringing/queued timer.

        Used only by `dismiss`, which addresses the controller by its own
        device id and has no other way to learn it from a bare HA timer id.
        """
        for echomuse_device_id, record in self._by_device.items():
            current = record.get("current")
            if current and current.get("timer_id") == timer_id:
                return echomuse_device_id
            if any(item.get("timer_id") == timer_id for item in record.get("queue") or []):
                return echomuse_device_id
        return None


def _alarm_row(record: dict[str, Any], state: str) -> dict[str, Any]:
    return {
        "id": record.get("timer_id"),
        "device_id": record.get("ha_device_id"),
        "device_name": None,  # filled in by build_snapshot, which has the resolver
        "name": record.get("name"),
        "state": state,
        "duration_seconds": record.get("total_seconds", 0),
        # A ringing or queued timer is, by definition, already at zero —
        # it finished in Home Assistant. The card shows it as "ringing" /
        # "queued" text rather than a countdown.
        "remaining_seconds": 0,
        "finishes_at": None,
    }


def manager_rows(
    manager, device_name: DeviceNameResolver, *, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Active/paused rows straight from `TimerManager`, in the card's data
    contract shape. Delayed-command timers (`timer.conversation_command`)
    are never included — see the same decision documented in
    `assist_satellite.py::_timer_event`."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for timer in manager.timers.values():
        if getattr(timer, "conversation_command", None):
            continue
        state = ACTIVE if timer.is_active else PAUSED
        seconds_left = timer.seconds_left
        rows.append({
            "id": timer.id,
            "device_id": timer.device_id,
            "device_name": device_name(timer.device_id),
            "name": timer.name,
            "state": state,
            "duration_seconds": timer.created_seconds,
            "remaining_seconds": seconds_left,
            # Absent while paused — a paused timer has no deadline to count
            # down to until it is resumed. Computed fresh from the
            # monotonic-clock-derived seconds_left on every snapshot, never
            # stored, so a DST transition between two snapshots cannot skew
            # it: only the wall-clock instant the card reads is exposed,
            # never a wall-clock value carried across time.
            "finishes_at": (
                (now + timedelta(seconds=seconds_left)).isoformat()
                if state == ACTIVE else None
            ),
            "area_name": getattr(timer, "area_name", None),
        })
    return rows


def build_snapshot(
    manager,
    presence: AlarmPresence,
    device_name: DeviceNameResolver,
    known_devices: Callable[[], Iterable[dict[str, str]]],
) -> dict[str, Any]:
    """The full `list`/`subscribe` payload: every timer row plus the known
    Echo devices, so the card's empty-state creation form has somewhere to
    target even when no timer currently exists to source a device from."""
    known_ids = set(manager.timers.keys()) if manager is not None else set()
    rows = manager_rows(manager, device_name) if manager is not None else []
    for row in presence.rows(known_ids):
        row["device_name"] = device_name(row["device_id"])
        rows.append(row)
    return {"timers": rows, "devices": list(known_devices())}


def apply_timer_action(
    manager, action: str, timer_id: str, *, seconds: int | None = None,
) -> bool:
    """Apply one TimerManager-backed card action. Returns whether it was
    both recognised and actually applied — an unknown timer id or an
    unknown action both come back False, deliberately indistinguishable to
    the caller: either way there is nothing further this function can do
    about it, and the WS handler reports a single "not found" error for
    both rather than a security-relevant distinction that doesn't exist."""
    try:
        if action == "pause":
            manager.pause_timer(timer_id)
        elif action == "resume":
            manager.unpause_timer(timer_id)
        elif action == "cancel":
            manager.cancel_timer(timer_id)
        elif action == "change":
            if seconds is None:
                return False
            manager.add_time(timer_id, seconds)
        else:
            return False
    except Exception:  # noqa: BLE001 — TimerManager's not-found error isn't
        # part of its public export surface (homeassistant.components.intent
        # re-exports the manager and the handler type, not its exceptions),
        # so this is deliberately broad rather than importing a private
        # exception path that could move under us.
        return False
    return True


def start_timer(
    manager,
    language: str,
    *,
    device_id: str,
    hours: int | None = None,
    minutes: int | None = None,
    seconds: int | None = None,
    name: str | None = None,
) -> str | None:
    """Start a timer for one EchoMuse device via `TimerManager.start_timer`.
    Returns the new HA timer id, or None if TimerManager refused it (no
    device, or a device that never registered a timer handler)."""
    try:
        return manager.start_timer(
            device_id, hours, minutes, seconds, language, name=name,
        )
    except Exception:  # noqa: BLE001 — same reasoning as apply_timer_action
        return None


class TimerCardHub:
    """Per-config-entry orchestrator behind the card's WebSocket commands.

    Every hass-touching lookup is an injected callable so this class can be
    constructed and exercised with plain fakes; `async_setup_timer_card`
    is the only place real Home Assistant accessors are wired in.
    """

    def __init__(
        self,
        client,
        *,
        manager_getter: Callable[[], Any | None],
        device_name_resolver: DeviceNameResolver,
        known_devices_getter: Callable[[], Iterable[dict[str, str]]],
        language: str = "en",
    ) -> None:
        self._client = client
        self._manager_getter = manager_getter
        self._device_name = device_name_resolver
        self._known_devices = known_devices_getter
        self._language = language
        self.presence = AlarmPresence()
        self._subscribers: dict[object, Callable[[dict[str, Any]], None]] = {}

    def snapshot(self) -> dict[str, Any]:
        return build_snapshot(
            self._manager_getter(), self.presence, self._device_name, self._known_devices,
        )

    def notify_manager_change(self) -> None:
        """Called whenever TimerManager's own state changed — a lifecycle
        event forwarded through the per-Echo timer handler, or a card
        action that mutated it directly."""
        self._push()

    def notify_alarm_event(self, event: dict[str, Any]) -> None:
        """Called on every `timer.alarm` event from the controller."""
        self.presence.update(event)
        self._push()

    def _push(self) -> None:
        # No subscribers is the common case between card opens, and
        # snapshot() reaches into TimerManager and the device registry —
        # skip that work entirely rather than computing a snapshot no one
        # reads. It also means a lifecycle event arriving before either is
        # ready (early in Home Assistant startup) is harmless as long as
        # nothing has subscribed yet.
        if not self._subscribers:
            return
        snapshot = self.snapshot()
        for push in list(self._subscribers.values()):
            push(snapshot)

    def subscribe(self, push: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """Register a push callback; it fires once immediately with the
        current snapshot (the `list`+deltas contract in one call), then
        again on every future change until the returned unsubscribe runs."""
        key = object()
        self._subscribers[key] = push
        push(self.snapshot())

        def unsubscribe() -> None:
            self._subscribers.pop(key, None)

        return unsubscribe

    def action(self, action: str, timer_id: str, *, seconds: int | None = None) -> bool:
        manager = self._manager_getter()
        if manager is None:
            return False
        applied = apply_timer_action(manager, action, timer_id, seconds=seconds)
        if applied:
            self.notify_manager_change()
        return applied

    def start(
        self,
        *,
        device_id: str,
        hours: int | None = None,
        minutes: int | None = None,
        seconds: int | None = None,
        name: str | None = None,
    ) -> str | None:
        manager = self._manager_getter()
        if manager is None:
            return None
        timer_id = start_timer(
            manager, self._language,
            device_id=device_id, hours=hours, minutes=minutes, seconds=seconds, name=name,
        )
        if timer_id is not None:
            self.notify_manager_change()
        return timer_id

    async def dismiss(self, timer_id: str) -> bool:
        device_id = self.presence.echomuse_device_for_timer(timer_id)
        if device_id is None:
            return False
        reply = await self._client.async_dismiss_timer_alarm(device_id)
        dismissed = bool(reply.get("dismissed"))
        if dismissed:
            # The controller has already pushed its own timer.alarm event
            # for this (notify_alarm_event runs it through the normal
            # coordinator listener), but that event's delivery is not
            # ordered against this call's return — push here too so a
            # card that dismissed its own alarm never waits on a second,
            # independently-timed event to see the result.
            self.notify_manager_change()
        return dismissed


def async_setup_timer_card(hass, entry, client) -> TimerCardHub:
    """Build the real-HA-backed hub for one config entry.

    The `homeassistant.components.intent` / `homeassistant.helpers.device_registry`
    imports are deferred into each accessor's own body, not done once here
    at construction time — same reasoning as every other deferred import in
    this package (see the package's `__init__.py` docstring): building the
    hub must not itself require those modules to be importable, only
    actually taking a snapshot does.
    """

    def manager_getter():
        from homeassistant.components.intent import TIMER_DATA
        return hass.data.get(TIMER_DATA)

    def device_name(device_id: str | None) -> str | None:
        if not device_id:
            return None
        from homeassistant.helpers import device_registry as dr
        registry_entry = dr.async_get(hass).async_get(device_id)
        if registry_entry is None:
            return None
        return registry_entry.name_by_user or registry_entry.name

    def known_devices() -> list[dict[str, str]]:
        from homeassistant.helpers import device_registry as dr
        registry = dr.async_get(hass)
        return [
            {"device_id": device.id, "device_name": device.name_by_user or device.name}
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        ]

    return TimerCardHub(
        client,
        manager_getter=manager_getter,
        device_name_resolver=device_name,
        known_devices_getter=known_devices,
        language=getattr(getattr(hass, "config", None), "language", None) or "en",
    )


def _hub_for(hass) -> TimerCardHub | None:
    from .const import DOMAIN

    for entry_data in hass.data.get(DOMAIN, {}).values():
        if isinstance(entry_data, dict) and "timer_card" in entry_data:
            return entry_data["timer_card"]
    return None


def async_register_websocket_commands(hass) -> None:
    """Register the card's WebSocket command set. Deferred import, same
    reasoning as every other `homeassistant`-touching import in this
    package: it must stay importable without Home Assistant installed."""
    from homeassistant.components import websocket_api
    import voluptuous as vol

    # Every key the card ever sends must be declared: websocket_command
    # builds a strict voluptuous schema from this dict, and an undeclared
    # key is rejected with "extra keys not allowed" before the handler
    # runs. Shipping a handler that reads msg.get("device_id") under a
    # schema that forbids "device_id" made every card action (start,
    # pause, resume, change, cancel, dismiss) fail while list/subscribe
    # — which send nothing extra — kept working. The payload shapes are
    # pinned by hacs/tests/test_timer_card_commands.py against the card's
    # actual _call() sites.
    @websocket_api.websocket_command({"type": "echo_voice_satellite/timers/list"})
    @websocket_api.async_response
    async def _list(hass, connection, msg):
        hub = _hub_for(hass)
        connection.send_result(
            msg["id"], hub.snapshot() if hub else {"timers": [], "devices": []},
        )

    @websocket_api.websocket_command({"type": "echo_voice_satellite/timers/subscribe"})
    @websocket_api.callback
    def _subscribe(hass, connection, msg):
        hub = _hub_for(hass)
        connection.send_result(msg["id"])
        if hub is None:
            connection.send_event(msg["id"], {"timers": [], "devices": []})
            return

        def push(snapshot: dict) -> None:
            connection.send_event(msg["id"], snapshot)

        connection.subscriptions[msg["id"]] = hub.subscribe(push)

    @websocket_api.websocket_command({
        "type": "echo_voice_satellite/timers/start",
        vol.Optional("device_id"): str,
        vol.Optional("hours"): vol.Coerce(int),
        vol.Optional("minutes"): vol.Coerce(int),
        vol.Optional("seconds"): vol.Coerce(int),
        vol.Optional("name"): vol.Any(str, None),
    })
    @websocket_api.async_response
    async def _start(hass, connection, msg):
        hub = _hub_for(hass)
        device_id = msg.get("device_id")
        if hub is None or not device_id:
            connection.send_error(msg["id"], "invalid_request", "Missing device_id")
            return
        timer_id = hub.start(
            device_id=device_id,
            hours=msg.get("hours"), minutes=msg.get("minutes"), seconds=msg.get("seconds"),
            name=msg.get("name"),
        )
        if timer_id is None:
            connection.send_error(msg["id"], "start_failed", "Could not start the timer")
            return
        connection.send_result(msg["id"], {"id": timer_id})

    def _register_action(name: str, action: str) -> None:
        @websocket_api.websocket_command({
            "type": f"echo_voice_satellite/timers/{name}",
            vol.Optional("timer_id"): str,
            vol.Optional("seconds"): vol.Coerce(int),
        })
        @websocket_api.async_response
        async def _handler(hass, connection, msg):
            hub = _hub_for(hass)
            timer_id = msg.get("timer_id")
            if hub is None or not timer_id:
                connection.send_error(msg["id"], "invalid_request", "Missing timer_id")
                return
            applied = hub.action(action, timer_id, seconds=msg.get("seconds"))
            if not applied:
                connection.send_error(msg["id"], "timer_not_found", "Unknown timer")
                return
            connection.send_result(msg["id"])

        websocket_api.async_register_command(hass, _handler)

    for name in ("pause", "resume", "cancel", "change"):
        _register_action(name, name)

    @websocket_api.websocket_command({
        "type": "echo_voice_satellite/timers/dismiss",
        vol.Optional("timer_id"): str,
    })
    @websocket_api.async_response
    async def _dismiss(hass, connection, msg):
        hub = _hub_for(hass)
        timer_id = msg.get("timer_id")
        if hub is None or not timer_id:
            connection.send_error(msg["id"], "invalid_request", "Missing timer_id")
            return
        dismissed = await hub.dismiss(timer_id)
        connection.send_result(msg["id"], {"dismissed": dismissed})

    websocket_api.async_register_command(hass, _list)
    websocket_api.async_register_command(hass, _subscribe)
    websocket_api.async_register_command(hass, _start)
    websocket_api.async_register_command(hass, _dismiss)
