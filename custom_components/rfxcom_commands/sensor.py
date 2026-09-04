"""The sensors that show what the scanner is hearing, as it hears it."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RFXCOMConfigEntry
from .const import DOMAIN
from .scanner import Scanner


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFXCOMConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([LastCodeSensor(entry), LastRepeatsSensor(entry)])


class ScannerSensor(SensorEntity):
    """Redrawn whenever the scanner hears something."""

    _attr_has_entity_name = True
    _key: str

    def __init__(self, entry: RFXCOMConfigEntry) -> None:
        self._scanner: Scanner = entry.runtime_data.scanner
        self._attr_unique_id = f"{entry.entry_id}-{self._key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._scanner.async_subscribe(self.async_write_ha_state))


class LastCodeSensor(ScannerSensor):
    """The last command the scanner decoded."""

    _attr_name = "Last code"
    _attr_icon = "mdi:radio-tower"
    _key = "last-code"

    @property
    def native_value(self) -> str | None:
        last = self._scanner.last
        return last["bits"] if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        scanner = self._scanner
        attributes: dict[str, Any] = {
            "scanning": scanner.running,
            "recent": scanner.recent,
            "packets": scanner.packets,
            "raw_packets": scanner.raw_packets,
            "bursts_dropped": scanner.bursts_dropped,
        }
        if scanner.last:
            attributes |= {
                key: value for key, value in scanner.last.items() if key != "bits"
            }
        return attributes


class LastRepeatsSensor(ScannerSensor):
    """How many times the remote sent that code, which is how it is replayed."""

    _attr_name = "Last code repeats"
    _attr_icon = "mdi:repeat"
    _key = "last-repeats"

    @property
    def native_value(self) -> int | None:
        last = self._scanner.last
        return last["repeats"] if last else None
