"""Button entities, one per learned command."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RFXCOMConfigEntry, RFXCOMRuntime
from .const import CONF_AREA_ID, CONF_EVENTS, DOMAIN
from .gateway import GatewayError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFXCOMConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    _prune_removed_commands(hass, entry)

    # Deliberately not passing config_subentry_id: every button hangs off the one
    # physical gateway, and a device can only belong to a single subentry, so
    # tagging them would make each new command steal the device from the last.
    async_add_entities(
        RFXCOMCommandButton(entry, subentry) for subentry in entry.subentries.values()
    )


def _prune_removed_commands(hass: HomeAssistant, entry: RFXCOMConfigEntry) -> None:
    """Drop registry entries for commands that no longer exist.

    Home Assistant cleans up entities owned by a subentry automatically; ours are
    not, so deleting a command would otherwise leave its button behind.
    """
    registry = er.async_get(hass)
    live = set(entry.subentries)
    for record in er.async_entries_for_config_entry(registry, entry.entry_id):
        if record.unique_id not in live:
            _LOGGER.debug("Removing %s, its command is gone", record.entity_id)
            registry.async_remove(record.entity_id)


class RFXCOMCommandButton(ButtonEntity):
    """Replays one learned command."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:remote"

    def __init__(self, entry: RFXCOMConfigEntry, subentry: ConfigSubentry) -> None:
        self._runtime: RFXCOMRuntime = entry.runtime_data
        self._events: list[str] = list(subentry.data[CONF_EVENTS])
        self._area_id: str | None = subentry.data.get(CONF_AREA_ID)
        self._attr_name = subentry.title
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        """Honour the area chosen while learning, which the device does not supply."""
        await super().async_added_to_hass()
        if not self._area_id:
            return
        registry = er.async_get(self.hass)
        entry = registry.async_get(self.entity_id)
        if entry is not None and entry.area_id is None:
            registry.async_update_entity(self.entity_id, area_id=self._area_id)

    async def async_press(self) -> None:
        try:
            await self._runtime.send(self._events)
        except GatewayError as err:
            raise HomeAssistantError(str(err)) from err
