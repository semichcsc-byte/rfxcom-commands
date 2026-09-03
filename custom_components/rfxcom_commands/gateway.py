"""Talking to the RFXtrx that the core `rfxtrx` integration already owns.

The serial port allows a single reader, so this integration never opens its own
connection. Transmitting goes through the `rfxtrx.send` action. Receiving is the
awkward half: `RFXtrxTransport.parse` discards packet type 0x7F because
pyRFXtrx has no class for it, so the raw pulse data never reaches an event.
`_receive_packet` calls `self.parse(pkt)`, so an instance attribute shadows the
class method and lets us see every packet before pyRFXtrx drops it.

Raw reporting also has to be switched on, and the only switch is the receive
protocol list. Which protocol bit unlocks it is undocumented and differs between
firmware versions, so learning enables the lot and puts the previous selection
back afterwards.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import (
    CONF_PROTOCOLS,
    RFXTRX_DATA_OBJECT,
    RFXTRX_DOMAIN,
    RFXTRX_SERVICE_SEND,
)

_LOGGER = logging.getLogger(__name__)

# Used when pyRFXtrx does not expose its mode table, which has happened across
# releases. Superset is harmless: unknown names are rejected before we send.
_FALLBACK_PROTOCOLS = [
    "ac", "adlightwave", "aeblyss", "arc", "ati", "blindst0", "blindst1234",
    "byronsx", "fineoffset", "fs20", "hideki", "homeconfort", "homeeasy",
    "imagintronix", "keeloq", "lacrosse", "lighting4", "meiantech", "mertik",
    "oregon", "proguard", "rsl", "rubicson", "undecoded", "visonic", "x10",
]


class GatewayError(Exception):
    """Raised when the RFXtrx cannot be reached or driven."""


def find_entry(hass: HomeAssistant) -> ConfigEntry:
    """The loaded RFXtrx config entry."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(RFXTRX_DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not entries:
        raise GatewayError(
            "The RFXCOM integration is not set up. Add it first: this "
            "integration borrows its connection rather than opening a second one."
        )
    return entries[0]


def _rfx_object(hass: HomeAssistant) -> Any:
    """The live pyRFXtrx `Connect` owned by the core integration."""
    try:
        rfx = hass.data[RFXTRX_DOMAIN][RFXTRX_DATA_OBJECT]
    except (KeyError, TypeError) as err:
        raise GatewayError("The RFXCOM connection is not available") from err
    if rfx is None:
        raise GatewayError("The RFXCOM connection is not available")
    return rfx


def supported_protocols() -> list[str]:
    """Every receive protocol this build of pyRFXtrx knows about."""
    try:
        from RFXtrx import lowlevel  # noqa: PLC0415

        modes = getattr(lowlevel, "RECMODES", None)
        if modes:
            names = sorted({m for group in modes for m in group if m})
            if names:
                return names
    except Exception:  # noqa: BLE001 - fall back rather than fail learning
        _LOGGER.debug("Could not read protocol list from pyRFXtrx", exc_info=True)
    return list(_FALLBACK_PROTOCOLS)


async def async_send(hass: HomeAssistant, events: list[str]) -> None:
    """Transmit the packets of one command, in order.

    Multi-packet commands must arrive as a run: the device buffers them and
    transmits when the packet flagged last shows up.
    """
    for event in events:
        await hass.services.async_call(
            RFXTRX_DOMAIN, RFXTRX_SERVICE_SEND, {"event": event}, blocking=True
        )


class RawListener:
    """Collects raw packets while the receive protocols are opened up.

    Use as an async context manager: it enables every protocol, reloads the
    RFXtrx entry so the new mode takes effect, hooks the transport, and puts
    everything back on the way out.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._entry: ConfigEntry | None = None
        self._previous: list[str] | None = None
        self._transport: Any = None
        self._original_parse: Callable[[Any], Any] | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()

    async def __aenter__(self) -> RawListener:
        self._entry = find_entry(self._hass)
        self._previous = list(self._entry.options.get(CONF_PROTOCOLS) or [])
        await self._async_set_protocols(supported_protocols())
        self._install_hook()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._remove_hook()
        if self._previous is not None:
            try:
                await self._async_set_protocols(self._previous)
            except Exception:  # noqa: BLE001 - never mask the original failure
                _LOGGER.exception("Could not restore the RFXCOM protocol list")

    async def _async_set_protocols(self, protocols: list[str]) -> None:
        """Rewrite the protocol list and wait for the reload to finish."""
        assert self._entry is not None
        if list(self._entry.options.get(CONF_PROTOCOLS) or []) == protocols:
            return
        self._hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_PROTOCOLS: protocols}
        )
        await self._hass.config_entries.async_reload(self._entry.entry_id)
        # The reload replaces the connection object, so re-resolve the entry.
        self._entry = find_entry(self._hass)

    def _install_hook(self) -> None:
        transport = _rfx_object(self._hass).transport
        original = transport.parse

        def _parse(data: Any) -> Any:
            try:
                packet = bytes(data)
                if len(packet) >= 6 and packet[1] == 0x7F:
                    self._loop.call_soon_threadsafe(self._queue.put_nowait, packet)
            except Exception:  # noqa: BLE001 - a bad capture must not kill the reader
                _LOGGER.debug("Ignoring malformed packet", exc_info=True)
            return original(data)

        transport.parse = _parse
        self._transport = transport
        self._original_parse = original

    def _remove_hook(self) -> None:
        if self._transport is None:
            return
        # Delete rather than restore, so the class method takes over again and
        # a stale closure cannot outlive this capture.
        try:
            del self._transport.parse
        except AttributeError:
            self._transport.parse = self._original_parse
        self._transport = None
        self._original_parse = None

    async def next_packet(self, timeout: float) -> bytes | None:
        """Wait for one raw packet, or None if nothing arrives in time."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None
