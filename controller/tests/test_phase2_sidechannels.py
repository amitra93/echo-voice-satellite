import asyncio
import sys
import types
from pathlib import Path

import em_ha_sidechannels as sidechannels


def test_sidechannel_event_helpers_publish_expected_payloads(monkeypatch):
    async def run():
        events = []

        async def push_event(event):
            events.append(event)

        monkeypatch.setitem(sys.modules, "em_api", types.SimpleNamespace(_push_event=push_event))

        sidechannels.button_event("d", "single", 0)
        sidechannels.ambient_light("d", 42)
        sidechannels.volume("d", 0.75)
        sidechannels.mute_state("d", True)
        sidechannels.capabilities("d", ["mic", "speaker"])
        sidechannels.wake_model("d", "hey_jarvis_v0.1")
        sidechannels.ble_adverts("d", [{"addr": "aa:bb:cc:dd:ee:ff", "rssi": -50, "data": ""}])
        await asyncio.sleep(0)

        assert events == [
            {"type": "button.event", "device_id": "d", "gesture": "single", "held_ms": 0},
            {"type": "ambient_light", "device_id": "d", "lux": 42},
            {"type": "volume_state", "device_id": "d", "volume": 0.75},
            {"type": "mute_state", "device_id": "d", "muted": True},
            {"type": "capabilities", "device_id": "d", "capabilities": ["mic", "speaker"]},
            {"type": "wake_model", "device_id": "d", "model_id": "hey_jarvis_v0.1"},
            {"type": "ble.adverts", "device_id": "d", "adverts": [{"addr": "aa:bb:cc:dd:ee:ff", "rssi": -50, "data": ""}]},
        ]

    asyncio.run(run())


def test_media_state_callback_publishes_reported_state(monkeypatch):
    async def run():
        events = []

        async def push_event(event):
            events.append(event)

        monkeypatch.setitem(sys.modules, "em_api", types.SimpleNamespace(_push_event=push_event))
        monkeypatch.setattr(sidechannels.em_player, "reported_state", lambda device_id: "playing")
        sidechannels.init(lambda device_id: types.SimpleNamespace(volume=0.5, muted=True))

        await sidechannels.media_state("d")
        assert events == [{
            "type": "media_state",
            "device_id": "d",
            "state": "playing",
            "volume": 0.5,
            "muted": True,
        }]

    asyncio.run(run())


def test_merge_device_exposes_capabilities_and_ambient_light_for_hacs():
    """
    The HACS integration's entity gating (button/ambient-light) reads the
    raw capability list, not the dashboard's per-feature booleans — see
    hacs/custom_components/echo_voice_satellite/entities.py's
    capability_available(). Both fields must exist on /api/devices and the
    /api/events snapshot, which share this function.
    """
    api = (Path(__file__).resolve().parent.parent / "em_api.py").read_text()
    assert '"capabilities":' in api, \
        "/api/devices must expose the raw capability list for HACS entity gating"
    assert '"ambient_light_lux"' in api, \
        "/api/devices must expose the last known ambient light reading"


def test_controller_pushes_a_dedicated_mute_state_sidechannel_event():
    """
    em_controller.py's `mute_state` handler already pushes a dashboard-facing
    `device_update` event ({"state": {"muted": ...}}) — general-purpose,
    nested, and not something coordinator.py's flat _STATE_EVENT_FIELDS
    mapping reads. Without a dedicated ha_sidechannels.mute_state() call
    alongside it, HACS's mute switch (or the binary_sensor it replaced)
    only ever picks up a mute toggle — hardware button or HACS's own
    mute_toggle button — on the 60s REST poll, reading as stuck for up to
    a minute after every press.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()

    handler = src[src.index('elif msg_type == "mute_state":'):]
    handler = handler[:handler.index('elif msg_type == "volume_state":')]

    assert "ha_sidechannels.mute_state(device_id, device.muted)" in handler, (
        "the mute_state handler must push a dedicated ha_sidechannels event "
        "so HACS's mute entity updates promptly, not just on REST poll"
    )


def test_controller_announces_device_connect_to_ha_event_clients():
    """
    The disconnect path pushes `device_disconnected`
    (api.notify_device_disconnected); the connect path must push its mirror
    so the HACS coordinator learns a device is back live rather than only on
    a REST poll. Without it, a controller restart left HACS entities stuck
    unavailable for hours (2026-08-19): the /api/events WS reconnected in
    seconds but the device reconnected a few seconds later, so the
    reconnect-moment snapshot showed connected=False and nothing pushed the
    correction.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()

    # The connect handler runs from the "Device connected" log through the
    # ack send; the announce must sit in that window.
    start = src.index('f"[control] Device connected: {device_id}')
    window = src[start:src.index('"type": "ack", "device_id": device_id')]
    assert "await _push_device_state(device)" in window, (
        "the device connect handler must push _push_device_state so HA-facing "
        "clients (HACS) get a live connected=True signal on (re)connect"
    )


def test_push_device_state_announces_connected_true_in_its_payload():
    """The connect announcement must clear HACS entity availability."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "em_controller.py").read_text()
    start = src.index("async def _push_device_state(device: Device)")
    body = src[start:src.index("\n\n", start)]

    assert '"type":      "device_update"' in body
    assert '"state": {' in body
    assert '"connected": True' in body
