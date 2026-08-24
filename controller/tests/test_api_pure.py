import sys
import types

from aiohttp.test_utils import make_mocked_request

sys.modules.setdefault("websockets", types.ModuleType("websockets"))

import em_api


def test_slug_produces_safe_filenames():
    assert em_api._slug("Bedroom - Sam!") == "bedroom-sam"
    assert em_api._slug("  ") == "device"
    assert em_api._slug("Café") == "caf"


def test_redact_turns_preserves_admin_rows_and_removes_transcripts():
    turns = [{"id": 1, "stt_text": "private", "outcome": "ok"}]
    assert em_api._redact_turns_for(turns, {"role": "admin"}) is turns
    assert em_api._redact_turns_for(turns, {"role": "readonly"}) == [
        {"id": 1, "outcome": "ok"}
    ]


def test_ingress_base_is_escaped_and_defaults_to_root():
    page = "<html><head></head><body></body></html>"
    root = make_mocked_request("GET", "/")
    assert '<base href="/">' in em_api._with_ingress_base(page, root)
    ingress = make_mocked_request(
        "GET", "/", headers={"X-Ingress-Path": '/api/ingress/<token>/'})
    rendered = em_api._with_ingress_base(page, ingress)
    assert '<base href="/api/ingress/&lt;token&gt;/">' in rendered


def test_extract_binary_version_and_dropped_keys():
    assert em_api._extract_binary_version(b"prefix 20260823-1234-dev suffix") == "20260823-1234-dev"
    assert em_api._extract_binary_version(b"no version") is None
    assert em_api._dropped_keys({"one": 1}, {"one": 1, "two": 2}) == ["two"]
    assert em_api._dropped_keys({}, {"z": 1, "a": 2}) == ["a", "z"]
