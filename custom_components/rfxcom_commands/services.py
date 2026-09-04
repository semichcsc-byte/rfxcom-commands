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
    DOMAIN,
    EVENT_RAW_COMMAND,
    MAX_WATCH_SECONDS,
    SERVICE_WATCH,
)
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


async def async_listen(hass: HomeAssistant, seconds: int) -> dict[str, Any]:
    """Collect every command heard within the window.

    Each one is announced on the event bus as it arrives, so it can be watched
    live as well as read afterwards.
    """
    if _in_progress.locked():
        raise HomeAssistantError(
            "Already listening. Raw mode belongs to the device, so only one "
            "capture can run at a time."
        )

    heard: dict[str, dict[str, Any]] = {}
    async with _in_progress:
        try:
            async with RawListener(hass) as listener:
                capture = Capture(listener)
                async for command in capture.commands(seconds):
                    record = heard.get(command.bits)
                    if record is None:
                        record = heard[command.bits] = {
                            "bits": command.bits,
                            "hex": command.hex,
                            "bit_count": len(command.bits),
                            "inverted": command.inverted,
                            "heard": 0,
                            "repeats": command.frames_seen,
                            "encoding": command.encoding,
                            "jitter_pct": command.jitter_pct,
                            "short_us": command.short,
                            "long_us": command.long,
                            "gap_us": command.gap,
                            "frame_us": command.frame_us,
                            "burst_us": command.burst_us,
                            "pulses": len(command.pulses),
                        }
                    record["heard"] += 1
                    # Fired as it happens, so it can also be watched live.
                    hass.bus.async_fire(EVENT_RAW_COMMAND, dict(record))
        except GatewayError as err:
            raise HomeAssistantError(str(err)) from err

    return {
        "heard": sorted(heard.values(), key=lambda r: r["heard"], reverse=True),
        "packets": listener.packets_seen,
        "raw_packets": listener.raw_seen,
        "bursts_dropped": capture.bursts_dropped,
    }


def format_report(result: dict[str, Any]) -> str:
    """The same findings as prose, for a dialog with nowhere to put a table."""
    lines = [
        f"**{index}. `{record['hex']}`**\n"
        f"`{record['bits']}` ({record['bit_count']} bits)\n"
        f"sent {record['repeats']}x per press, heard {record['heard']}x, "
        f"{record['encoding'].upper()}, {record['jitter_pct']}% jitter\n"
        f"pulses {record['short_us']}/{record['long_us']} us, "
        f"gap {record['gap_us']} us, burst {record['burst_us'] / 1000:.0f} ms"
        for index, record in enumerate(result["heard"], start=1)
    ]

    if not lines:
        if result["packets"] == 0:
            lines = ["Nothing at all was received."]
        elif result["raw_packets"] == 0:
            lines = [
                f"The receiver handed over {result['packets']} transmissions but "
                "none as raw pulse data, so it never switched into raw mode."
            ]
        else:
            lines = [
                f"{result['raw_packets']} raw packets arrived but none of them "
                "formed a readable transmission."
            ]

    lines.append(
        f"_{result['packets']} packets from the receiver, "
        f"{result['raw_packets']} raw, {result['bursts_dropped']} bursts cut "
        "short by another transmission._"
    )
    return "\n\n".join(lines)


async def async_watch_report(hass: HomeAssistant, seconds: int) -> str:
    return format_report(await async_listen(hass, seconds))


async def _async_watch(call: ServiceCall) -> ServiceResponse:
    return await async_listen(call.hass, call.data[ATTR_SECONDS])


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
