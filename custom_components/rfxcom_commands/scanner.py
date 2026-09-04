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
from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .capture import Capture
from .const import MAX_SCAN_SECONDS, RECENT_CODES
from .gateway import GatewayError, RawListener

_LOGGER = logging.getLogger(__name__)


class Scanner:
    """Holds raw mode open and announces every command decoded."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[], None]] = []
        self.last: dict[str, Any] | None = None
        self.recent: list[dict[str, Any]] = []
        self.packets = 0
        self.raw_packets = 0
        self.bursts_dropped = 0
        self.error: str | None = None

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
        if self.running:
            return
        self.error = None
        self._task = self._hass.async_create_task(self._run())
        self._notify()

    async def async_stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._notify()

    async def _run(self) -> None:
        try:
            async with RawListener(self._hass) as listener:
                capture = Capture(listener)
                async for command in capture.commands(MAX_SCAN_SECONDS):
                    self.last = {
                        "bits": command.bits,
                        "repeats": command.frames_seen,
                        "short_us": command.short,
                        "long_us": command.long,
                        "gap_us": command.gap,
                        "pulses": len(command.pulses),
                    }
                    self._remember(command.bits, command.frames_seen)
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
            self._task = None
            self._notify()

    def _remember(self, bits: str, repeats: int) -> None:
        for seen in self.recent:
            if seen["bits"] == bits:
                seen["heard"] += 1
                seen["repeats"] = repeats
                self.recent.remove(seen)
                self.recent.insert(0, seen)
                return
        self.recent.insert(0, {"bits": bits, "heard": 1, "repeats": repeats})
        del self.recent[RECENT_CODES:]
