"""Thorough tests for config_flow OptionsFlow (Phase 3)."""

from __future__ import annotations

import asyncio
import importlib
import types

import pytest

config_flow = importlib.import_module("custom_components.echo_voice_satellite.config_flow")
from custom_components.echo_voice_satellite.const import (
    CONF_CUSTOM_VOCABULARY,
    CONF_GEMINI_API_KEY,
    CONF_LANGUAGE_CODES,
    CONF_TRANSCRIPTION_MODE,
)


def _make_options_flow(monkeypatch=None, initial_options=None):
    """Create an OptionsFlow instance like HA does via async_get_options_flow."""
    entry = types.SimpleNamespace(options=dict(initial_options or {}))
    flow = config_flow.EchoVoiceSatelliteOptionsFlow()
    flow.config_entry = entry
    # Stub HA's async_create_entry / async_show_form to return inspectable dicts
    flow.async_create_entry = lambda title, data: {"type": "create_entry", "title": title, "data": data}
    flow.async_show_form = lambda step_id, data_schema, errors=None, **kw: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors or {},
    }
    return flow, entry


# ---------------------------------------------------------------------------
# _async_validate_gemini_key — direct unit tests
# ---------------------------------------------------------------------------

def test_validate_blank_key_is_noop():
    # Blank must not attempt network and must not raise
    asyncio.run(config_flow._async_validate_gemini_key(""))
    asyncio.run(config_flow._async_validate_gemini_key("   "))


def test_validate_valid_key_passes_via_fake():
    # Our conftest fake genai Client with valid key returns [] from models.list()
    asyncio.run(config_flow._async_validate_gemini_key("valid-key-123"))


def test_validate_invalid_key_raises_gemini_auth_error():
    with pytest.raises(config_flow.GeminiAuthError, match="invalid"):
        asyncio.run(config_flow._async_validate_gemini_key("invalid-key"))
    with pytest.raises(config_flow.GeminiAuthError):
        asyncio.run(config_flow._async_validate_gemini_key("my-invalid-token"))


# ---------------------------------------------------------------------------
# async_step_init — blank off switch, no validation call
# ---------------------------------------------------------------------------

def test_blank_api_key_is_off_switch_and_skips_validation(monkeypatch):
    flow, _ = _make_options_flow(initial_options={CONF_GEMINI_API_KEY: "old"})
    called = []
    orig = config_flow._async_validate_gemini_key

    async def spy(k):
        called.append(k)

    monkeypatch.setattr(config_flow, "_async_validate_gemini_key", spy)
    result = asyncio.run(
        flow.async_step_init(
            {
                CONF_GEMINI_API_KEY: "",
                CONF_CUSTOM_VOCABULARY: [],
                CONF_TRANSCRIPTION_MODE: "VERBATIM",
                CONF_LANGUAGE_CODES: "",
            }
        )
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_GEMINI_API_KEY] == ""
    assert called == [], "blank must not trigger validation"
    monkeypatch.setattr(config_flow, "_async_validate_gemini_key", orig)


def test_valid_key_with_all_fields_creates_entry(monkeypatch):
    flow, _ = _make_options_flow()
    monkeypatch.setattr(config_flow, "_async_validate_gemini_key", lambda k: asyncio.sleep(0))
    result = asyncio.run(
        flow.async_step_init(
            {
                CONF_GEMINI_API_KEY: "AIzaValid123",
                CONF_CUSTOM_VOCABULARY: ["hello", "world"],
                CONF_TRANSCRIPTION_MODE: "SMART",
                CONF_LANGUAGE_CODES: "en-US, es-ES",
            }
        )
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_GEMINI_API_KEY] == "AIzaValid123"
    assert result["data"][CONF_CUSTOM_VOCABULARY] == ["hello", "world"]
    assert result["data"][CONF_TRANSCRIPTION_MODE] == "SMART"
    assert result["data"][CONF_LANGUAGE_CODES] == "en-US, es-ES"


def test_invalid_key_shows_form_with_error_and_creates_nothing(monkeypatch):
    flow, _ = _make_options_flow()

    async def always_invalid(k):
        raise config_flow.GeminiAuthError("bad")

    monkeypatch.setattr(config_flow, "_async_validate_gemini_key", always_invalid)
    result = asyncio.run(flow.async_step_init({CONF_GEMINI_API_KEY: "invalid"}))
    assert result["type"] == "form"
    assert result["errors"]["base"] == "invalid_gemini_key"
    assert result["step_id"] == "init"


def test_no_input_shows_form_with_current_defaults():
    flow, _ = _make_options_flow(
        initial_options={
            CONF_GEMINI_API_KEY: "existing",
            CONF_TRANSCRIPTION_MODE: "SMART",
            CONF_CUSTOM_VOCABULARY: ["a"],
            CONF_LANGUAGE_CODES: "en-US",
        }
    )
    result = asyncio.run(flow.async_step_init(None))
    assert result["type"] == "form"
    assert result["step_id"] == "init"
    # Errors empty on first show
    assert result["errors"] == {}
    # Schema must contain all four keys (check via string repr of schema dict keys)
    schema = result["data_schema"]
    # Our vol fake returns the dict itself as schema
    assert CONF_GEMINI_API_KEY in schema
    assert CONF_CUSTOM_VOCABULARY in schema
    assert CONF_TRANSCRIPTION_MODE in schema
    assert CONF_LANGUAGE_CODES in schema


def test_empty_vocab_and_defaults_round_trip(monkeypatch):
    flow, _ = _make_options_flow()
    monkeypatch.setattr(config_flow, "_async_validate_gemini_key", lambda k: asyncio.sleep(0))
    result = asyncio.run(
        flow.async_step_init(
            {
                CONF_GEMINI_API_KEY: "valid",
                CONF_CUSTOM_VOCABULARY: [],
                CONF_TRANSCRIPTION_MODE: "VERBATIM",
                CONF_LANGUAGE_CODES: "",
            }
        )
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_CUSTOM_VOCABULARY] == []
    assert result["data"][CONF_TRANSCRIPTION_MODE] == "VERBATIM"
    assert result["data"][CONF_LANGUAGE_CODES] == ""


def test_vocab_hard_ceiling_1000_blocks_and_soft_100_passes(monkeypatch):
    flow, _ = _make_options_flow()
    monkeypatch.setattr(config_flow, "_async_validate_gemini_key", lambda k: asyncio.sleep(0))
    # 1001 must block
    result = asyncio.run(
        flow.async_step_init(
            {
                CONF_GEMINI_API_KEY: "valid",
                CONF_CUSTOM_VOCABULARY: ["w"] * 1001,
                CONF_TRANSCRIPTION_MODE: "VERBATIM",
                CONF_LANGUAGE_CODES: "",
            }
        )
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "too_many_terms"
    # 1000 must pass (hard ceiling)
    result = asyncio.run(
        flow.async_step_init(
            {
                CONF_GEMINI_API_KEY: "valid",
                CONF_CUSTOM_VOCABULARY: ["w"] * 1000,
                CONF_TRANSCRIPTION_MODE: "VERBATIM",
                CONF_LANGUAGE_CODES: "",
            }
        )
    )
    assert result["type"] == "create_entry"
    # 150 must pass (soft recommendation)
    result = asyncio.run(
        flow.async_step_init(
            {
                CONF_GEMINI_API_KEY: "valid",
                CONF_CUSTOM_VOCABULARY: ["w"] * 150,
                CONF_TRANSCRIPTION_MODE: "VERBATIM",
                CONF_LANGUAGE_CODES: "",
            }
        )
    )
    assert result["type"] == "create_entry"


def test_options_flow_does_not_trigger_reload(monkeypatch):
    # No entry.add_update_listener / async_reload pattern should be present
    import inspect

    src = inspect.getsource(config_flow.EchoVoiceSatelliteOptionsFlow)
    assert "add_update_listener" not in src
    assert "async_reload" not in src
    # Verify stt reads fresh per call, not cached at init (checked in stt tests)
    # Here just ensure options flow returns data that would be read fresh
    flow, _ = _make_options_flow()
    monkeypatch.setattr(config_flow, "_async_validate_gemini_key", lambda k: asyncio.sleep(0))
    result = asyncio.run(
        flow.async_step_init(
            {
                CONF_GEMINI_API_KEY: "new-key",
                CONF_CUSTOM_VOCABULARY: ["a,b"],  # contains comma, must not be split by flow
                CONF_TRANSCRIPTION_MODE: "SMART",
                CONF_LANGUAGE_CODES: "en-US, fr-FR",
            }
        )
    )
    # Custom vocab with comma must be preserved as single term
    assert result["data"][CONF_CUSTOM_VOCABULARY] == ["a,b"]


def test_async_get_options_flow_returns_correct_type():
    flow = config_flow.EchoVoiceSatelliteConfigFlow.async_get_options_flow(
        types.SimpleNamespace(options={})
    )
    assert isinstance(flow, config_flow.EchoVoiceSatelliteOptionsFlow)


def test_config_flow_text_selector_uses_multiple():
    # Ensure the schema actually uses TextSelector with multiple=True
    flow, _ = _make_options_flow()
    result = asyncio.run(flow.async_step_init(None))
    schema = result["data_schema"]
    # Find the TextSelector instance for custom vocab
    sel = schema[CONF_CUSTOM_VOCABULARY]
    # Our fake TextSelector stores config
    assert hasattr(sel, "config")
    assert sel.config.multiple is True


# ---------------------------------------------------------------------------
# Privacy — API key must never be logged
# ---------------------------------------------------------------------------

def test_options_flow_never_logs_api_key(caplog):
    import logging

    flow, _ = _make_options_flow()
    # Even valid key should not appear in logs
    with caplog.at_level(logging.DEBUG):
        asyncio.run(
            flow.async_step_init(
                {
                    CONF_GEMINI_API_KEY: "super-secret-key-123",
                    CONF_CUSTOM_VOCABULARY: [],
                    CONF_TRANSCRIPTION_MODE: "VERBATIM",
                    CONF_LANGUAGE_CODES: "",
                }
            )
        )
    assert "super-secret-key-123" not in caplog.text
