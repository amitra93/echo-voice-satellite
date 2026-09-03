"""Config flow: manual controller URL + API key (docs/design/full-duplex-plan.md Q8/Q9).

No mDNS discovery — the fork's zeroconf attempt hit a real HA limitation
(service names capped at 15 characters) and manual entry is what it shipped
with. No UUID gateway identity either: the controller has no gateway_id
concept, so the config entry is keyed on the URL itself.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .client import ControllerClient, ControllerError
from .const import (
    CONF_API_KEY,
    CONF_CUSTOM_VOCABULARY,
    CONF_GEMINI_API_KEY,
    CONF_LANGUAGE_CODES,
    CONF_TRANSCRIPTION_MODE,
    CONF_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def normalize_url(raw: str) -> str:
    """Validate and normalize a controller base URL, or raise ValueError."""
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port is None:
        raise ValueError("invalid_url")
    return parsed._replace(path="", params="", query="", fragment="").geturl().rstrip("/")


class GeminiAuthError(Exception):
    """Raised when a Gemini API key is rejected by Google."""


async def _async_validate_gemini_key(api_key: str) -> None:
    """Lightweight validation of a Gemini API key.

    Mirrors ``async_step_user``'s ``client.async_get_devices()`` probe: do a
    minimal authenticated call before accepting the key, so a typo is reported
    at save time rather than on the next voice turn. The cheapest correct call
    is a models-list; the exact SDK surface for that is an implementation
    detail confirmed at call time against the installed ``google-genai`` — the
    doc's Q5 resolution covers the Live transcription call shape, not this
    probe. A missing SDK or any auth-shaped failure surfaces as
    ``GeminiAuthError`` so the form can show ``invalid_gemini_key``.
    """
    if not api_key or not api_key.strip():
        return
    try:
        from google import genai  # type: ignore[import-untyped]

        client = genai.Client(api_key=api_key)
        # Prefer the async surface if present; fall back to sync.
        aio_models = getattr(getattr(client, "aio", None), "models", None)
        sync_models = getattr(client, "models", None)
        target = aio_models if aio_models is not None else sync_models
        if target is not None and hasattr(target, "list"):
            result = target.list()
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                await result  # type: ignore[no-redef]
        # If the SDK exposes no list() at all, construction succeeding is
        # treated as format-acceptance; a real auth failure will still be
        # caught on the next Live connect, exactly as the doc's "blank or
        # incorrect key fails loudly at Gemini's own auth step" path.
    except GeminiAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 — any failure here is "invalid key" for the form
        raise GeminiAuthError(str(exc)) from exc


class EchoVoiceSatelliteOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Gemini STT settings.

    This is the first OptionsFlow this integration has needed — the required
    ``async_step_user`` remains the only install-time step, while these four
    Gemini knobs live in options, per docs/design/hacs-stt-plan.md "Decisions
    locked in" and "config_flow.py (changed)".

    Blank API key is a valid, supported off state (``vol.Optional`` not
    ``vol.Required``) — clearing the field and saving is the reversible off
    switch. Validation only runs when a non-empty key is supplied, so blank
    never attempts a network call. No config-entry reload or update listener
    is needed: ``stt.py`` reads ``self._entry.options`` fresh per turn.
    """

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        # ``self.config_entry`` is set by HA when the flow is instantiated
        # via ``async_get_options_flow``; tests stub it directly.
        current = {}
        try:
            current = dict(getattr(self, "config_entry", None).options or {})  # type: ignore[attr-defined]
        except Exception:
            current = {}
        if user_input is not None:
            api_key = user_input.get(CONF_GEMINI_API_KEY, "")
            if api_key:
                try:
                    await _async_validate_gemini_key(api_key)
                except GeminiAuthError:
                    errors["base"] = "invalid_gemini_key"
            # Hard ceiling: 1,000 terms (Google's API limit). 100 is a soft
            # quality recommendation and must not block a save — only the hard
            # limit is enforced here.
            vocab = user_input.get(CONF_CUSTOM_VOCABULARY, [])
            if isinstance(vocab, list) and len(vocab) > 1000:
                errors["base"] = "too_many_terms"
            if not errors:
                return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_GEMINI_API_KEY,
                        default=current.get(CONF_GEMINI_API_KEY, ""),
                    ): str,
                    vol.Optional(
                        CONF_CUSTOM_VOCABULARY,
                        default=current.get(CONF_CUSTOM_VOCABULARY, []),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(multiple=True)
                    ),
                    vol.Optional(
                        CONF_TRANSCRIPTION_MODE,
                        default=current.get(CONF_TRANSCRIPTION_MODE, "VERBATIM"),
                    ): vol.In(["VERBATIM", "SMART"]),
                    vol.Optional(
                        CONF_LANGUAGE_CODES,
                        default=current.get(CONF_LANGUAGE_CODES, ""),
                    ): str,
                }
            ),
            errors=errors,
        )


class EchoVoiceSatelliteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input:
            try:
                base_url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                client = ControllerClient(base_url, user_input[CONF_API_KEY])
                try:
                    await client.async_get_devices()
                except (aiohttp.ClientError, OSError, ControllerError):
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(base_url)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="EchoMuse Controller",
                        data={CONF_URL: base_url, CONF_API_KEY: user_input[CONF_API_KEY]},
                    )
                finally:
                    await client.async_close()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_URL, default=(user_input or {}).get(CONF_URL, "http://")): str,
                vol.Required(CONF_API_KEY, default=""): str,
            }),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return EchoVoiceSatelliteOptionsFlow()
