"""Timer alarm playback orchestration.

The timer state machine remains in :mod:`em_timers`; this module owns only the
physical alarm task and is deliberately dependency-injected for testing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

import em_player
import em_timers


log = logging.getLogger(__name__)


PlaybackCallback = Callable[[object, bytes, asyncio.Event], Awaitable[None]]
StateCallback = Callable[[str, bool], Awaitable[None]]
CurrentCallback = Callable[[str], Awaitable[None]]
# Fired after the runner itself mutates the session (unanswered-ring
# timeout, undeliverable alarm) — the same moments the event-driven paths
# push a fresh timer.alarm snapshot, and for the same reason: without it
# downstream consumers (HACS presence, the card) keep showing a "ringing"
# timer the controller has already dropped, and a later dismiss correctly
# reports false while the card never clears.
ChangedCallback = Callable[[str], Awaitable[None]]


class TimerAlarmRunner:
    def __init__(
        self,
        get_device: Callable[[str], object | None],
        playback: PlaybackCallback,
        *,
        on_state: StateCallback | None = None,
        on_current: CurrentCallback | None = None,
        on_changed: ChangedCallback | None = None,
        sound_file: str = em_timers.ALARM_SOUND_FILE,
        max_ring_s: float = em_timers.MAX_RING_S,
    ) -> None:
        self._get_device = get_device
        self._playback = playback
        self._on_state = on_state
        self._on_current = on_current
        self._on_changed = on_changed
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
            try:
                await device.send_control({"type": "speaker_flush"})
            except Exception as e:
                # A closed control socket cannot flush, but it must not leave
                # the alarm task running after its device is gone.
                log.info("[%s] Timer alarm flush skipped: %s", device_id, e)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._tasks.pop(device_id, None)
        self._cancel.pop(device_id, None)
        return True

    async def disconnect(self, device_id: str) -> None:
        await self.stop(device_id)

    async def _notify_changed(self, device_id: str) -> None:
        if self._on_changed is not None:
            await self._on_changed(device_id)

    def _clock(self) -> float:
        return asyncio.get_running_loop().time()

    async def _run(
        self, device_id: str, session: em_timers.AlarmSession, cancel: asyncio.Event
    ) -> None:
        device = self._get_device(device_id)
        if device is None:
            session.disconnect()
            await self._notify_changed(device_id)
            return
        try:
            pcm = await self._load_sound()
        except OSError:
            # No ffmpeg / unreadable file: create_subprocess_exec raises
            # instead of returning empty bytes, and an uncaught raise here
            # would leave the session ringing with no task left to clear
            # it — the same silent-stuck shape as the paths below.
            session.disconnect()
            await self._notify_changed(device_id)
            return
        if not pcm:
            session.disconnect()
            await self._notify_changed(device_id)
            return

        firing = False
        interrupted = False
        current_timer_id: str | None = None
        deadline = 0.0
        try:
            if self._on_state is not None:
                firing = True
                await self._on_state(device_id, True)
            await em_player.interrupt(device_id)
            interrupted = True
            while session.current is not None and not cancel.is_set():
                timer = session.current
                if timer.timer_id != current_timer_id:
                    current_timer_id = timer.timer_id
                    if self._on_current is not None:
                        await self._on_current(device_id)
                    deadline = self._clock() + self._max_ring_s
                await self._playback(device, pcm, cancel)
                if cancel.is_set():
                    break
                if self._clock() >= deadline:
                    session.timeout_current()
                    await self._notify_changed(device_id)
                    continue
                await asyncio.sleep(em_timers.BURST_GAP_S)
        finally:
            try:
                if interrupted:
                    await em_player.resume_interrupted(device_id)
            finally:
                try:
                    if firing and self._on_state is not None:
                        await self._on_state(device_id, False)
                finally:
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
