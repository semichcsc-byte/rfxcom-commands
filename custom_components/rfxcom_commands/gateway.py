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
back afterwards. For the length of the capture the hook then passes nothing but
raw packets through: with every protocol enabled, anything else is a
transmission the configured list deliberately excludes.

The mode change is written straight to the open connection rather than by
rewriting the RFXtrx config entry. Reloading the entry would close and reopen
the serial port twice per capture, and closing it while the reader thread sits
in a blocking read hangs the reload.

Every operation on the port is bounded. The library writes to pyserial without
a write timeout and, from 0.33, waits seconds for an acknowledgement under its
own lock, so an unbounded wait here would be inherited by whatever asked for it
-- an entity, a config flow, or Home Assistant shutting down.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from queue import Empty, Queue
from threading import Lock
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import DOMAIN, RFXTRX_DATA_OBJECT, RFXTRX_DOMAIN, RFXTRX_SERVICE_SEND

_LOGGER = logging.getLogger(__name__)

# Long enough for the firmware to apply a mode change and answer with a status.
MODE_SETTLE = 1.0

# Nothing is allowed to wait on the serial port forever. pyserial is opened
# without a write timeout, so a port that stops draining blocks its thread
# indefinitely, and newer pyRFXtrx waits seconds for an acknowledgement under
# its own lock. Waiting that out unbounded would hold the port, block the
# config entry from unloading and stop Home Assistant from shutting down.
WRITE_TIMEOUT = 10.0

# A command can be up to four packets, each a separate write, so it gets a
# larger budget than a single mode change.
SEND_TIMEOUT = 20.0

# Cleanup restores the protocol list, which is one write plus the settle, and
# may first have to wait for a command that is still on the wire.
CLOSE_TIMEOUT = WRITE_TIMEOUT * 2 + MODE_SETTLE

# Bound the buffer on the reader thread, before any event-loop work is queued.
# Overflow aborts capture rather than assembling a signal from missing packets.
QUEUE_SIZE = 64
QUEUE_POLL_INTERVAL = 0.01


class GatewayError(Exception):
    """Raised when the RFXtrx cannot be reached or driven."""


def _lock(hass: HomeAssistant, name: str) -> asyncio.Lock:
    locks = hass.data.setdefault(f"{DOMAIN}_gateway_locks", {})
    return locks.setdefault(name, asyncio.Lock())


async def _async_finish(task: asyncio.Future[Any], timeout: float) -> Any:
    """Finish an in-flight operation before propagating cancellation.

    Cancelling an executor future does not stop its serial write. Cleanup and
    the next command must wait until that write has actually finished --
    but not indefinitely. A write that is never going to return would
    otherwise make the caller uncancellable, which takes the config entry and
    Home Assistant's own shutdown down with it.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    cancelled = False
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(asyncio.shield(task), remaining)
        except TimeoutError:
            break
        except asyncio.CancelledError:
            cancelled = True

    if not task.done():
        # Abandoned, not stopped: it still owns the port, so whoever holds the
        # lock on its behalf keeps holding it until it really finishes.
        _LOGGER.warning(
            "The RFXCOM did not finish an operation within %.0f seconds; "
            "giving up on waiting for it",
            timeout,
        )
        if cancelled:
            raise asyncio.CancelledError
        raise GatewayError(
            f"The RFXCOM did not respond within {timeout:.0f} seconds. Its "
            "connection may need to be reloaded."
        )

    if cancelled:
        try:
            task.result()
        finally:
            raise asyncio.CancelledError
    return task.result()


async def _async_exclusive(
    hass: HomeAssistant,
    start: Callable[[], asyncio.Future[Any]],
    timeout: float,
) -> Any:
    """Drive the port from one place at a time, within a deadline.

    The lock is released when the operation genuinely finishes rather than
    when this returns, so abandoning a stuck write cannot let the next one
    interleave with it half-written on the wire.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    lock = _lock(hass, "send")
    try:
        async with asyncio.timeout_at(deadline):
            await lock.acquire()
    except TimeoutError:
        raise GatewayError(
            "The RFXCOM is still busy with an earlier command that has not "
            "come back. Its connection may need to be reloaded."
        ) from None

    try:
        task = start()
    except BaseException:
        lock.release()
        raise
    task.add_done_callback(lambda _: lock.release())

    return await _async_finish(task, deadline - loop.time())


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


def supported_bands() -> dict[str, int]:
    """The bands this build of pyRFXtrx can name, by their firmware code.

    Which of them a given RFXtrx will actually accept depends on its hardware;
    the device is the only authority on that and it is not asked in advance.
    """
    from RFXtrx import lowlevel  # noqa: PLC0415

    types = getattr(getattr(lowlevel, "Status", None), "TYPES", None) or {}
    return {name: code for code, name in types.items()}


def receiver_band(hass: HomeAssistant) -> str | None:
    """The band the RFXtrx is tuned to, as its firmware reports it.

    This is the receiver's own fixed frequency, not a measurement of anything
    received: raw packets carry pulse timings and nothing else, so the
    frequency a remote actually transmits on cannot be known from here.
    """
    try:
        rfx = _rfx_object(hass)
    except GatewayError:
        return None
    device = getattr(getattr(rfx, "_status", None), "device", None)
    return getattr(device, "type_string", None)


def receiver_firmware(hass: HomeAssistant) -> int | None:
    try:
        rfx = _rfx_object(hass)
    except GatewayError:
        return None
    device = getattr(getattr(rfx, "_status", None), "device", None)
    return getattr(device, "firmware_version", None)


def _mode_packet(
    rfx: Any, protocols: list[str], band: int | None = None
) -> bytearray:
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
    # The device's own type is what puts the band back, because the status is
    # never re-read: it still holds the band we found it on.
    data[5] = device.tranceiver_type if band is None else band
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
    async def send_command() -> None:
        for event in events:
            await hass.services.async_call(
                RFXTRX_DOMAIN, RFXTRX_SERVICE_SEND, {"event": event}, blocking=True
            )

    await _async_exclusive(
        hass, lambda: hass.async_create_task(send_command()), SEND_TIMEOUT
    )


class RawListener:
    """Collects raw packets while every receive protocol is enabled.

    Use as an async context manager: it opens the protocol list up, hooks the
    transport, and puts both back on the way out.
    """

    def __init__(self, hass: HomeAssistant, band: int | None = None) -> None:
        self._hass = hass
        self._band = band
        self._rfx: Any = None
        self._previous: list[str] | None = None
        self._transport: Any = None
        self._original_parse: Callable[[Any], Any] | None = None
        self._had_instance_parse = False
        self._capture_lock: asyncio.Lock | None = None
        self._queue: Queue[bytes] = Queue(maxsize=QUEUE_SIZE)
        self._buffer_lock = Lock()
        self._accepting = False
        self.packets_dropped = 0
        self._loop = asyncio.get_running_loop()
        # Counted so a failed capture can say which of the two things went
        # wrong: nothing arrived, or the device never went into raw mode.
        self.packets_seen = 0
        self.raw_seen = 0

    async def __aenter__(self) -> RawListener:
        capture_lock = _lock(self._hass, "capture")
        if capture_lock.locked():
            raise GatewayError("Already listening. Stop the current capture first.")
        await capture_lock.acquire()
        self._capture_lock = capture_lock
        try:
            find_entry(self._hass)
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
            await self._async_set_protocols(supported_protocols(), band=self._band)
        except (Exception, asyncio.CancelledError):
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await _async_finish(
            self._hass.async_create_task(self._async_close()), CLOSE_TIMEOUT
        )

    async def _async_close(self) -> None:
        try:
            if self._previous is not None:
                await self._async_set_protocols(self._previous)
        except Exception:  # noqa: BLE001 - never mask the original failure
            _LOGGER.exception("Could not restore the RFXCOM protocol list")
        finally:
            self._remove_hook()
            self._previous = None
            if self._capture_lock is not None:
                self._capture_lock.release()
                self._capture_lock = None

    async def _async_set_protocols(
        self, protocols: list[str], band: int | None = None
    ) -> None:
        packet = _mode_packet(self._rfx, protocols, band=band)
        transport = self._rfx.transport
        _LOGGER.debug(
            "Setting %d receive protocols%s: %s",
            len(protocols),
            f" on band {band:#04x}" if band is not None else "",
            packet.hex(),
        )
        try:
            await _async_exclusive(
                self._hass,
                lambda: self._hass.async_add_executor_job(transport.send, packet),
                WRITE_TIMEOUT,
            )
        except GatewayError:
            raise
        except Exception as err:  # noqa: BLE001 - surfaced to the user
            raise GatewayError(f"Could not change the RFXCOM mode: {err}") from err
        await asyncio.sleep(MODE_SETTLE)

    def _install_hook(self) -> None:
        transport = self._rfx.transport
        original = transport.parse
        self._had_instance_parse = "parse" in vars(transport)

        self._accepting = True

        def _parse(data: Any) -> Any:
            raw = False
            try:
                packet = bytes(data)
                self.packets_seen += 1
                raw = len(packet) >= 6 and packet[1] == 0x7F
                if raw:
                    self.raw_seen += 1
                    with self._buffer_lock:
                        if self._accepting:
                            if self._queue.full():
                                self.packets_dropped += 1
                            else:
                                self._queue.put_nowait(packet)
            except Exception:  # noqa: BLE001 - a bad capture must not kill the reader
                _LOGGER.debug("Ignoring malformed packet", exc_info=True)
                return original(data)

            if raw:
                # pyRFXtrx has no class for 0x7F and would discard it anyway.
                return original(data)

            # Everything else is dropped for the length of the capture. The
            # protocol list is wide open here, so these are transmissions the
            # configured list excludes: passing them on would have the core
            # integration decode the whole neighbourhood, add devices nobody
            # asked for, and do it on the event loop at the speed of the
            # serial port. The receiver is deaf during a capture either way.
            return None

        transport.parse = _parse
        self._transport = transport
        self._original_parse = original

    def _remove_hook(self) -> None:
        with self._buffer_lock:
            self._accepting = False
            while not self._queue.empty():
                self._queue.get_nowait()
        if self._transport is None:
            return
        if self._had_instance_parse:
            self._transport.parse = self._original_parse
        else:
            del self._transport.parse
        self._transport = None
        self._original_parse = None

    async def next_packet(self, timeout: float) -> bytes | None:
        """Wait for one raw packet, or None if nothing arrives in time."""
        deadline = self._loop.time() + timeout
        while True:
            if self.packets_dropped:
                raise GatewayError(
                    "RAW capture stopped because the receive buffer overflowed. "
                    "The receiver is producing packets faster than they can be processed."
                )
            try:
                return self._queue.get_nowait()
            except Empty:
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    return None
                await asyncio.sleep(min(QUEUE_POLL_INTERVAL, remaining))
