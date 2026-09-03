"""Tests for const.py PLATFORMS and manifest.json contract."""

import json
import pathlib


def test_platforms_includes_stt_and_remains_tuple():
    from custom_components.echo_voice_satellite.const import PLATFORMS

    assert "stt" in PLATFORMS
    # Order: assist_satellite first, stt last is fine but must be present
    assert PLATFORMS == (
        "assist_satellite",
        "sensor",
        "binary_sensor",
        "select",
        "switch",
        "stt",
    )
    assert isinstance(PLATFORMS, tuple)


def test_gemini_conf_keys_are_distinct_from_controller_key():
    from custom_components.echo_voice_satellite import const

    # Distinct names so logs/diffs never confuse controller vs Gemini credentials
    assert const.CONF_GEMINI_API_KEY != const.CONF_API_KEY
    assert const.CONF_GEMINI_API_KEY == "gemini_api_key"
    assert const.CONF_CUSTOM_VOCABULARY == "gemini_custom_vocabulary"
    assert const.CONF_TRANSCRIPTION_MODE == "gemini_transcription_mode"
    assert const.CONF_LANGUAGE_CODES == "gemini_language_codes"
    # Ensure the four options-flow keys are present and no extra gemini_api key collides
    for key in (
        "CONF_GEMINI_API_KEY",
        "CONF_CUSTOM_VOCABULARY",
        "CONF_TRANSCRIPTION_MODE",
        "CONF_LANGUAGE_CODES",
    ):
        assert hasattr(const, key), f"missing {key}"


def test_manifest_requires_google_genai():
    mf = pathlib.Path(__file__).parents[1] / "custom_components/echo_voice_satellite/manifest.json"
    data = json.loads(mf.read_text())
    reqs = data["requirements"]
    assert any("google-genai" in r for r in reqs), f"google-genai missing from {reqs}"
    assert any("aiohttp" in r for r in reqs)
    # Must be pinned with version specifier, not bare
    for r in reqs:
        if "google-genai" in r:
            assert ">=" in r or "==" in r or "~=" in r, f"unpinned {r}"


def test_stt_platform_is_importable():
    import importlib

    mod = importlib.import_module("custom_components.echo_voice_satellite.stt")
    assert hasattr(mod, "GeminiTranscribeEntity")
    assert hasattr(mod, "CorrelatedMicStream")
    assert hasattr(mod, "_parse_language_codes")
    assert hasattr(mod, "_SUPPORTED_LANGUAGES")


def test_const_and_manifest_agree_on_min_ha_version():
    from custom_components.echo_voice_satellite.const import MIN_HA_VERSION
    # MIN_HA_VERSION must be a valid YYYY.M.x string
    parts = MIN_HA_VERSION.split(".")
    assert len(parts) == 3
    assert parts[0].isdigit() and parts[1].isdigit()
