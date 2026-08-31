"""Select the Assist pipeline used by one satellite."""

from __future__ import annotations

from homeassistant.components import assist_pipeline
from homeassistant.components.select import SelectEntity

from .const import DOMAIN
from .entities import EchoCoordinatorEntity, add_dynamic_entities


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    entry.async_on_unload(add_dynamic_entities(
        coordinator, async_add_entities,
        lambda record: [
            EchoAssistPipelineSelect(hass, coordinator, entry, record["device_id"]),
        ],
    ))


class EchoAssistPipelineSelect(EchoCoordinatorEntity, SelectEntity):
    _attr_name = "Assist pipeline"

    def __init__(self, hass, coordinator, entry, device_id: str):
        super().__init__(coordinator, device_id)
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{device_id}_assist_pipeline"
        self._pipeline_ids: dict[str, str] = {}

    @property
    def options(self) -> list[str]:
        pipelines = assist_pipeline.async_get_pipelines(self.hass)
        self._pipeline_ids = {pipeline.name: pipeline.id for pipeline in pipelines}
        return list(self._pipeline_ids)

    @property
    def current_option(self) -> str | None:
        self.options
        selected_id = self.entry.options.get("assist_pipeline_ids", {}).get(self.device_id)
        if not selected_id:
            return None
        for name, pipeline_id in self._pipeline_ids.items():
            if pipeline_id == selected_id:
                return name
        return None

    async def async_select_option(self, option: str) -> None:
        pipeline_id = self._pipeline_ids.get(option)
        if pipeline_id is None:
            raise ValueError(f"Unknown Assist pipeline: {option}")
        selected = {
            **self.entry.options.get("assist_pipeline_ids", {}),
            self.device_id: pipeline_id,
        }
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, "assist_pipeline_ids": selected},
        )
        self.async_write_ha_state()
