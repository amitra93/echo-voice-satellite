import asyncio
import importlib

import pytest


config_flow = importlib.import_module("custom_components.echo_voice_satellite.config_flow")
from custom_components.echo_voice_satellite.client import ControllerError  # noqa: E402
from custom_components.echo_voice_satellite.const import CONF_API_KEY, CONF_URL  # noqa: E402


def test_normalize_url_accepts_scheme_host_and_port():
    assert config_flow.normalize_url("http://192.168.1.50:8768/") == "http://192.168.1.50:8768"
    assert config_flow.normalize_url("https://controller.local:8768") == "https://controller.local:8768"


@pytest.mark.parametrize("raw", ["not-a-url", "ftp://host:1", "http://", "http://host"])
def test_normalize_url_rejects_missing_scheme_host_or_port(raw):
    with pytest.raises(ValueError):
        config_flow.normalize_url(raw)


# ── async_step_user — the flow's own branching logic, isolated from HA's
# unique-id/entry-creation machinery (already tested by HA core itself). The
# flow instance is object.__new__'d, per FlowHandler's own class-level
# defaults (hass=None, context={}), and async_set_unique_id /
# _abort_if_unique_id_configured / async_create_entry are stubbed on the
# instance so what's under test is: does this flow validate the URL, build a
# ControllerClient with the right args, probe it, and close it — not HA's
# own entry bookkeeping. ────────────────────────────────────────────────────

class _FakeControllerClient:
    instances = []

    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key
        self.closed = False
        self.get_devices_error = None
        type(self).instances.append(self)

    async def async_get_devices(self):
        if self.get_devices_error is not None:
            raise self.get_devices_error
        return []

    async def async_close(self):
        self.closed = True


def _make_flow(monkeypatch, client_error=None):
    _FakeControllerClient.instances = []

    def factory(base_url, api_key):
        client = _FakeControllerClient(base_url, api_key)
        client.get_devices_error = client_error
        return client

    monkeypatch.setattr(config_flow, "ControllerClient", factory)

    flow = object.__new__(config_flow.EchoVoiceSatelliteConfigFlow)
    flow.context = {}

    async def fake_set_unique_id(unique_id, **kwargs):
        # unique_id is a read-only property backed by context["unique_id"]
        # (config_entries.ConfigFlow.unique_id) — write through the same
        # place the real async_set_unique_id would.
        flow.context["unique_id"] = unique_id

    flow.async_set_unique_id = fake_set_unique_id
    flow._abort_if_unique_id_configured = lambda: None
    created = {}

    def fake_create_entry(*, title, data):
        created["title"] = title
        created["data"] = data
        return {"type": "create_entry", "title": title, "data": data}

    flow.async_create_entry = fake_create_entry
    return flow, created


def test_successful_connection_creates_entry_with_normalized_url_and_key(monkeypatch):
    flow, created = _make_flow(monkeypatch)

    result = asyncio.run(flow.async_step_user({
        CONF_URL: "http://192.168.1.50:8768/", CONF_API_KEY: "em_secret",
    }))

    assert created["data"] == {CONF_URL: "http://192.168.1.50:8768", CONF_API_KEY: "em_secret"}
    assert flow.context["unique_id"] == "http://192.168.1.50:8768"
    assert result["type"] == "create_entry"
    client = _FakeControllerClient.instances[0]
    assert client.closed is True  # the probe client is always closed, success or not


def test_invalid_url_shows_form_with_error_and_never_builds_a_client(monkeypatch):
    flow, _ = _make_flow(monkeypatch)

    result = asyncio.run(flow.async_step_user({
        CONF_URL: "not-a-url", CONF_API_KEY: "em_secret",
    }))

    assert result["errors"]["base"] == "invalid_url"
    assert _FakeControllerClient.instances == []


def test_unreachable_controller_shows_cannot_connect_and_still_closes_client(monkeypatch):
    flow, created = _make_flow(monkeypatch, client_error=ControllerError("controller_unreachable"))

    result = asyncio.run(flow.async_step_user({
        CONF_URL: "http://192.168.1.50:8768", CONF_API_KEY: "em_wrong",
    }))

    assert result["errors"]["base"] == "cannot_connect"
    assert created == {}  # no entry created
    assert _FakeControllerClient.instances[0].closed is True


def test_no_input_shows_the_initial_form_without_probing(monkeypatch):
    flow, _ = _make_flow(monkeypatch)

    result = asyncio.run(flow.async_step_user(None))

    assert result["step_id"] == "user"
    assert _FakeControllerClient.instances == []
