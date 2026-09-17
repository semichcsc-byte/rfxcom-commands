"""The live scanner: keeps raw mode on and publishes what arrives.

Everything else in this integration listens for a moment and puts the receiver
back. This one stays on until it is switched off, because watching codes appear
as you press buttons is the whole point of it.

The cost is real: while it runs the RFXCOM decodes nothing else, so it stops on
its own after a while rather than being left on by accident.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .capture import Capture
from .const import EVENT_RAW_COMMAND, MAX_SCAN_SECONDS, RECENT_CODES, ROLLING_CODES_HINT
from .gateway import GatewayError, RawListener

_LOGGER = logging.getLogger(__name__)


class Scanner:
    """Holds raw mode open and announces every command decoded."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Event | None = None
        self._listeners: list[Callable[[], None]] = []
        self.last: dict[str, Any] | None = None
        self.recent: list[dict[str, Any]] = []
        self.address = ""
        self.packets = 0
        self.raw_packets = 0
        self.bursts_dropped = 0
        self.rejected = 0
        self.last_reject_reason: str | None = None
        self.error: str | None = None
        # None means whatever the device is already on.
        self.band: int | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @callback
    def async_subscribe(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)

        def _unsubscribe() -> None:
            self._listeners.remove(update)

        return _unsubscribe

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    async def async_start(self) -> None:
        if not self.running:
            self.error = None
            self._ready = asyncio.Event()
            self._task = self._hass.async_create_task(self._run())
            self._notify()
        assert self._ready is not None
        try:
            await self._ready.wait()
        except asyncio.CancelledError:
            await self.async_stop()
            raise

    async def async_stop(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._notify()

    async def _run(self) -> None:
        ready = self._ready
        try:
            async with RawListener(self._hass, band=self.band) as listener:
                if ready is not None:
                    ready.set()
                capture = Capture(listener)

                def _rejected(reason: str) -> None:
                    self.rejected = capture.rejected
                    self.last_reject_reason = reason
                    self.packets = listener.packets_seen
                    self.raw_packets = listener.raw_seen
                    self.bursts_dropped = capture.bursts_dropped
                    self._notify()

                async for command in capture.commands(
                    MAX_SCAN_SECONDS, on_reject=_rejected
                ):
                    self.last = {
                        "bits": command.bits,
                        "hex": command.hex,
                        "bit_count": len(command.bits),
                        "repeats": command.frames_seen,
                        "encoding": command.encoding,
                        "jitter_pct": command.jitter_pct,
                        "inverted": command.inverted,
                        "short_us": command.short,
                        "long_us": command.long,
                        "gap_us": command.gap,
                        "frame_us": command.frame_us,
                        "burst_us": command.burst_us,
                        "pulses": len(command.pulses),
                    }
                    if not command.trustworthy:
                        self.last["warning"] = (
                            f"This {command.encoding} signature describes pulse "
                            "lengths, not decoded protocol bits. The captured "
                            "pulses are used for replay."
                        )
                    self._remember(command.bits, command.frames_seen)
                    self._hass.bus.async_fire(
                        EVENT_RAW_COMMAND,
                        {**self.last, "heard": self.recent[0]["heard"]},
                    )
                    self.packets = listener.packets_seen
                    self.raw_packets = listener.raw_seen
                    self.bursts_dropped = capture.bursts_dropped
                    self._notify()
                self.packets = listener.packets_seen
                self.raw_packets = listener.raw_seen
                self.bursts_dropped = capture.bursts_dropped
        except asyncio.CancelledError:
            raise
        except GatewayError as err:
            self.error = str(err)
        except Exception as err:  # noqa: BLE001 - surfaced on the switch
            _LOGGER.exception("The scanner stopped")
            self.error = str(err)
        finally:
            if self._task is asyncio.current_task():
                self._task = None
            if ready is not None:
                ready.set()
            self._notify()

    @property
    def repeating(self) -> bool:
        """Whether any code was heard more than once."""
        return any(seen["heard"] > 1 for seen in self.recent)

    @property
    def looks_like_rolling(self) -> bool:
        """Several codes from one address, none of them ever repeating.

        This is a heuristic, not a protocol classification. Separate commands,
        alternating bits and unrelated transmitters can produce similar results.
        """
        return (
            len(self.recent) >= ROLLING_CODES_HINT
            and bool(self.address)
            and not self.repeating
        )

    def _remember(self, bits: str, repeats: int) -> None:
        for seen in self.recent:
            if seen["bits"] == bits:
                seen["heard"] += 1
                seen["repeats"] = repeats
                self.recent.remove(seen)
                self.recent.insert(0, seen)
                break
        else:
            self.recent.insert(0, {"bits": bits, "heard": 1, "repeats": repeats})
            del self.recent[RECENT_CODES:]
        self._split_address()

    def _split_address(self) -> None:
        """Whatever every code heard has in common is the remote's address.

        Two buttons of one remote share it, so the part that differs is the
        button. With a single code there is nothing to compare and no split.
        """
        codes = [seen["bits"] for seen in self.recent]
        self.address = os.path.commonprefix(codes) if len(codes) > 1 else ""
        for seen in self.recent:
            seen["button"] = seen["bits"][len(self.address):]
