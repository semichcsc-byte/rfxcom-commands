"""The capture loop must not be able to monopolise the event loop.

Home Assistant runs everything on one loop. A coroutine that awaits only
things that are already resolved never reaches it, and the symptom of that is
indistinguishable from a crash: connections are accepted and nothing is
served.

`await queue.get()` on a queue that already holds an item is exactly such an
await, which is what makes this worth a test of its own rather than a comment.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import CAPTURE  # noqa: E402

from custom_components.rfxcom_commands import config_flow  # noqa: E402
from custom_components.rfxcom_commands.config_flow import (  # noqa: E402
    CommandSubentryFlowHandler,
)


class FloodingListener:
    """Never runs dry, and never blocks — the worst case for the loop."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._index = 0

    async def __aenter__(self) -> FloodingListener:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return

    async def next_packet(self, timeout: float) -> bytes:
        packet = CAPTURE[self._index % len(CAPTURE)]
        self._index += 1
        return packet


async def test_capture_keeps_yielding_under_a_flood(
    hass: HomeAssistant, monkeypatch
) -> None:
    monkeypatch.setattr(config_flow, "RawListener", FloodingListener)
    monkeypatch.setattr(config_flow, "LEARN_TIMEOUT", 0.5)
    monkeypatch.setattr(config_flow, "QUIET_PERIOD", 999)  # never go quiet
    monkeypatch.setattr(config_flow, "CONFIDENT_REPEATS", 10**9)  # never settle

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)

    handler = CommandSubentryFlowHandler()
    handler.hass = hass
    await handler._capture()

    beat.cancel()

    # Without a yield per iteration this is 0 and Home Assistant is frozen for
    # the whole capture window.
    assert ticks > 100
