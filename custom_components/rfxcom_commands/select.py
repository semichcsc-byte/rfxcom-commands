"""Choosing the band the scanner listens on."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RFXCOMConfigEntry
from .const import BAND_AS_FOUND, DOMAIN
from .gateway import receiver_band, supported_bands


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFXCOMConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ScanBandSelect(entry)])


class ScanBandSelect(SelectEntity):
    """Which band to put the receiver on while the scanner runs.

    Only while it runs: the band is a property of the whole device, so leaving
    it moved would take the core integration's own devices off the air. It is
    put back when the scanner stops, the same way the protocol list is.
    """

    _attr_has_entity_name = True
    _attr_name = "Scan band"
    _attr_icon = "mdi:tune-vertical"

    def __init__(self, entry: RFXCOMConfigEntry) -> None:
        self._scanner = entry.runtime_data.scanner
        self._bands = supported_bands()
        self._current = BAND_AS_FOUND
        self._attr_unique_id = f"{entry.entry_id}-scan-band"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def options(self) -> list[str]:
        return [BAND_AS_FOUND, *sorted(self._bands)]

    @property
    def current_option(self) -> str:
        return self._current

    async def async_select_option(self, option: str) -> None:
        if self._scanner.running:
            raise HomeAssistantError(
                "Stop the scanner before changing band; the band is applied "
                "when it starts."
            )
        self._current = option
        self._scanner.band = None if option == BAND_AS_FOUND else self._bands[option]
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {
            "receiver_band": receiver_band(self.hass),
            "note": (
                "Whether an RFXtrx accepts a given band depends on its "
                "hardware, and it is not asked in advance. The band is "
                "restored when the scanner stops."
            ),
        }
