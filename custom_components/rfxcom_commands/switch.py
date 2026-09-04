"""Switch entities for commands whose button toggles something.

A one-way remote gives no feedback, so the state here is what we last asked
for, not what the light is actually doing. That is what assumed_state is for.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RFXCOMConfigEntry, RFXCOMRuntime
from .const import CONF_AREA_ID, CONF_EVENTS, CONF_KIND, DOMAIN, KIND_SWITCH
from .gateway import GatewayError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RFXCOMConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            ScannerSwitch(entry),
            *(
                RFXCOMCommandSwitch(entry, subentry)
                for subentry in entry.subentries.values()
                if subentry.data.get(CONF_KIND) == KIND_SWITCH
            ),
        ]
    )


class ScannerSwitch(SwitchEntity):
    """Holds the receiver in raw mode so codes can be watched as they arrive."""

    _attr_has_entity_name = True
    _attr_name = "Scanner"
    _attr_icon = "mdi:access-point"

    def __init__(self, entry: RFXCOMConfigEntry) -> None:
        self._scanner = entry.runtime_data.scanner
        self._attr_unique_id = f"{entry.entry_id}-scanner"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._scanner.async_subscribe(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._scanner.running

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._scanner.async_start()
        if self._scanner.error:
            raise HomeAssistantError(self._scanner.error)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._scanner.async_stop()


class RFXCOMCommandSwitch(SwitchEntity, RestoreEntity):
    """Replays one learned toggle command and remembers what it asked for."""

    _attr_has_entity_name = True
    _attr_assumed_state = True
    _attr_icon = "mdi:remote"

    def __init__(self, entry: RFXCOMConfigEntry, subentry: ConfigSubentry) -> None:
        self._runtime: RFXCOMRuntime = entry.runtime_data
        self._events: list[str] = list(subentry.data[CONF_EVENTS])
        self._area_id: str | None = subentry.data.get(CONF_AREA_ID)
        self._attr_name = subentry.title
        self._attr_unique_id = subentry.subentry_id
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"

        # Honour the area chosen while learning, which the device does not supply.
        if not self._area_id:
            return
        registry = er.async_get(self.hass)
        record = registry.async_get(self.entity_id)
        if record is not None and record.area_id is None:
            registry.async_update_entity(self.entity_id, area_id=self._area_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._press(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._press(False)

    async def _press(self, target: bool) -> None:
        """Send the one code the remote has, then assume it worked.

        Always sends, even when the state already matches. With no feedback the
        two buttons are requests, not a target state, and asking again is the
        only way back when a transmission was lost -- refusing would leave the
        switch and the light disagreeing with no way to correct it.
        """
        try:
            await self._runtime.send(self._events)
        except GatewayError as err:
            raise HomeAssistantError(str(err)) from err
        self._attr_is_on = target
        self.async_write_ha_state()
