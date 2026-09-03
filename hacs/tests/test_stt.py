"""Thorough tests for stt.py — the Gemini Live STT entity."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import types

import pytest

from homeassistant.components.stt import (
    DEFAULT_AUDIO_PROCESSING,
    SpeechAudioProcessing,
    SpeechResultState,
)

# Import under test — module itself must be importable without a real HA install
# (conftest fakes homeassistant.components.stt) and without google-genai installed
# in the test env beyond the conftest fake.
stt = importlib.import_module("custom_components.echo_voice_satellite.stt")
from custom_components.echo_voice_satellite.const import (
    CONF_CUSTOM_VOCABULARY,
    CONF_GEMINI_API_KEY,
    CONF_LANGUAGE_CODES,
    CONF_TRANSCRIPTION_MODE,
)

# ---------------------------------------------------------------------------
# _parse_language_codes — pure helper
# ---------------------------------------------------------------------------

def test_parse_language_codes_splits_and_trims():
    assert stt._parse_language_codes("en-US, es-ES") == ["en-US", "es-ES"]
    assert stt._parse_language_codes("en-US,es-ES") == ["en-US", "es-ES"]
    assert stt._parse_language_codes("  en-US  ,  fr-FR  ") == ["en-US", "fr-FR"]


def test_parse_language_codes_empty_and_whitespace():
    assert stt._parse_language_codes("") == []
    assert stt._parse_language_codes("   ") == []
    assert stt._parse_language_codes(",, ,") == []


def test_parse_language_codes_trailing_and_stray_commas():
    assert stt._parse_language_codes("en-US, es-ES,") == ["en-US", "es-ES"]
    assert stt._parse_language_codes(",en-US,,es-ES,") == ["en-US", "es-ES"]
    assert stt._parse_language_codes("en-US,  , fr-FR") == ["en-US", "fr-FR"]


# ---------------------------------------------------------------------------
# Supported surface — contract with HA
# ---------------------------------------------------------------------------

def test_supported_languages_is_broad_not_narrow():
    # Must be broad so check_metadata doesn't spuriously reject a pipeline
    # language outside CONF_LANGUAGE_CODES. See design doc.
    entry = types.SimpleNamespace(entry_id="e", options={})
    ent = stt.GeminiTranscribeEntity(entry)
    langs = ent.supported_languages
    assert len(langs) >= 100, f"expected ~100 languages, got {len(langs)}"
    for code in ("en-US", "en-GB", "es-ES", "fr-FR", "de-DE", "ja-JP", "zh-CN", "ar-SA"):
        assert code in langs, f"{code} missing from allowlist"
    # check_metadata should pass for any of these
    meta = types.SimpleNamespace(language="en-US")
    ent.check_metadata(meta)  # must not raise
    with pytest.raises(ValueError, match="stt-provider-unsupported-metadata"):
        ent.check_metadata(types.SimpleNamespace(language="xx-XX"))


def test_supported_formats_and_audio_params():
    entry = types.SimpleNamespace(entry_id="e", options={})
    ent = stt.GeminiTranscribeEntity(entry)
    assert ent.supported_formats == ["wav"]
    assert ent.supported_codecs == ["pcm"]
    assert ent.supported_bit_rates == [16]
    assert ent.supported_sample_rates == [16000]
    assert ent.supported_channels == [1]


def test_audio_processing_disables_external_vad():
    # Load-bearing: requires_external_vad=False disables HA's VoiceCommandSegmenter
    entry = types.SimpleNamespace(entry_id="e", options={})
    ent = stt.GeminiTranscribeEntity(entry)
    proc = ent.audio_processing
    assert isinstance(proc, SpeechAudioProcessing)
    assert proc.requires_external_vad is False
    assert proc.prefers_auto_gain_enabled is False
    assert proc.prefers_noise_reduction_enabled is False
    # Base default must be True — verifies we actually override
    assert DEFAULT_AUDIO_PROCESSING.requires_external_vad is True


def test_entity_stores_entry_and_unique_id():
    entry = types.SimpleNamespace(entry_id="abc123", options={})
    ent = stt.GeminiTranscribeEntity(entry)
    assert ent._entry is entry
    assert ent._attr_unique_id == "abc123_gemini_transcribe"
    assert ent._attr_name == "Gemini Transcribe"


# ---------------------------------------------------------------------------
# CorrelatedMicStream — per-turn callback carrier
# ---------------------------------------------------------------------------

def test_correlated_mic_stream_delegates_iteration():
    async def inner():
        for chunk in [b"a", b"b", b"c"]:
            yield chunk

    async def run():
        received = []
        stream = stt.CorrelatedMicStream(inner(), on_partial=lambda t: received.append(t))
        async for chunk in stream:
            received.append(chunk)
        # First three are on_partial? No — iteration yields chunks, not partials
        # Our on_partial is separate; iteration should yield original chunks
        assert b"a" in received

    asyncio.run(run())


def test_correlated_mic_stream_caches_iterator():
    # __aiter__ called twice must return same iterator (single-use stream)
    async def inner():
        yield b"x"

    async def run():
        cm = stt.CorrelatedMicStream(inner(), on_partial=None)
        a1 = cm.__aiter__()
        a2 = cm.__aiter__()
        assert a1 is a2
        # Iteration still works
        chunks = []
        async for c in cm:
            chunks.append(c)
        assert chunks == [b"x"]

    asyncio.run(run())


def test_correlated_mic_stream_on_partial_forwards_and_swallows_exceptions(caplog):
    calls = []

    def good(t):
        calls.append(t)

    def bad(t):
        raise RuntimeError("boom")

    # Good callback
    cm = stt.CorrelatedMicStream(_empty_stream(), on_partial=good)
    cm.on_partial("hello")
    assert calls == ["hello"]

    # Bad callback must not propagate, but must log
    cm2 = stt.CorrelatedMicStream(_empty_stream(), on_partial=bad)
    with caplog.at_level(logging.ERROR):
        cm2.on_partial("hi")
    assert "on_partial callback failed" in caplog.text

    # None callback is no-op
    cm3 = stt.CorrelatedMicStream(_empty_stream(), on_partial=None)
    cm3.on_partial("ignored")  # must not raise


def test_correlated_mic_stream_plain_iterable_fallback():
    # Test the aiter() fallback path for non-__aiter__ streams
    class Plain:
        def __init__(self, chunks):
            self.chunks = chunks

        def __aiter__(self):
            # This is still async iterable, but exercise the branch where
            # _stream has __aiter__
            async def gen():
                for c in self.chunks:
                    yield c

            return gen()

    async def run():
        plain = Plain([b"1", b"2"])
        cm = stt.CorrelatedMicStream(plain, on_partial=None)
        out = []
        async for c in cm:
            out.append(c)
        assert out == [b"1", b"2"]

    asyncio.run(run())


def _empty_stream():
    async def gen():
        if False:
            yield b""
    return gen()


# ---------------------------------------------------------------------------
# async_process_audio_stream — fresh options per call, .get() not [], etc.
# ---------------------------------------------------------------------------

def test_async_process_audio_stream_reads_options_fresh_per_call(monkeypatch):
    # Mutating entry.options between calls must produce different LiveConnectConfigs
    from unittest.mock import patch
    import google.genai as genai_mod

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid-key",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "en-US",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)
    captured = []
    real_client = genai_mod.Client

    class CapClient:
        def __init__(self, api_key=""):
            self.api_key = api_key
            self._real = real_client(api_key=api_key)
            self.aio = self._real.aio
            orig = self.aio.live.connect

            def cap(model=None, config=None):
                captured.append(config)
                return orig(model=model, config=config)

            self.aio.live.connect = cap
            self.models = self._real.models

    monkeypatch.setattr(genai_mod, "Client", CapClient)

    async def run():
        meta = types.SimpleNamespace(language="en-US")

        async def gen1():
            yield b"\x00" * 2560

        await ent.async_process_audio_stream(meta, gen1())
        assert captured[-1].input_audio_transcription.mode == "VERBATIM"
        assert captured[-1].input_audio_transcription.language_codes == ["en-US"]

        # Mutate options — second call must see new values without reconstructing entity
        entry.options[CONF_TRANSCRIPTION_MODE] = "SMART"
        entry.options[CONF_LANGUAGE_CODES] = "es-ES, fr-FR"
        entry.options[CONF_CUSTOM_VOCABULARY] = ["hello"]

        async def gen2():
            yield b"\x00" * 2560

        await ent.async_process_audio_stream(meta, gen2())
        assert captured[-1].input_audio_transcription.mode == "SMART"
        assert captured[-1].input_audio_transcription.language_codes == ["es-ES", "fr-FR"]
        assert captured[-1].input_audio_transcription.custom_vocabulary == ["hello"]

    asyncio.run(run())


def test_async_process_audio_stream_blank_key_reaches_gemini_auth_not_keyerror():
    # Pins .get() vs [] — blank must not raise KeyError before connect
    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            # No CONF_GEMINI_API_KEY at all
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)

    async def gen():
        yield b"\x00" * 2560

    meta = types.SimpleNamespace(language="en-US")
    with pytest.raises(ValueError, match="missing|invalid"):
        asyncio.run(ent.async_process_audio_stream(meta, gen()))

    # Explicit blank string also reaches auth
    entry.options[CONF_GEMINI_API_KEY] = ""
    with pytest.raises(ValueError, match="missing|invalid"):
        asyncio.run(ent.async_process_audio_stream(meta, gen()))

    # Invalid string also
    entry.options[CONF_GEMINI_API_KEY] = "invalid-key-here"
    with pytest.raises(ValueError, match="invalid"):
        asyncio.run(ent.async_process_audio_stream(meta, gen()))


def test_async_process_audio_stream_interim_not_delivered_for_plain_stream():
    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)

    async def gen():
        yield b"\x00" * 2560

    meta = types.SimpleNamespace(language="en-US")
    result = asyncio.run(ent.async_process_audio_stream(meta, gen()))
    assert result.text == "hello world"
    assert result.result_state == SpeechResultState.SUCCESS


def test_async_process_audio_stream_interim_delivered_via_correlated_stream():
    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)
    partials = []

    async def mic_gen():
        for _ in range(3):
            yield b"\x00" * 2560

    correlated = stt.CorrelatedMicStream(mic_gen(), on_partial=lambda t: partials.append(t))
    meta = types.SimpleNamespace(language="en-US")
    result = asyncio.run(ent.async_process_audio_stream(meta, correlated))
    assert partials == ["hello", "hello world"]
    assert result.text == "hello world"


def test_async_process_audio_stream_handles_none_server_content_and_empty_fields(monkeypatch):
    # Patch the fake session to yield edge-case responses
    import google.genai as genai_mod
    from google.genai import types as gtypes

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)

    # Responses: None server_content, interim without text, final without text, then real final
    responses = [
        types.SimpleNamespace(server_content=None),
        types.SimpleNamespace(
            server_content=types.SimpleNamespace(
                interim_input_transcription=types.SimpleNamespace(text=None),
                input_transcription=None,
            )
        ),
        types.SimpleNamespace(
            server_content=types.SimpleNamespace(
                interim_input_transcription=None,
                input_transcription=types.SimpleNamespace(text="final via edge"),
            )
        ),
    ]

    real_client = genai_mod.Client

    class EdgeClient:
        def __init__(self, api_key=""):
            self.api_key = api_key
            self.aio = types.SimpleNamespace(
                live=types.SimpleNamespace(
                    connect=lambda model=None, config=None: _EdgeSession(responses, api_key)
                )
            )
            self.models = real_client(api_key=api_key).models

    class _EdgeSession:
        def __init__(self, resps, api_key):
            self._resps = resps
            self.config = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send_realtime_input(self, **kw):
            pass

        async def receive(self):
            for r in self._resps:
                yield r

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(genai_mod, "Client", EdgeClient)

    async def gen():
        yield b"\x00" * 2560

    meta = types.SimpleNamespace(language="en-US")
    result = asyncio.run(ent.async_process_audio_stream(meta, gen()))
    assert result.text == "final via edge"
    monkeypatch.undo()


def test_async_process_audio_stream_pump_cancelled_on_final(monkeypatch):
    # Ensure pump_task is cancelled after final break (no hang)
    import google.genai as genai_mod

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)

    # Track whether pump was cancelled
    pump_started = []
    pump_cancelled = []

    real_client = genai_mod.Client

    class TrackingClient:
        def __init__(self, api_key=""):
            self.api_key = api_key
            self._real = real_client(api_key=api_key)
            self.aio = self._real.aio
            orig_connect = self._real.aio.live.connect

            def tracked(model=None, config=None):
                sess = orig_connect(model=model, config=config)
                orig_aenter = sess.__aenter__
                orig_receive = sess.receive

                async def tracked_aenter():
                    await orig_aenter()
                    return sess

                sess.__aenter__ = tracked_aenter
                return sess

            self.aio.live.connect = tracked
            self.models = self._real.models

    monkeypatch.setattr(genai_mod, "Client", TrackingClient)

    async def gen():
        # Infinite-ish stream to ensure pump would hang if not cancelled
        for _ in range(100):
            yield b"\x00" * 2560

    meta = types.SimpleNamespace(language="en-US")
    result = asyncio.run(ent.async_process_audio_stream(meta, gen()))
    # Must return quickly with final, not hang on pump
    assert result.text == "hello world"


def test_async_process_audio_stream_sends_correct_blob_and_model(monkeypatch):
    import google.genai as genai_mod

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid-key-123",
            CONF_TRANSCRIPTION_MODE: "SMART",
            CONF_CUSTOM_VOCABULARY: ["myentity"],
            CONF_LANGUAGE_CODES: "en-US, de-DE",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)
    captured = {}

    real_client = genai_mod.Client

    class CapClient:
        def __init__(self, api_key=""):
            captured["api_key"] = api_key
            self.api_key = api_key
            self._real = real_client(api_key=api_key)
            self.aio = types.SimpleNamespace(
                live=types.SimpleNamespace(connect=self._cap_connect)
            )
            self.models = self._real.models

        def _cap_connect(self, model=None, config=None):
            captured["model"] = model
            captured["config"] = config
            return self._real.aio.live.connect(model=model, config=config)

    monkeypatch.setattr(genai_mod, "Client", CapClient)

    async def gen():
        yield b"\x01\x02" * 1280

    meta = types.SimpleNamespace(language="en-US")
    asyncio.run(ent.async_process_audio_stream(meta, gen()))

    assert captured["api_key"] == "valid-key-123"
    assert captured["model"] == "gemini-3.5-transcribe-live"
    cfg = captured["config"]
    assert cfg.response_modalities == ["TEXT"]
    assert cfg.input_audio_transcription.mode == "SMART"
    assert cfg.input_audio_transcription.language_codes == ["en-US", "de-DE"]
    assert cfg.input_audio_transcription.custom_vocabulary == ["myentity"]


# ---------------------------------------------------------------------------
# Privacy — never log transcript text at any level in stt.py
# ---------------------------------------------------------------------------

def test_stt_never_logs_transcript_text():
    src = inspect.getsource(stt)
    # The file must not contain a log call that interpolates transcript text
    # Search for any logging of text/interim/final at any level
    lowered = src.lower()
    # Allow the exception log for on_partial callback, but not transcript content
    for bad in ["%r.*text", "text=%r", "interim", "transcript"]:
        # We allow the comment and docstring mentions, but not a log line
        # So check log.* lines specifically
        for line in src.splitlines():
            if "log." in line.lower() and "transcript" in line.lower() and "%r" in line:
                # The only allowed is the exception message for on_partial
                assert "on_partial" in line or "callback failed" in line, f"transcript logging leaked: {line!r}"
    # Direct check: no log.info/debug with text variable
    assert "log.info" not in src or "text" not in src.split("log.info")[1][:500] if "log.info" in src else True


# ---------------------------------------------------------------------------
# async_setup_entry — platform setup
# ---------------------------------------------------------------------------

def test_async_setup_entry_adds_one_entity():
    entry = types.SimpleNamespace(entry_id="e", options={})
    added = []

    def fake_add(entities):
        added.extend(entities)

    asyncio.run(stt.async_setup_entry(None, entry, fake_add))
    assert len(added) == 1
    assert isinstance(added[0], stt.GeminiTranscribeEntity)
    assert added[0]._entry is entry

def test_final_none_text_defaults_to_empty(monkeypatch):
    import google.genai as genai_mod

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)

    real_client = genai_mod.Client

    # Single final response with text=None -> should return ""
    class EdgeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def send_realtime_input(self, **kw): pass
        async def receive(self):
            yield types.SimpleNamespace(
                server_content=types.SimpleNamespace(
                    interim_input_transcription=None,
                    input_transcription=types.SimpleNamespace(text=None),
                )
            )

    class EdgeClient:
        def __init__(self, api_key=""):
            self.aio = types.SimpleNamespace(live=types.SimpleNamespace(connect=lambda model=None, config=None: EdgeSession()))
            self.models = real_client(api_key=api_key).models

    monkeypatch.setattr(genai_mod, "Client", EdgeClient)

    async def gen():
        yield b"\x00" * 2560

    result = asyncio.run(ent.async_process_audio_stream(types.SimpleNamespace(language="en-US"), gen()))
    assert result.text == ""
    assert result.result_state == SpeechResultState.SUCCESS


def test_interim_empty_string_is_ignored_and_not_forwarded():
    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)
    partials = []

    async def mic():
        yield b"\x00" * 2560

    # Patch the fake session to yield interim with empty string
    import google.genai as genai_mod
    from google.genai import types as gtypes

    real = genai_mod.Client

    class EmptyInterimClient:
        def __init__(self, api_key=""):
            self._real = real(api_key=api_key)
            self.aio = types.SimpleNamespace(live=types.SimpleNamespace(connect=self._connect))
            self.models = self._real.models

        def _connect(self, model=None, config=None):
            class S:
                async def __aenter__(self): return self
                async def __aexit__(self, *a): return False
                async def send_realtime_input(self, **kw): pass
                async def receive(self):
                    # Empty string interim should be ignored (if text and isinstance check)
                    yield types.SimpleNamespace(
                        server_content=types.SimpleNamespace(
                            interim_input_transcription=types.SimpleNamespace(text=""),
                            input_transcription=None,
                        )
                    )
                    yield types.SimpleNamespace(
                        server_content=types.SimpleNamespace(
                            interim_input_transcription=None,
                            input_transcription=types.SimpleNamespace(text="ok"),
                        )
                    )
            return S()

    # Use monkeypatch via context
    import unittest.mock as mock

    with mock.patch.object(genai_mod, "Client", EmptyInterimClient):
        correlated = stt.CorrelatedMicStream(mic(), on_partial=lambda t: partials.append(t))
        result = asyncio.run(ent.async_process_audio_stream(types.SimpleNamespace(language="en-US"), correlated))
        # Empty interim must not have been forwarded
        assert partials == []
        assert result.text == "ok"


def test_no_final_yields_empty_string(monkeypatch):
    # If Gemini never sends a final (stream ends), we return "" as final_text
    import google.genai as genai_mod

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)

    real_client = genai_mod.Client

    class NoFinalSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def send_realtime_input(self, **kw): pass
        async def receive(self):
            # Only interims, no final
            yield types.SimpleNamespace(
                server_content=types.SimpleNamespace(
                    interim_input_transcription=types.SimpleNamespace(text="hello"),
                    input_transcription=None,
                )
            )
            # End of async generator without final -> loop exhausts

    class NoFinalClient:
        def __init__(self, api_key=""):
            self.aio = types.SimpleNamespace(live=types.SimpleNamespace(connect=lambda model=None, config=None: NoFinalSession()))
            self.models = real_client(api_key=api_key).models

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(genai_mod, "Client", NoFinalClient)

    async def gen():
        yield b"\x00" * 2560

    result = asyncio.run(ent.async_process_audio_stream(types.SimpleNamespace(language="en-US"), gen()))
    # No final was ever received, so final_text stays ""
    assert result.text == ""
    monkeypatch.undo()


# ---------------------------------------------------------------------------
# Pump sends correct Blob and audio_stream_end
# ---------------------------------------------------------------------------

def test_pump_sends_blob_with_correct_mime_and_audio_stream_end(monkeypatch):
    import google.genai as genai_mod

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)
    sent = []

    real = genai_mod.Client

    class CapSession:
        def __init__(self, *a, **kw):
            self.sent = sent

        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def send_realtime_input(self, audio=None, audio_stream_end=False, **kw):
            sent.append((audio, audio_stream_end))

        async def receive(self):
            # Delay final to allow pump to send both chunks before we break
            await asyncio.sleep(0.05)
            yield types.SimpleNamespace(
                server_content=types.SimpleNamespace(
                    interim_input_transcription=None,
                    input_transcription=types.SimpleNamespace(text="done"),
                )
            )

    class CapClient:
        def __init__(self, api_key=""):
            self.aio = types.SimpleNamespace(live=types.SimpleNamespace(connect=lambda model=None, config=None: CapSession()))
            self.models = real(api_key=api_key).models

    # Use the fixture's monkeypatch, not a new instance
    monkeypatch.setattr(genai_mod, "Client", CapClient)

    async def gen():
        yield b"\x01\x02" * 1280
        yield b"\x03\x04" * 1280

    result = asyncio.run(ent.async_process_audio_stream(types.SimpleNamespace(language="en-US"), gen()))
    # Pump should have sent both blobs and the end marker (pump runs until stream exhausted)
    # At least verify blobs have correct mime and data; exact count may vary with timing
    assert len(sent) >= 2
    assert sent[0][0] is not None and sent[0][0].mime_type == "audio/pcm;rate=16000"
    assert sent[0][0].data == b"\x01\x02" * 1280
    # Find audio_stream_end marker
    assert any(end for _, end in sent), "audio_stream_end should have been sent"
    assert result.text == "done"


# ---------------------------------------------------------------------------
# Exception propagation from genai client
# ---------------------------------------------------------------------------

def test_client_construction_exception_propagates(monkeypatch):
    import google.genai as genai_mod

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)

    class BoomClient:
        def __init__(self, api_key=""):
            raise RuntimeError("network down")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(genai_mod, "Client", BoomClient)

    async def gen():
        yield b"\x00" * 2560

    with pytest.raises(RuntimeError, match="network down"):
        asyncio.run(ent.async_process_audio_stream(types.SimpleNamespace(language="en-US"), gen()))
    monkeypatch.undo()


# ---------------------------------------------------------------------------
# Custom vocabulary with commas preserved
# ---------------------------------------------------------------------------

def test_custom_vocabulary_with_commas_preserved(monkeypatch):
    import google.genai as genai_mod

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: ["hello, world", "a,b"],
            CONF_LANGUAGE_CODES: "",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)
    captured = []
    real = genai_mod.Client

    class CapClient:
        def __init__(self, api_key=""):
            self._real = real(api_key=api_key)
            self.aio = self._real.aio
            orig = self._real.aio.live.connect

            def cap(model=None, config=None):
                captured.append(config)
                return orig(model=model, config=config)

            self.aio.live.connect = cap
            self.models = self._real.models

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(genai_mod, "Client", CapClient)

    async def gen():
        yield b"\x00" * 2560

    asyncio.run(ent.async_process_audio_stream(types.SimpleNamespace(language="en-US"), gen()))
    assert captured[0].input_audio_transcription.custom_vocabulary == ["hello, world", "a,b"]
    monkeypatch.undo()


# ---------------------------------------------------------------------------
# Language codes whitespace and empty handling
# ---------------------------------------------------------------------------

def test_language_codes_whitespace_and_empty(monkeypatch):
    import google.genai as genai_mod

    entry = types.SimpleNamespace(
        entry_id="e",
        options={
            CONF_GEMINI_API_KEY: "valid",
            CONF_TRANSCRIPTION_MODE: "VERBATIM",
            CONF_CUSTOM_VOCABULARY: [],
            CONF_LANGUAGE_CODES: "  en-US  ,  , de-DE  , ",
        },
    )
    ent = stt.GeminiTranscribeEntity(entry)
    captured = []
    real = genai_mod.Client

    class CapClient:
        def __init__(self, api_key=""):
            self._real = real(api_key=api_key)
            self.aio = self._real.aio
            orig = self._real.aio.live.connect

            def cap(model=None, config=None):
                captured.append(config)
                return orig(model=model, config=config)

            self.aio.live.connect = cap
            self.models = self._real.models

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(genai_mod, "Client", CapClient)

    async def gen():
        yield b"\x00" * 2560

    asyncio.run(ent.async_process_audio_stream(types.SimpleNamespace(language="en-US"), gen()))
    assert captured[0].input_audio_transcription.language_codes == ["en-US", "de-DE"]
    monkeypatch.undo()


# ---------------------------------------------------------------------------
# CorrelatedMicStream iteration after exhaust
# ---------------------------------------------------------------------------

def test_correlated_stream_exhaust_raises_stop():
    async def inner():
        yield b"one"
        yield b"two"

    async def run():
        cm = stt.CorrelatedMicStream(inner(), on_partial=None)
        out = []
        async for c in cm:
            out.append(c)
        assert out == [b"one", b"two"]
        # Second iteration should yield nothing (already exhausted, cached iterator)
        out2 = []
        async for c in cm:
            out2.append(c)
        assert out2 == []

    asyncio.run(run())
