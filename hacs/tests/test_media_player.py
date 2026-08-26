import importlib

import pytest


module = importlib.import_module("custom_components.echo_voice_satellite.media_player")


class _Coordinator:
    control_available = True
    last_update_success = True

    def __init__(self, record):
        self.data = {"devices": [record]}


class _Client:
    def __init__(self):
        self.calls = []

    async def async_media_command(self, device_id, body):
        self.calls.append((device_id, body))


def _make(record=None):
    client = _Client()
    entity = object.__new__(module.EchoSendspinMediaPlayer)
    entity.coordinator = _Coordinator(record or {"device_id": "A"})
    entity.device_id = "A"
    entity.client = client
    entity._observed = False
    return entity, client


def test_state_and_metadata_are_read_from_live_sendspin_state():
    entity, _ = _make({
        "device_id": "A",
        "volume": 79 / 127.0,  # Level 1 on device
        "muted": False,
        "sendspin_state": {
            "state": "playing",
            "title": "Song", "artist": "Artist",
        },
    })
    assert entity.state.value == "playing"
    assert entity.volume_level == 0.01
    assert entity.is_volume_muted is False
    assert entity.media_title == "Song"
    assert entity.media_artist == "Artist"


def test_volume_level_reports_zero_when_muted():
    entity, _ = _make({
        "device_id": "A",
        "volume": 127 / 127.0,
        "muted": True,
    })
    assert entity.volume_level == 0.0
    assert entity.is_volume_muted is True


def test_media_player_falls_back_for_legacy_and_invalid_state():
    entity, _ = _make({"device_id": "A", "media_state": "paused"})
    assert entity.state.value == "paused"
    entity, _ = _make({"device_id": "A", "sendspin_state": {"state": "bogus"}})
    assert entity.state.value == "idle"


def test_volume_mapping_handles_missing_zero_and_fallback_metadata():
    entity, _ = _make({"device_id": "A", "sendspin_title": "Fallback", "sendspin_artist": "Band"})
    assert entity.volume_level is None
    assert entity.media_title == "Fallback"
    assert entity.media_artist == "Band"
    entity, _ = _make({"device_id": "A", "volume": 0, "muted": False})
    assert entity.volume_level == 0.0


def test_media_commands_convert_controller_errors_to_runtime_errors():
    entity, client = _make()

    async def fail(device_id, body):
        raise module.ControllerError("offline")

    client.async_media_command = fail
    import asyncio
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(entity.async_media_pause())
    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(entity.async_play_media("music", "url"))


def test_volume_and_mute_noop_paths_do_not_send_commands():
    entity, client = _make({"device_id": "A", "muted": True})
    import asyncio
    asyncio.run(entity.async_set_volume_level(0.0))
    asyncio.run(entity.async_mute_volume(True))
    assert client.calls == []


@pytest.mark.asyncio
async def test_volume_level_maps_1_to_9_across_slider():
    entity, _ = _make({"device_id": "A", "volume": 127 / 127.0, "muted": False})  # Level 9
    assert entity.volume_level == 1.0

    entity, _ = _make({"device_id": "A", "volume": 103 / 127.0, "muted": False})  # Level 5 / midpoint
    assert 0.49 <= entity.volume_level <= 0.51


@pytest.mark.asyncio
async def test_setting_volume_to_zero_toggles_privacy_mute():
    entity, client = _make({"device_id": "A", "muted": False})
    await entity.async_set_volume_level(0.0)
    assert client.calls == [("A", {"command": "mute_toggle"})]


@pytest.mark.asyncio
async def test_setting_volume_above_zero_unmutes_and_sets_volume():
    entity, client = _make({"device_id": "A", "muted": True})
    await entity.async_set_volume_level(0.50)
    assert client.calls == [
        ("A", {"command": "mute_toggle"}),
        ("A", {"volume": 103 / 127.0}),
    ]


@pytest.mark.asyncio
async def test_commands_use_sendspin_media_endpoint():
    entity, client = _make({"device_id": "A", "muted": False})
    await entity.async_media_play()
    await entity.async_media_next_track()
    await entity.async_media_previous_track()
    await entity.async_media_seek(12.5)
    await entity.async_set_volume_level(0.01)  # maps to raw 79 / level 1
    await entity.async_set_volume_level(1.00)  # maps to raw 127 / level 9
    await entity.async_mute_volume(True)
    assert client.calls == [
        ("A", {"sendspin": True, "command": "play"}),
        ("A", {"sendspin": True, "command": "next"}),
        ("A", {"sendspin": True, "command": "previous"}),
        ("A", {"sendspin": True, "command": "seek", "position_ms": 12500}),
        ("A", {"volume": 79 / 127.0}),
        ("A", {"volume": 127 / 127.0}),
        ("A", {"command": "mute_toggle"}),
    ]
