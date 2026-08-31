"""Config flow: manual controller URL + API key (docs/design/full-duplex-plan.md Q8/Q9).

No mDNS discovery — the fork's zeroconf attempt hit a real HA limitation
(service names capped at 15 characters) and manual entry is what it shipped
with. No UUID gateway identity either: the controller has no gateway_id
concept, so the config entry is keyed on the URL itself.
"""

from __future__ import annotations

from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant import config_entries

from .client import ControllerClient, ControllerError
from .const import CONF_API_KEY, CONF_URL, DOMAIN


def normalize_url(raw: str) -> str:
    """Validate and normalize a controller base URL, or raise ValueError."""
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.port is None:
        raise ValueError("invalid_url")
    return parsed._replace(path="", params="", query="", fragment="").geturl().rstrip("/")


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
