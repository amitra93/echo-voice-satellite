"""Native Assist satellite facade; the controller's turn engine stays turn
authority (mic capture, playback, barge-in all live there — see
docs/design/full-duplex-plan.md). This entity's only job is to run HA's Assist pipeline
against the controller-offered mic stream and stream the resulting TTS back
down the same per-turn audio channel.

Only ANNOUNCE is advertised, not START_CONVERSATION: `em_turn_engine.create_turn`
only implements the "announcement" turn kind today (conversation-kind turns
are always controller-triggered, by wake word or button). Advertising a
feature the engine 400s on would be exactly the "control that silently does
nothing" CLAUDE.md's capability-negotiation rule exists to forbid.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from homeassistant.components import tts
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
try:
    from homeassistant.components.assist_pipeline import PipelineStage
except ImportError:
    try:
        from homeassistant.components.assist_pipeline.pipeline import PipelineStage
    except ImportError:
        class PipelineStage:
            STT = "stt"
from homeassistant.components.assist_satellite import (
    AssistSatelliteAnnouncement,
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityFeature,
    AssistSatelliteWakeWord,
)

from .client import ControllerError
from .const import DOMAIN
from .entities import EchoCoordinatorEntity, add_dynamic_entities
from .tts_stream import TTSIncompatible, stream_result_to_audio

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data["echo_voice_satellite"][entry.entry_id]["coordinator"]
    entry.async_on_unload(add_dynamic_entities(
        coordinator, async_add_entities,
        lambda record: [EchoAssistSatellite(coordinator, record["device_id"])],
    ))


class EchoAssistSatellite(EchoCoordinatorEntity, AssistSatelliteEntity):
    _attr_supported_features = AssistSatelliteEntityFeature.ANNOUNCE

    def __init__(self, coordinator, device_id: str):
        super().__init__(coordinator, device_id)
        self.client = coordinator.client
        self._active_turn_id: int | None = None
        self._active_channel = None
        # Identity changes even if an old turn id is somehow reused. Every
        # asynchronous callback retains this token and must prove ownership
        # before it can touch the controller rendezvous.
        self._active_turn_token: object | None = None
        self._tts_task: asyncio.Task | None = None
        self._tts_turn_token: object | None = None
        self._pipeline_task: asyncio.Task | None = None
        self._offer_lock = asyncio.Lock()
        self._transcript_sent = False
        self._endpoint_sent = False
        self._continue_conversation = False
        self._attr_unique_id = f"{device_id}_assist_satellite"
        self._attr_name = "Voice Assistant"
        self._event_remove = coordinator.async_add_event_listener(self._async_gateway_event)

    async def async_will_remove_from_hass(self) -> None:
        self._event_remove()
        turn_id = self._active_turn_id
        channel = self._active_channel
        tasks = [task for task in (self._pipeline_task, self._tts_task)
                 if task is not None]
        for task in tasks:
            task.cancel()
        self._active_turn_id = None
        self._active_channel = None
        self._active_turn_token = None
        self._pipeline_task = None
        self._tts_task = None
        self._tts_turn_token = None
        if turn_id is not None:
            with contextlib.suppress(ControllerError):
                await self.client.async_turn_action(turn_id, "reject")
        if channel is not None:
            with contextlib.suppress(Exception):
                await channel.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().async_will_remove_from_hass()

    async def async_added_to_hass(self) -> None:
        """Register this satellite as the owner of its HA timer events."""
        await super().async_added_to_hass()
        from homeassistant.components.intent import async_register_timer_handler
        from homeassistant.helpers import device_registry as dr

        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, self.device_id)}
        )
        if device is None:
            raise RuntimeError(f"HA device registry entry missing for {self.device_id}")

        self._timer_unregister = async_register_timer_handler(
            self.hass, device.id, self._timer_event
        )
        self.async_on_remove(self._timer_unregister)

    def _timer_event(self, event, timer) -> None:
        """TimerManager invokes handlers synchronously from its event loop."""
        self.hass.async_create_task(
            self._async_forward_timer_event(event, timer),
            name=f"echo-timer-event-{timer.id}",
        )

    async def _async_forward_timer_event(self, event, timer) -> None:
        try:
            await self.client.async_timer_event(
                self.device_id,
                {
                    "event": getattr(event, "value", event),
                    "timer_id": timer.id,
                    "ha_device_id": timer.device_id,
                    "name": timer.name,
                    "total_seconds": timer.created_seconds,
                    "seconds_left": timer.seconds_left,
                    "is_active": timer.is_active,
                },
            )
        except ControllerError:
            _LOGGER.exception("Failed to forward timer %s for %s", timer.id, self.device_id)

    @property
    def available(self) -> bool:
        return super().available and not bool(self.record.get("muted"))

    async def _async_gateway_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "wake.offer" and event.get("device_id") == self.device_id:
            offer = dict(event)
            # Spawned as a task so it never blocks the /api/events reader,
            # which must keep dispatching the correlated replies this
            # handler itself waits on.
            asyncio.create_task(
                self._handle_wake_offer(offer), name=f"echo-wake-offer-{self.device_id}"
            )
        elif (
            event.get("type") in {"turn.cancel", "turn.terminal"}
            and event.get("device_id") == self.device_id
            and event.get("turn_id") == self._active_turn_id
        ):
            # Stop is targeted: never let a stale cancellation for an earlier
            # turn interrupt the current one on this device.
            turn_id = self._active_turn_id
            channel = self._active_channel
            pipeline_task = self._pipeline_task
            tts_task = self._tts_task
            # Clear ownership before cancelling tasks. Their finally blocks and
            # queued pipeline events can then never resolve a newer turn.
            self._active_turn_id = None
            self._active_channel = None
            self._active_turn_token = None
            self._pipeline_task = None
            self._tts_task = None
            self._tts_turn_token = None
            if pipeline_task is not None:
                pipeline_task.cancel()
            if tts_task is not None:
                tts_task.cancel()
            if event.get("type") == "turn.cancel":
                with contextlib.suppress(ControllerError):
                    await self.client.async_turn_action(turn_id, "tts/end")
            self.tts_response_finished()
            if channel is not None:
                with contextlib.suppress(Exception):
                    await channel.close()
            current = asyncio.current_task()
            await asyncio.gather(
                *(task for task in (pipeline_task, tts_task) if task is not None and task is not current),
                return_exceptions=True,
            )


    async def _handle_wake_offer(self, offer: dict[str, Any]) -> None:
        turn_id = offer.get("turn_id")
        async with self._offer_lock:
            if self._active_turn_id is not None:
                with contextlib.suppress(ControllerError):
                    await self.client.async_turn_action(turn_id, "reject")
                return
            try:
                channel = await self.client.async_attach_audio(turn_id)
            except ControllerError:
                _LOGGER.exception("Failed to attach audio channel for turn %s", turn_id)
                with contextlib.suppress(ControllerError):
                    await self.client.async_turn_action(turn_id, "reject")
                return
            self._active_turn_id = turn_id
            self._active_channel = channel
            token = object()
            self._active_turn_token = token
            self._transcript_sent = False
            self._endpoint_sent = False
            self._continue_conversation = False
            self._tts_task = None
            try:
                # The controller does not grant the device until HA has accepted
                # this provisional offer. This keeps the device ring and mic dark
                # when the integration cannot own the turn.
                await self.client.async_turn_action(turn_id, "accept")
            except ControllerError:
                _LOGGER.exception("Failed to accept turn %s", turn_id)
                self._active_turn_id = None
                self._active_channel = None
                self._active_turn_token = None
                with contextlib.suppress(ControllerError):
                    await self.client.async_turn_action(turn_id, "reject")
                with contextlib.suppress(Exception):
                    await channel.close()
                return
            self._pipeline_task = asyncio.create_task(
                self._run_wake_pipeline(
                    channel, turn_id, token, timer_speech=offer.get("trigger") == "timer-speech"
                ), name=f"echo-wake-{self.device_id}"
            )

    def _owns_turn(self, turn_id: int, token: object, channel) -> bool:
        return (
            self._active_turn_id == turn_id
            and self._active_turn_token is token
            and self._active_channel is channel
        )

    async def _run_wake_pipeline(
        self, channel, turn_id: int | None = None, token: object | None = None,
        timer_speech: bool = False,
    ) -> None:
        # Optional arguments retain the direct unit-test call shape; production
        # always passes the values captured when the offer was accepted.
        if turn_id is None:
            turn_id = self._active_turn_id
            token = self._active_turn_token
        if turn_id is None or token is None or not self._owns_turn(turn_id, token, channel):
            return
        try:
            if timer_speech:
                await super().async_accept_pipeline_from_satellite(
                    channel.mic_frames(), end_stage=PipelineStage.STT
                )
            else:
                await super().async_accept_pipeline_from_satellite(channel.mic_frames())
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Pipeline failed for %s", self.device_id)
        finally:
            if self._pipeline_task is asyncio.current_task():
                self._pipeline_task = None
            if not self._owns_turn(turn_id, token, channel):
                return
            if self._tts_task is not None and self._tts_turn_token is token:
                await asyncio.gather(self._tts_task, return_exceptions=True)
            elif self._owns_turn(turn_id, token, channel):
                # A speech-start timeout ends the pipeline without STT_END, so
                # no normal endpoint action reached the controller. Resolve the
                # mic side before resolving the empty TTS side.
                try:
                    if not self._endpoint_sent:
                        await self.client.async_turn_action(
                            turn_id, "endpoint"
                        )
                        self._endpoint_sent = True
                    await self.client.async_turn_action(turn_id, "tts/end")
                except ControllerError:
                    pass

    @property
    def is_on(self) -> bool:
        return self._active_turn_id is not None

    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        """Report the device's current wake word — read-only from HA's side.

        AssistSatelliteEntity declares this @abstractmethod; without an
        implementation HA cannot construct the entity at all (confirmed by
        instantiating this class in tests — a TypeError at platform setup,
        not a runtime surprise). Wake word selection is controller-side
        config (CLAUDE.md), the same posture the ESPHome-mode satellite took
        (`VoiceAssistantConfigurationResponse`): one model, nothing to
        choose between, so HA's dropdown has exactly one option.
        """
        model_id = self.record.get("wake_model_id")
        return AssistSatelliteConfiguration(
            available_wake_words=(
                [AssistSatelliteWakeWord(id=model_id, wake_word=model_id, trained_languages=["en"])]
                if model_id else []
            ),
            active_wake_words=[model_id] if model_id else [],
            max_active_wake_words=1,
        )

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        raise ValueError(
            "wake word is set in the EchoMuse dashboard, not from Home Assistant"
        )

    async def async_announce(self, announcement: AssistSatelliteAnnouncement) -> None:
        if announcement.media_id_source != "tts" or not announcement.tts_token:
            raise ValueError("announcement must be resolved to a TTS result stream")
        created = await self.client.async_create_turn(self.device_id, "announcement")
        turn_id = created["turn_id"]
        channel = await self.client.async_attach_audio(turn_id)
        self._active_turn_id = turn_id
        self._active_channel = channel
        token = object()
        self._active_turn_token = token
        self._tts_task = asyncio.current_task()
        self._tts_turn_token = token
        try:
            await self.client.async_turn_action(turn_id, "tts/start")
            result = tts.async_get_stream(self.hass, announcement.tts_token)
            if result is None:
                raise TTSIncompatible("Home Assistant did not provide the TTS result stream")
            await stream_result_to_audio(result, channel)
            await self.client.async_turn_action(turn_id, "tts/end")
        except (ControllerError, TTSIncompatible) as exc:
            raise ValueError(str(exc)) from exc
        finally:
            if self._tts_task is asyncio.current_task():
                self._tts_task = None
                self._tts_turn_token = None
            if self._owns_turn(turn_id, token, channel):
                # turn.terminal (pushed once the engine's playback finishes)
                # will close the channel; if it never arrives, don't leak one.
                pass

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        if self._active_turn_id is not None and self._active_turn_token is not None:
            # PipelineEvent has no turn id. Bind it while HA invokes this
            # callback, rather than when its asynchronous forwarding runs.
            self.hass.async_create_task(self._async_pipeline_event(
                event, self._active_turn_id, self._active_turn_token, self._active_channel
            ))
        self.async_write_ha_state()

    async def _async_pipeline_event(
        self, event: PipelineEvent, turn_id: int | None = None,
        token: object | None = None, channel=None,
    ) -> None:
        if turn_id is None:
            turn_id = self._active_turn_id
            token = self._active_turn_token
            channel = self._active_channel
        if turn_id is None or token is None or channel is None or not self._owns_turn(turn_id, token, channel):
            return
        event_type = event.type
        try:
            if event_type == PipelineEventType.STT_END:
                if not self._transcript_sent:
                    text = (event.data or {}).get("stt_output", {}).get("text")
                    if isinstance(text, str) and text:
                        if not self._owns_turn(turn_id, token, channel):
                            return
                        await self.client.async_turn_action(
                            turn_id, "transcript", {"text": text, "is_final": True}
                        )
                        self._transcript_sent = True
                if not self._owns_turn(turn_id, token, channel):
                    return
                await self.client.async_turn_action(turn_id, "endpoint")
                self._endpoint_sent = True
            elif event_type == PipelineEventType.INTENT_END:
                response = (event.data or {}).get("intent_output", {}).get("response", {})
                speech = response.get("speech", {}).get("plain", {}).get("speech")
                if isinstance(speech, str) and speech:
                    if not self._owns_turn(turn_id, token, channel):
                        return
                    await self.client.async_turn_action(
                        turn_id, "tts-text", {"text": speech}
                    )
                continue_conversation = bool(
                    (event.data or {}).get("intent_output", {}).get("continue_conversation")
                )
                self._continue_conversation = continue_conversation
                if not self._owns_turn(turn_id, token, channel):
                    return
                await self.client.async_turn_action(
                    turn_id, "pipeline-event",
                    {"event": "intent_end", "continue_conversation": continue_conversation},
                )
            elif event_type == PipelineEventType.TTS_END:
                tts_token = (event.data or {}).get("tts_output", {}).get("token")
                if tts_token and self._tts_task is None and self._owns_turn(turn_id, token, channel):
                    self._tts_turn_token = token
                    self._tts_task = asyncio.create_task(
                        self._stream_pipeline_tts(tts_token, turn_id, token, channel)
                    )
            elif event_type == PipelineEventType.ERROR:
                if not self._owns_turn(turn_id, token, channel):
                    return
                await self.client.async_turn_action(
                    turn_id, "pipeline-event", {"event": "error"}
                )
        except ControllerError:
            _LOGGER.exception("Turn action failed for turn %s (%s)", turn_id, event_type)

    async def _stream_pipeline_tts(
        self, token: str, turn_id: int, turn_token: object | None = None, channel=None
    ) -> None:
        if channel is None:
            # Compatibility with callers/tests using the previous signature.
            channel = turn_token
            turn_token = self._active_turn_token
        if turn_token is None or not self._owns_turn(turn_id, turn_token, channel):
            return
        try:
            await self.client.async_turn_action(turn_id, "tts/start")
            result = tts.async_get_stream(self.hass, token)
            if result is None:
                raise TTSIncompatible("Home Assistant did not provide the TTS result stream")
            await stream_result_to_audio(result, channel)
            if self._owns_turn(turn_id, turn_token, channel):
                await self.client.async_turn_action(turn_id, "tts/end")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A provider failure can surface while ResultStream is consumed
            # (for example, a cloud-TTS quota error), not just as
            # TTSIncompatible. The controller is waiting for this terminal
            # signal; without it its audio-timeout spinner lasts two minutes.
            _LOGGER.exception("TTS stream failed for turn %s", turn_id)
            with contextlib.suppress(ControllerError):
                if self._owns_turn(turn_id, turn_token, channel):
                    await self.client.async_turn_action(turn_id, "tts/end")
        finally:
            if self._tts_task is asyncio.current_task():
                self._tts_task = None
                self._tts_turn_token = None

    def tts_response_finished(self) -> None:
        # The base implementation is what actually transitions the entity's
        # state out of RESPONDING (AssistSatelliteEntity._set_state(IDLE),
        # which itself calls async_write_ha_state()) — this override used to
        # replace that with a bare state-write, discarding the transition
        # entirely and leaving the entity stuck reporting "responding" for
        # the life of the connection once the first TTS response finished.
        super().tts_response_finished()
