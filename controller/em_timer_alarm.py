"""Timer alarm playback orchestration.

The timer state machine remains in :mod:`em_timers`; this module owns only the
physical alarm task and is deliberately dependency-injected for testing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

import em_player
import em_timers


PlaybackCallback = Callable[[object, bytes, asyncio.Event], Awaitable[None]]
StateCallback = Callable[[str, bool], Awaitable[None]]


class TimerAlarmRunner:
    def __init__(
        self,
        get_device: Callable[[str], object | None],
        playback: PlaybackCallback,
        *,
        on_state: StateCallback | None = None,
        sound_file: str = em_timers.ALARM_SOUND_FILE,
        max_ring_s: float = em_timers.MAX_RING_S,
    ) -> None:
        self._get_device = get_device
        self._playback = playback
        self._on_state = on_state
        self._sound_file = Path(sound_file)
        self._max_ring_s = max_ring_s
        self._sound_cache: bytes | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel: dict[str, asyncio.Event] = {}

    def start(self, device_id: str, session: em_timers.AlarmSession) -> bool:
        task = self._tasks.get(device_id)
        if task is not None and not task.done():
            return False
        cancel = asyncio.Event()
        self._cancel[device_id] = cancel
        if self._on_state is not None:
            asyncio.create_task(self._on_state(device_id, True))
        task = asyncio.create_task(
            self._run(device_id, session, cancel),
            name=f"timer-alarm-{device_id}",
        )
        self._tasks[device_id] = task
        task.add_done_callback(lambda done: self._tasks.pop(device_id, None))
        return True

    def is_running(self, device_id: str) -> bool:
        task = self._tasks.get(device_id)
        return task is not None and not task.done()

    async def stop(self, device_id: str) -> bool:
        task = self._tasks.get(device_id)
        if task is None or task.done():
            return False
        self._cancel[device_id].set()
        device = self._get_device(device_id)
        if device is not None:
            await device.send_control({"type": "speaker_flush"})
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._tasks.pop(device_id, None)
        self._cancel.pop(device_id, None)
        return True

    async def disconnect(self, device_id: str) -> None:
        await self.stop(device_id)

    async def _run(
        self, device_id: str, session: em_timers.AlarmSession, cancel: asyncio.Event
    ) -> None:
        device = self._get_device(device_id)
        if device is None:
            session.delivery_failed()
            return
        pcm = await self._load_sound()
        if not pcm:
            session.delivery_failed()
            return

        await em_player.interrupt(device_id)
        deadline = asyncio.get_running_loop().time() + self._max_ring_s
        try:
            while session.current is not None and not cancel.is_set():
                timer = session.current
                await self._playback(device, pcm, cancel)
                if cancel.is_set():
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    session.timeout_current()
                    continue
                await asyncio.sleep(em_timers.BURST_GAP_S)
        finally:
            await em_player.resume_interrupted(device_id)
            if self._on_state is not None:
                await self._on_state(device_id, False)
            self._cancel.pop(device_id, None)

    async def _load_sound(self) -> bytes:
        if self._sound_cache is not None:
            return self._sound_cache
        if not self._sound_file.is_file():
            return b""
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(self._sound_file), "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", "48000", "-ac", "1", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        pcm, _stderr = await proc.communicate()
        if proc.returncode == 0 and pcm:
            self._sound_cache = pcm
        return pcm if proc.returncode == 0 else b""
