"""Talking to the RFXtrx that the core `rfxtrx` integration already owns.

The serial port allows a single reader, so this integration never opens its own
connection. Transmitting goes through the `rfxtrx.send` action. Receiving is the
awkward half: `RFXtrxTransport.parse` discards packet type 0x7F because
pyRFXtrx has no class for it, so the raw pulse data never reaches an event.
`_receive_packet` calls `self.parse(pkt)`, so an instance attribute shadows the
class method and lets us see every packet before pyRFXtrx discards it.

Raw reporting also has to be switched on, and the only switch is the receive
protocol list. Which protocol bit unlocks it is undocumented and differs between
firmware versions, so learning enables the lot and puts the previous selection
back afterwards.

The mode change is written straight to the open connection rather than by
rewriting the RFXtrx config entry. Reloading the entry would close and reopen
the serial port twice per capture, and closing it while the reader thread sits
in a blocking read hangs the reload.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import RFXTRX_DATA_OBJECT, RFXTRX_DOMAIN, RFXTRX_SERVICE_SEND

_LOGGER = logging.getLogger(__name__)

# Long enough for the firmware to apply a mode change and answer with a status.
MODE_SETTLE = 1.0

# Raw mode reports every RF transmission in earshot. A capture only ever needs
# the last few packets, so a bounded queue that drops the oldest is both
# sufficient and the only thing standing between a noisy band and an
# ever-growing backlog.
QUEUE_SIZE = 64


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
            "The RFXCOM integration is not set up, or is still starting. This "
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
    """Every receive protocol this build of pyRFXtrx knows about.

    The table hangs off the Status packet class, and its shape is (byte, bit)
    positions in the "set mode" command — the same thing get_recmode_tuple
    resolves a name to.
    """
    from RFXtrx import lowlevel  # noqa: PLC0415

    groups = getattr(getattr(lowlevel, "Status", None), "RECMODES", None)
    names = sorted({mode for group in groups or [] for mode in group or [] if mode})
    if not names:
        raise GatewayError("This version of pyRFXtrx exposes no protocol list")
    return names


def current_protocols(hass: HomeAssistant) -> list[str]:
    """What the device is currently decoding.

    Prefers what the connection was told to use, falling back to what the
    device reported at startup.
    """
    rfx = _rfx_object(hass)
    modes = list(getattr(rfx, "_modes", None) or [])
    if modes:
        return modes

    status = getattr(rfx, "_status", None)
    device = getattr(status, "device", None)
    return list(getattr(device, "devices", None) or [])


def _mode_packet(rfx: Any, protocols: list[str]) -> bytearray:
    """Build the 'set mode' command for a protocol selection.

    Mirrors `Connect.set_recmodes`, minus its blocking read: the reader thread
    owns the port and picks up the device's reply on its own.
    """
    from RFXtrx import lowlevel  # noqa: PLC0415

    status = getattr(rfx, "_status", None)
    device = getattr(status, "device", None)
    if device is None:
        raise GatewayError(
            "The RFXCOM has not reported its status yet; try again in a moment"
        )

    data = bytearray(
        [0x0D, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00,
         0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
    )
    data[5] = device.tranceiver_type
    data[6] = device.output_power
    for mode in protocols:
        byteno, bitno = lowlevel.get_recmode_tuple(mode)
        if byteno is None:
            continue  # not supported by this receiver; skip rather than fail
        data[7 + byteno] |= 1 << bitno
    return data


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
    """Collects raw packets while every receive protocol is enabled.

    Use as an async context manager: it opens the protocol list up, hooks the
    transport, and puts both back on the way out.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._rfx: Any = None
        self._previous: list[str] | None = None
        self._transport: Any = None
        self._original_parse: Callable[[Any], Any] | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._loop = asyncio.get_running_loop()

    async def __aenter__(self) -> RawListener:
        find_entry(self._hass)  # fails with a clear message when not set up
        self._rfx = _rfx_object(self._hass)

        previous = current_protocols(self._hass)
        if not previous:
            raise GatewayError(
                "Cannot tell which protocols the RFXCOM is decoding, so they "
                "could not be restored afterwards. Set the protocol list in the "
                "RFXCOM integration options, then try again."
            )
        self._previous = previous

        self._install_hook()
        try:
            await self._async_set_protocols(supported_protocols())
        except Exception:
            self._remove_hook()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            if self._previous is not None:
                # Shielded: when the capture is cancelled the restore still has
                # to finish, or the RFXCOM is left decoding everything.
                await asyncio.shield(
                    self._async_set_protocols(self._previous)
                )
        except asyncio.CancelledError:
            pass  # the shielded restore carries on without us
        except Exception:  # noqa: BLE001 - never mask the original failure
            _LOGGER.exception("Could not restore the RFXCOM protocol list")
        finally:
            self._remove_hook()

    async def _async_set_protocols(self, protocols: list[str]) -> None:
        packet = _mode_packet(self._rfx, protocols)
        transport = self._rfx.transport
        try:
            await self._hass.async_add_executor_job(transport.send, packet)
        except Exception as err:  # noqa: BLE001 - surfaced to the user
            raise GatewayError(f"Could not change the RFXCOM mode: {err}") from err
        await asyncio.sleep(MODE_SETTLE)

    def _install_hook(self) -> None:
        transport = self._rfx.transport
        original = transport.parse

        def _queue(packet: bytes) -> None:
            if self._queue.full():
                self._queue.get_nowait()  # drop the oldest; a capture wants the newest
            self._queue.put_nowait(packet)

        def _parse(data: Any) -> Any:
            try:
                packet = bytes(data)
                if len(packet) >= 6 and packet[1] == 0x7F:
                    self._loop.call_soon_threadsafe(_queue, packet)
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
