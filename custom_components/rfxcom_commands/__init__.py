"""The RFXCOM Commands integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_KIND, DOMAIN, KIND_BUTTON
from .gateway import GatewayError, async_send, find_entry

PLATFORMS = [Platform.BUTTON, Platform.SWITCH]

type RFXCOMConfigEntry = ConfigEntry[RFXCOMRuntime]


@dataclass
class RFXCOMRuntime:
    """What the platforms need at runtime."""

    hass: HomeAssistant

    async def send(self, events: list[str]) -> None:
        """Transmit a learned command."""
        await async_send(self.hass, events)


async def async_setup_entry(hass: HomeAssistant, entry: RFXCOMConfigEntry) -> bool:
    try:
        find_entry(hass)
    except GatewayError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = RFXCOMRuntime(hass=hass)

    # Owned by the config entry rather than by a command, so that adding a
    # command cannot reassign it and orphan the previous command's button.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="RFXCOM",
        model="RFXtrx raw RF",
        # The device page has no hook for adding a subentry, so link to the page
        # that does have the "Add command" button.
        configuration_url=(
            f"homeassistant://config/integrations/integration/{DOMAIN}"
            f"#config_entry={entry.entry_id}"
        ),
    )

    _prune_stale_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


def _prune_stale_entities(hass: HomeAssistant, entry: RFXCOMConfigEntry) -> None:
    """Drop registry entries no command owns any more.

    Home Assistant cleans up entities owned by a subentry automatically; ours
    are not, so a deleted command would leave its entity behind. A command that
    changed from a button to a switch leaves one behind too, under the old
    domain.
    """
    wanted = {
        subentry.subentry_id: subentry.data.get(CONF_KIND, KIND_BUTTON)
        for subentry in entry.subentries.values()
    }
    registry = er.async_get(hass)
    for record in er.async_entries_for_config_entry(registry, entry.entry_id):
        if wanted.get(record.unique_id) != record.domain:
            registry.async_remove(record.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: RFXCOMConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: RFXCOMConfigEntry) -> None:
    """Adding or removing a command changes which entities should exist."""
    await hass.config_entries.async_reload(entry.entry_id)
