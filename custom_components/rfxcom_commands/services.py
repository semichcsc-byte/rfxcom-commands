"""The watch action: listen, and report everything the RFXCOM hears.

Raw mode cannot be left on -- while it is active the core integration decodes
nothing -- so this listens for a bounded window and puts the receiver back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError

from .capture import Capture
from .const import (
    ATTR_SECONDS,
    DEFAULT_WATCH_SECONDS,
    EVENT_RAW_COMMAND,
    MAX_WATCH_SECONDS,
    SERVICE_WATCH,
)
from .const import DOMAIN
from .gateway import GatewayError, RawListener

_LOGGER = logging.getLogger(__name__)

WATCH_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_SECONDS, default=DEFAULT_WATCH_SECONDS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_WATCH_SECONDS)
        )
    }
)

# Raw mode is a property of the device, so two listeners would fight over it.
_in_progress = asyncio.Lock()


async def _async_watch(call: ServiceCall) -> ServiceResponse:
    """Listen for a while and report every command heard."""
    hass = call.hass
    seconds = call.data[ATTR_SECONDS]

    if _in_progress.locked():
        raise HomeAssistantError(
            "Already listening. Raw mode belongs to the device, so only one "
            "capture can run at a time."
        )

    async with _in_progress:
        heard: dict[str, dict[str, Any]] = {}
        try:
            async with RawListener(hass) as listener:
                capture = Capture(listener)
                async for command in capture.commands(seconds):
                    record = heard.get(command.bits)
                    if record is None:
                        record = heard[command.bits] = {
                            "bits": command.bits,
                            "times": 0,
                            "frames": command.frames_seen,
                            "short_us": command.short,
                            "long_us": command.long,
                            "gap_us": command.gap,
                            "pulses": len(command.pulses),
                        }
                    record["times"] += 1
                    # Fired as it happens, so it can be watched live in
                    # Developer tools -> Events.
                    hass.bus.async_fire(EVENT_RAW_COMMAND, dict(record))
        except GatewayError as err:
            raise HomeAssistantError(str(err)) from err

    return {
        "heard": sorted(heard.values(), key=lambda r: r["times"], reverse=True),
        "packets": listener.packets_seen,
        "raw_packets": listener.raw_seen,
        "bursts_dropped": capture.bursts_dropped,
    }


def async_setup_services(hass: HomeAssistant) -> None:
    hass.services.async_register(
        DOMAIN,
        SERVICE_WATCH,
        _async_watch,
        schema=WATCH_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    hass.services.async_remove(DOMAIN, SERVICE_WATCH)
