"""Gemini 3.5 Transcribe STT platform — HACS-owned streaming STT.

One shared ``GeminiTranscribeEntity`` per config entry, correlated per-turn
via ``CorrelatedMicStream`` rather than per-device. See
``docs/design/hacs-stt-plan.md`` for the full design.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from typing import AsyncIterable

# ContextVar to carry the per-turn on_partial callback through HA's
# audio-enhancer/VAD wrapping. The pipeline's process_enhance_audio →
# _speech_to_text_stream yields a *new* async generator, so the STT no longer
# receives the original CorrelatedMicStream object and isinstance() would
# always be False. The satellite sets this var before calling the pipeline
# and resets it after, so the STT can still correlate without a shared dict.
_partial_callback_var: contextvars.ContextVar = contextvars.ContextVar(
    "_partial_callback", default=None
)

from homeassistant.components.stt import (
    SpeechAudioProcessing,
    SpeechResult,
    SpeechResultState,
    SpeechToTextEntity,
)

from .const import (
    CONF_CUSTOM_VOCABULARY,
    CONF_GEMINI_API_KEY,
    CONF_LANGUAGE_CODES,
    CONF_TRANSCRIPTION_MODE,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Language helpers
# ---------------------------------------------------------------------------

def _parse_language_codes(raw: str) -> list[str]:
    """Comma-separated BCP-47 codes as the user typed them.

    e.g. ``"en-US, es-ES" -> ["en-US", "es-ES"]``.  Empty/unset -> []
    (auto-detect), same as leaving ``language_codes`` off entirely per
    Gemini's own default.  No validation against Google's supported-language
    list — a typo'd code is Gemini's error to report at connect time, not
    ours to pre-guess.
    """
    return [code.strip() for code in raw.split(",") if code.strip()]


# ---------------------------------------------------------------------------
# Broad language allowlist — what HA is allowed to ask for.
#
# SpeechToTextEntity.check_metadata() rejects before
# async_process_audio_stream if metadata.language not in
# self.supported_languages.  This must be broad so a pipeline configured
# with a language outside CONF_LANGUAGE_CODES isn't spuriously rejected.
# See stt.py "supported_languages needs to be broad, not narrow" in the
# design doc.  Values are Google's "Supported languages" table for
# gemini-3.5-transcribe-live (~100 BCP-47 codes).
# ---------------------------------------------------------------------------

_SUPPORTED_LANGUAGES: list[str] = [
    "af-ZA",
    "am-ET",
    "ar-AE",
    "ar-BH",
    "ar-DZ",
    "ar-EG",
    "ar-IL",
    "ar-IQ",
    "ar-JO",
    "ar-KW",
    "ar-LB",
    "ar-MA",
    "ar-OM",
    "ar-QA",
    "ar-SA",
    "ar-PS",
    "ar-TN",
    "az-AZ",
    "bg-BG",
    "bn-BD",
    "bn-IN",
    "bs-BA",
    "ca-ES",
    "cs-CZ",
    "da-DK",
    "de-AT",
    "de-CH",
    "de-DE",
    "el-GR",
    "en-AU",
    "en-CA",
    "en-GB",
    "en-IE",
    "en-IN",
    "en-NZ",
    "en-PH",
    "en-SG",
    "en-US",
    "en-ZA",
    "es-AR",
    "es-BO",
    "es-CL",
    "es-CO",
    "es-CR",
    "es-DO",
    "es-EC",
    "es-ES",
    "es-GT",
    "es-HN",
    "es-MX",
    "es-NI",
    "es-PA",
    "es-PE",
    "es-PR",
    "es-PY",
    "es-SV",
    "es-US",
    "es-UY",
    "es-VE",
    "et-EE",
    "eu-ES",
    "fa-IR",
    "fi-FI",
    "fil-PH",
    "fr-BE",
    "fr-CA",
    "fr-CH",
    "fr-FR",
    "gl-ES",
    "gu-IN",
    "he-IL",
    "hi-IN",
    "hr-HR",
    "hu-HU",
    "hy-AM",
    "id-ID",
    "is-IS",
    "it-IT",
    "ja-JP",
    "jv-ID",
    "ka-GE",
    "kk-KZ",
    "km-KH",
    "kn-IN",
    "ko-KR",
    "lo-LA",
    "lt-LT",
    "lv-LV",
    "mk-MK",
    "ml-IN",
    "mr-IN",
    "ms-MY",
    "my-MM",
    "ne-NP",
    "nl-BE",
    "nl-NL",
    "no-NO",
    "pa-Guru-IN",
    "pl-PL",
    "pt-BR",
    "pt-PT",
    "ro-RO",
    "ru-RU",
    "si-LK",
    "sk-SK",
    "sl-SI",
    "sq-AL",
    "sr-RS",
    "su-ID",
    "sv-SE",
    "sw-KE",
    "sw-TZ",
    "ta-IN",
    "ta-LK",
    "ta-MY",
    "ta-SG",
    "te-IN",
    "th-TH",
    "tr-TR",
    "uk-UA",
    "ur-IN",
    "ur-PK",
    "uz-UZ",
    "vi-VN",
    "yue-Hant-HK",
    "zh-CN",
    "zh-TW",
    "zu-ZA",
]


# ---------------------------------------------------------------------------
# CorrelatedMicStream — per-turn callback carrier
# ---------------------------------------------------------------------------

class CorrelatedMicStream:
    """Wraps the mic generator with a partial-transcript callback.

    ``assist_satellite.py`` constructs ``CorrelatedMicStream(channel.mic_frames(),
    on_partial=self._on_stt_partial)`` and hands it to HA's pipeline.  The STT
    entity ``isinstance``-checks the ``stream`` argument and, when it matches,
    calls ``on_partial`` for each interim result — no shared dict, no registry.

    The wrapper itself is an async iterable that delegates to the underlying
    generator, so HA's pipeline sees a normal audio stream.
    """

    def __init__(self, stream: AsyncIterable[bytes], on_partial=None):
        self._stream = stream
        self._on_partial = on_partial
        self._aiter = None  # type: ignore[var-annotated]

    def on_partial(self, text: str) -> None:
        if self._on_partial is not None:
            try:
                self._on_partial(text)
            except Exception:  # noqa: BLE001 — callback must not break STT
                _LOGGER.exception("on_partial callback failed")

    def __aiter__(self):
        # Cache the underlying async iterator so a single CorrelatedMicStream
        # can be iterated exactly once, regardless of how many times
        # __aiter__ is called (HA's pipeline may call it indirectly).
        if self._aiter is None:
            # ``stream`` is an async generator from channel.mic_frames().
            if hasattr(self._stream, "__aiter__"):
                self._aiter = self._stream.__aiter__()  # type: ignore[attr-defined]
            else:
                # Fallback for test fakes that are plain async iterables.
                self._aiter = aiter(self._stream)  # type: ignore[arg-type]
        return self

    async def __anext__(self):
        if self._aiter is None:
            self.__aiter__()
        assert self._aiter is not None
        return await self._aiter.__anext__()


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------

class GeminiTranscribeEntity(SpeechToTextEntity):
    """Single shared Gemini Live STT entity per config entry."""

    def __init__(self, entry):
        super().__init__()
        self._entry = entry
        self._attr_name = "Gemini Transcribe"
        self._attr_unique_id = f"{entry.entry_id}_gemini_transcribe"

    @property
    def supported_languages(self) -> list[str]:
        return _SUPPORTED_LANGUAGES

    @property
    def supported_formats(self) -> list[str]:
        return ["wav"]

    @property
    def supported_codecs(self) -> list[str]:
        return ["pcm"]

    @property
    def supported_bit_rates(self) -> list[int]:
        return [16]

    @property
    def supported_sample_rates(self) -> list[int]:
        return [16000]

    @property
    def supported_channels(self) -> list[int]:
        return [1]

    @property
    def audio_processing(self) -> SpeechAudioProcessing:
        # The load-bearing override: without this HA's pipeline wraps our
        # audio in a VoiceCommandSegmenter (local VAD) before we see it.
        # requires_external_vad=False disables that, so Gemini's own
        # Automatic VAD owns the audio.  See docs/design/hacs-stt-plan.md
        # "audio_processing" section.
        return SpeechAudioProcessing(
            requires_external_vad=False,
            prefers_auto_gain_enabled=False,
            prefers_noise_reduction_enabled=False,
        )

    async def async_process_audio_stream(self, metadata, stream) -> SpeechResult:
        # Read fresh per call, not cached at __init__ — a mid-flight options
        # save takes effect on the next turn, not this one, and avoids a
        # reload that would tear down every entity in the config entry.
        options = self._entry.options
        # Late imports keep the module importable without google-genai
        # installed (tests fake it; production installs it via manifest).
        from google import genai  # type: ignore[import-untyped]
        from google.genai import types  # type: ignore[import-untyped]

        # AudioTranscriptionConfig gained language_codes/mode/custom_vocabulary
        # in google-genai 2.22.0; older installs (e.g. 1.59.0 in the HA stable
        # image) reject them as extra_forbidden. Fall back to an empty config
        # so transcription still works (auto-detect, VERBATIM) rather than
        # failing the turn with a ValidationError. The manifest now pins
        # >=2.22.0, so this fallback is only for already-deployed containers
        # that haven't re-installed requirements yet.
        try:
            transcription_config = types.AudioTranscriptionConfig(
                language_codes=_parse_language_codes(
                    options.get(CONF_LANGUAGE_CODES, "")
                ),
                mode=options.get(CONF_TRANSCRIPTION_MODE, "VERBATIM"),
                custom_vocabulary=options.get(CONF_CUSTOM_VOCABULARY, []),
            )
        except Exception:  # noqa: BLE001 — ValidationError on older SDK
            _LOGGER.debug(
                "AudioTranscriptionConfig with custom fields not supported by "
                "installed google-genai, falling back to empty config"
            )
            transcription_config = types.AudioTranscriptionConfig()

        config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
            input_audio_transcription=transcription_config,
        )
        # .get() not options[...]: blank key must fail at Gemini's auth step
        # with a Gemini-reported error, not as a bare KeyError before connect.
        client = genai.Client(api_key=options.get(CONF_GEMINI_API_KEY, ""))
        final_text = ""

        async with client.aio.live.connect(
            model="gemini-3.5-transcribe-live", config=config,
        ) as session:

            async def _pump():
                async for chunk in stream:
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=chunk, mime_type="audio/pcm;rate=16000"
                        )
                    )
                # Transport courtesy if mic stream itself ends before Gemini
                # has found a final — not how speech-end is normally detected
                # (Automatic VAD owns that, server-side).
                await session.send_realtime_input(audio_stream_end=True)

            pump_task = asyncio.create_task(_pump())
            try:
                async for response in session.receive():
                    content = getattr(response, "server_content", None)
                    if not content:
                        continue
                    interim = getattr(content, "interim_input_transcription", None)
                    if interim is not None:
                        text = getattr(interim, "text", None)
                        if text:
                            # Prefer the ContextVar (survives pipeline wrapping); fall back to isinstance for direct tests
                            cb = _partial_callback_var.get()
                            if cb is not None:
                                try:
                                    cb(text)
                                except Exception:  # noqa: BLE001
                                    _LOGGER.exception("ContextVar on_partial failed")
                            elif isinstance(stream, CorrelatedMicStream):
                                stream.on_partial(text)
                    final = getattr(content, "input_transcription", None)
                    if final is not None:
                        text = getattr(final, "text", None)
                        if text is not None:
                            final_text = text
                        # Gemini's Automatic VAD decided speech is complete —
                        # done regardless of whether stream still has audio.
                        break
            finally:
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task

        return SpeechResult(final_text, SpeechResultState.SUCCESS)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Gemini STT platform from a config entry."""
    async_add_entities([GeminiTranscribeEntity(entry)])
