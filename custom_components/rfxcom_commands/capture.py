"""Assembling raw packets into commands.

Shared by the learning flow, which wants the first command it can trust, and
the watch action, which wants everything it hears.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable

from .const import MAX_PACKETS_PER_CAPTURE, MIN_FRAMES, POLL_INTERVAL
from .gateway import RawListener
from .rawrf import MAX_PACKETS, Command, RawRFError, decode, is_last_packet

_LOGGER = logging.getLogger(__name__)


class Capture:
    """Turns the packet stream from one listener into decoded commands.

    Bursts are assembled strictly by packet index. A burst can never hold more
    than four packets, so anything that does not fit that shape is a lost or
    interleaved packet and the partial burst is dropped.
    """

    def __init__(self, listener: RawListener) -> None:
        self._listener = listener
        self.last_decode_error: str | None = None
        self.bursts_dropped = 0
        self.rejected = 0

    async def commands(
        self,
        seconds: float,
        on_reject: Callable[[str], None] | None = None,
    ) -> AsyncIterator[Command]:
        """Yield every command decoded within the window.

        `on_reject` hears about the ones that arrived and could not be read,
        which is the difference between a silent band and a remote this cannot
        decode -- indistinguishable otherwise.
        """
        deadline = time.monotonic() + seconds
        burst: list[bytes] = []
        seen = 0

        while time.monotonic() < deadline:
            # Yield unconditionally. Awaiting a queue that already has an item
            # does not reach the event loop, so a fast enough stream of packets
            # would otherwise let this loop starve Home Assistant for the whole
            # window.
            await asyncio.sleep(0)

            packet = await self._listener.next_packet(timeout=POLL_INTERVAL)
            if packet is None:
                continue

            seen += 1
            if seen > MAX_PACKETS_PER_CAPTURE:
                _LOGGER.debug("Too many packets to keep up with; stopping")
                return

            index = packet[2]
            _LOGGER.debug(
                "raw packet #%d: index=%d seq=%d last=%s pulses=%d",
                seen, index, packet[3], bool(packet[4]), (len(packet) - 5) // 2,
            )
            if index == 0:
                if burst:
                    self.bursts_dropped += 1
                    _LOGGER.debug("  a new burst started before %d finished", len(burst))
                burst = [packet]
            elif burst and index == len(burst) and len(burst) < MAX_PACKETS:
                burst.append(packet)
            else:
                self.bursts_dropped += 1
                _LOGGER.debug(
                    "  out of order: expected index %d, dropping the burst", len(burst)
                )
                burst = []
                continue

            if not is_last_packet(packet):
                continue

            try:
                command = decode(burst, min_frames=MIN_FRAMES)
            except RawRFError as err:
                _LOGGER.debug("  burst of %d packet(s) rejected: %s", len(burst), err)
                self.last_decode_error = str(err)
                self.rejected += 1
                burst = []
                if on_reject is not None:
                    on_reject(str(err))
                continue
            burst = []
            _LOGGER.debug("  accepted: %s", command.bits)
            yield command
