from __future__ import annotations

import asyncio
import types

import numpy as np

import em_sendspin


class FakeDevice:
    def __init__(self):
        self.frames = []
        self.clock_sync = FakeClock()
        self.volume = 0.5
        self.muted = False

    async def send_data(self, frame):
        self.frames.append(frame)


class FakeClock:
    synchronized = True

    def controller_to_device(self, value):
        return value + 100


class FakeSDK:
    connected = True
    server_info = None

    def add_audio_chunk_listener(self, callback):
        self.audio = callback
        return lambda: None

    def add_stream_start_listener(self, callback):
        self.start = callback
        return lambda: None

    def add_stream_clear_listener(self, callback):
        self.clear = callback
        return lambda: None

    def add_stream_end_listener(self, callback):
        self.end = callback
        return lambda: None

    def add_disconnect_listener(self, callback):
        self.disconnect = callback
        return lambda: None

    def is_time_synchronized(self):
        return True

    def compute_play_time(self, timestamp):
        return timestamp


class RuntimeSDK(FakeSDK):
    def __init__(self):
        self.connected = False
        self.server_info = None
        self.connect_calls = []
        self.disconnect_callback = None

    def add_disconnect_listener(self, callback):
        self.disconnect_callback = callback
        return lambda: None

    async def connect(self, url, *, expected_server_id=None):
        self.connect_calls.append(url)
        self.connected = True
        self.server_info = types.SimpleNamespace(server_id="ma-server")

    async def disconnect(self):
        was_connected = self.connected
        self.connected = False
        if was_connected and self.disconnect_callback:
            self.disconnect_callback()

def test_device_session_serializes_start_pcm_and_end():
    async def run():
        device = FakeDevice()
        session = em_sendspin.SendspinDeviceSession("study", "Study", device, FakeClock())
        sdk = FakeSDK()
        session.attach(sdk)
        await session.start()
        sdk.start(None)
        sdk.audio(1_000, b"\x01\x00", "pcm")
        sdk.end(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await session.close()
        assert [frame[0] for frame in device.frames] == [0x06, 0x07, 0x09]
        assert session.rejected_frames == 0

    asyncio.run(run())


def test_audio_is_rejected_until_clock_is_synchronized():
    async def run():
        device = FakeDevice()
        clock = FakeClock()
        clock.synchronized = False
        session = em_sendspin.SendspinDeviceSession("study", "Study", device, clock)
        sdk = FakeSDK()
        session.attach(sdk)
        sdk.start(None)
        sdk.audio(1, b"\x00\x00", "pcm")
        assert session.rejected_frames == 1
        assert device.frames == []

    asyncio.run(run())


def test_clear_discards_queued_audio_and_resets_sequence():
    async def run():
        device = FakeDevice()
        session = em_sendspin.SendspinDeviceSession("study", "Study", device, FakeClock())
        sdk = FakeSDK()
        session.attach(sdk)
        sdk.start(None)
        sdk.audio(100, b"\x00\x00", "pcm")
        sdk.clear(["player"])
        sdk.audio(200, b"\x01\x00", "pcm")
        await session.start()
        await asyncio.sleep(0)
        await session.close()
        assert [frame[0] for frame in device.frames] == [0x08, 0x07]
        assert int.from_bytes(device.frames[-1][5:9], "big") == 0

    asyncio.run(run())


def test_runtime_connects_with_stable_device_identity(monkeypatch, tmp_path):
    import em_ha_sidechannels

    sdk = RuntimeSDK()
    client_args = []

    async def client(*args, **_kwargs):
        client_args.append(args)
        return sdk

    monkeypatch.setattr(em_sendspin, "create_sdk_client", client)
    monkeypatch.setattr(em_ha_sidechannels, "sendspin_state", lambda *_args, **_kwargs: None)

    async def run():
        runtime = em_sendspin.SendspinRuntime(None, str(tmp_path))
        await runtime.configure("ma.local:8927")
        session = await runtime.register_device("study", "Study", FakeDevice())
        await asyncio.sleep(0)
        assert sdk.connect_calls == ["ws://ma.local:8927/sendspin"]
        assert client_args[0][:2] == ("echomuse-study", "Study")
        assert runtime.status()["devices"][0]["client_id"] == "echomuse-study"
        await runtime.close()

    asyncio.run(run())


def test_buffer_capacity_is_derived_from_device_depth():
    # 5.0s of 48kHz mono S16 = 480000 bytes — well under the device's ~5.46s
    # (audioChanDepth) buffer and far below the old hardcoded 2_000_000.
    assert em_sendspin.BUFFER_CAPACITY_BYTES == 480_000
    assert em_sendspin.BUFFER_CAPACITY_BYTES < 2_000_000
    assert em_sendspin.PCM_BYTES_PER_SECOND == 96_000
    assert em_sendspin.REQUIRED_LEAD_MS == 4_000
    assert em_sendspin.MIN_BUFFER_MS == 1_000
    assert em_sendspin.MIN_INITIAL_LEAD_MS == 1_000
    assert em_sendspin.MAX_INITIAL_LEAD_MS == 10_000


def test_register_device_advertises_derived_buffer_capacity(monkeypatch, tmp_path):
    import em_ha_sidechannels

    captured = {}
    sdk = RuntimeSDK()

    async def client(*_args, **kwargs):
        captured.update(kwargs)
        return sdk

    monkeypatch.setattr(em_sendspin, "create_sdk_client", client)
    monkeypatch.setattr(em_ha_sidechannels, "sendspin_state", lambda *_a, **_k: None)

    async def run():
        runtime = em_sendspin.SendspinRuntime(None, str(tmp_path))
        await runtime.configure("ma.local:8927")
        await runtime.register_device("study", "Study", FakeDevice())
        await asyncio.sleep(0)
        assert captured["buffer_capacity"] == em_sendspin.BUFFER_CAPACITY_BYTES
        await runtime.close()

    asyncio.run(run())


def test_audio_callback_enqueues_raw_pcm_not_encoded_or_eq_processed():
    # The SDK audio callback must not run EQ/encoding — those move to the writer
    # so the callback returns promptly. Peek the queue before the writer runs.
    async def run():
        device = FakeDevice()
        device.eq_bands = [6.0, 0, 0, 0, 0, 0, 0, 0]
        device.eq_loudness = True
        session = em_sendspin.SendspinDeviceSession("study", "Study", device, FakeClock())
        sdk = FakeSDK()
        session.attach(sdk)
        sdk.start(None)                 # enqueues ("start", 1) — no writer running
        sdk.audio(1_000, b"\x11\x22", "pcm")

        first = session._queue.get_nowait()
        second = session._queue.get_nowait()
        assert first == ("start", 1)
        assert second[0] == "pcm"
        assert second[4] == b"\x11\x22"  # raw, unencoded, un-EQ'd

    asyncio.run(run())


def test_legacy_play_makes_sendspin_yield_then_release_rearms(monkeypatch, tmp_path):
    import em_ha_sidechannels

    sdk = RuntimeSDK()

    async def client(*_args, **_kwargs):
        return sdk

    monkeypatch.setattr(em_sendspin, "create_sdk_client", client)
    monkeypatch.setattr(em_ha_sidechannels, "sendspin_state", lambda *_a, **_k: None)

    async def run():
        device = FakeDevice()
        runtime = em_sendspin.SendspinRuntime(None, str(tmp_path))
        await runtime.configure("ma.local:8927")
        session = await runtime.register_device("study", "Study", device)
        for _ in range(3):
            await asyncio.sleep(0)
        assert sdk.connect_calls == ["ws://ma.local:8927/sendspin"]

        # An active Sendspin stream is flowing to the device.
        sdk.start(None)
        sdk.audio(1_000, b"\x01\x00", "pcm")
        for _ in range(3):
            await asyncio.sleep(0)
        assert 0x06 in [f[0] for f in device.frames]

        # Direct legacy "play jazz" — HA wins.
        device.frames.clear()
        await runtime.yield_device("study")
        for _ in range(3):
            await asyncio.sleep(0)
        # Device is handed back to 0x04: clear then end of the scheduled stream.
        assert [f[0] for f in device.frames] == [0x08, 0x09]
        assert session._legacy_owns is True
        # Left the group cleanly and does NOT reconnect while legacy owns it.
        assert len(sdk.connect_calls) == 1
        # Audio that still arrives is dropped, not forwarded.
        rejected_before = session.rejected_frames
        sdk.audio(2_000, b"\x02\x00", "pcm")
        assert session.rejected_frames == rejected_before + 1

        # Legacy playback ended — the player becomes available again (reconnect,
        # not a rejoin of old audio).
        await runtime.release_device("study")
        for _ in range(3):
            await asyncio.sleep(0)
        assert session._legacy_owns is False
        assert len(sdk.connect_calls) == 2

        await runtime.close()

    asyncio.run(run())


def test_sendspin_session_applies_streaming_eq_to_audio():
    async def run():
        device = FakeDevice()
        device.eq_bands = [6.0, 0, 0, 0, 0, 0, 0, 0]  # boost
        device.eq_loudness = True
        session = em_sendspin.SendspinDeviceSession("study", "Study", device, FakeClock())
        sdk = FakeSDK()
        session.attach(sdk)
        await session.start()
        sdk.start(None)

        # 1600 samples of 125Hz sine
        t = np.arange(1600) / 48000.0
        pcm = (np.sin(2 * np.pi * 125 * t) * 0.2 * 32767).astype(np.int16).tobytes()
        sdk.audio(1_000, pcm, "pcm")
        await asyncio.sleep(0)
        await session.close()

        assert len(device.frames) == 2  # start + pcm
        pcm_frame = device.frames[1]
        assert pcm_frame[0] == 0x07  # pcm frame
        # Audio payload inside frame (bytes 17:) should have EQ applied and differ from input
        frame_payload = pcm_frame[17:]
        assert len(frame_payload) == len(pcm)
        assert frame_payload != pcm

    asyncio.run(run())


def test_sendspin_eq_chain_has_limiter_and_bass_guard_like_legacy_path():
    # Parity with em_player's legacy 0x04 music feed: the Sendspin EQ must
    # carry the same clip protection. Without it this path applied EQ+loudness
    # bass boost with nothing to catch the overshoot, so the mix hard-clipped
    # (peak=32767 on the AEC far-end tap) and sounded flat / bass-light while
    # legacy music, guarded, did not.
    async def run():
        device = FakeDevice()
        device.eq_loudness = True
        device.limiter_enabled = True
        device.bass_guard_enabled = True
        session = em_sendspin.SendspinDeviceSession("study", "Study", device, FakeClock())
        sdk = FakeSDK()
        session.attach(sdk)
        await session.start()
        sdk.start(None)
        await asyncio.sleep(0)
        assert session._eq is not None
        assert session._eq.limiter is not None
        assert session._eq.guard is not None
        await session.close()

    asyncio.run(run())
