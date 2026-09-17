"""Tests for the bridge to the RFXtrx the core integration owns.

These stand in for the part that cannot be exercised without hardware: the
hook into pyRFXtrx's transport, and switching the receive protocols over the
open connection.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rfxcom_commands import gateway
from custom_components.rfxcom_commands.config_flow import CommandSubentryFlowHandler
from custom_components.rfxcom_commands.gateway import (
    GatewayError,
    RawListener,
    current_protocols,
    find_entry,
)
from custom_components.rfxcom_commands.scanner import Scanner
from custom_components.rfxcom_commands.services import async_listen

RAW_PACKET = bytes.fromhex("087f000000017c046f")
UNDECODED_PACKET = bytes.fromhex("05030c2405f8")


class FakeTransport:
    """Enough of pyRFXtrx's PySerialTransport to hook into.

    `parse` has to live on the class: the listener shadows it with an instance
    attribute and deletes that again on the way out.
    """

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.parsed: list[bytes] = []

    def parse(self, data):  # noqa: D102 - mirrors the library
        # Tolerant on purpose: these tests are about the hook wrapped around
        # this method, not about how the library reacts to rubbish.
        try:
            self.parsed.append(bytes(data))
        except TypeError:
            pass
        return None

    def send(self, data) -> None:  # noqa: D102 - mirrors the library
        self.sent.append(bytes(data))

    def close(self) -> None:  # noqa: D102 - mirrors the library
        return


def make_rfx(modes: list[str] | None = None) -> SimpleNamespace:
    """A stand-in for pyRFXtrx's `Connect`."""
    return SimpleNamespace(
        transport=FakeTransport(),
        _modes=modes,
        _status=SimpleNamespace(
            device=SimpleNamespace(
                tranceiver_type=0x53, output_power=0x00, devices=[]
            )
        ),
        close_connection=lambda: None,
    )


@pytest.fixture
def rfxtrx(hass: HomeAssistant):
    """A loaded RFXtrx config entry with a fake connection behind it."""
    entry = MockConfigEntry(domain="rfxtrx", data={}, options={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    rfx = make_rfx(modes=["arc", "x10"])
    hass.data["rfxtrx"] = {"rfxobject": rfx}
    return rfx


@pytest.fixture(autouse=True)
def no_settle(monkeypatch):
    """The real settle time is a second per mode change; tests do not need it."""
    monkeypatch.setattr(gateway, "MODE_SETTLE", 0)


async def test_find_entry_requires_the_core_integration(hass: HomeAssistant) -> None:
    with pytest.raises(GatewayError, match="not set up"):
        find_entry(hass)


async def test_current_protocols_prefers_the_live_selection(
    hass: HomeAssistant, rfxtrx
) -> None:
    assert current_protocols(hass) == ["arc", "x10"]


async def test_current_protocols_falls_back_to_the_device_report(
    hass: HomeAssistant, rfxtrx
) -> None:
    rfxtrx._modes = None
    rfxtrx._status.device.devices = ["oregon"]
    assert current_protocols(hass) == ["oregon"]


async def test_refuses_to_switch_when_it_cannot_restore(
    hass: HomeAssistant, rfxtrx
) -> None:
    """Better to fail than to leave the receiver decoding nothing."""
    rfxtrx._modes = None
    rfxtrx._status.device.devices = []
    with pytest.raises(GatewayError, match="restored"):
        async with RawListener(hass):
            pass
    assert rfxtrx.transport.sent == []


async def test_enables_every_protocol_then_puts_it_back(
    hass: HomeAssistant, rfxtrx
) -> None:
    async with RawListener(hass):
        assert len(rfxtrx.transport.sent) == 1
        opened = rfxtrx.transport.sent[0]

    assert len(rfxtrx.transport.sent) == 2
    restored = rfxtrx.transport.sent[1]

    # Both are "set mode" commands for the same transceiver.
    for packet in (opened, restored):
        assert packet[0] == 0x0D
        assert packet[4] == 0x03
        assert packet[5] == 0x53

    # Opening up sets strictly more protocol bits than restoring does.
    opened_bits = sum(bin(b).count("1") for b in opened[7:11])
    restored_bits = sum(bin(b).count("1") for b in restored[7:11])
    assert opened_bits > restored_bits


async def test_hook_is_installed_and_removed(hass: HomeAssistant, rfxtrx) -> None:
    transport = rfxtrx.transport
    original = FakeTransport.parse

    async with RawListener(hass):
        assert transport.__dict__.get("parse") is not None

    assert "parse" not in transport.__dict__
    assert transport.parse.__func__ is original


async def test_hook_still_passes_packets_to_the_library(
    hass: HomeAssistant, rfxtrx
) -> None:
    transport = rfxtrx.transport
    async with RawListener(hass):
        transport.parse(UNDECODED_PACKET)
    assert transport.parsed == [UNDECODED_PACKET]


async def test_only_raw_packets_are_queued(hass: HomeAssistant, rfxtrx) -> None:
    transport = rfxtrx.transport
    async with RawListener(hass) as listener:
        transport.parse(UNDECODED_PACKET)
        transport.parse(RAW_PACKET)
        await asyncio.sleep(0)  # let call_soon_threadsafe run

        assert await listener.next_packet(timeout=0.1) == RAW_PACKET
        assert await listener.next_packet(timeout=0.05) is None


async def test_queue_is_bounded(hass: HomeAssistant, rfxtrx) -> None:
    """A busy band must not be able to grow the backlog without limit."""
    transport = rfxtrx.transport
    async with RawListener(hass) as listener:
        for index in range(gateway.QUEUE_SIZE * 3):
            transport.parse(RAW_PACKET[:4] + bytes([index & 0xFF]) + RAW_PACKET[5:])
        await asyncio.sleep(0)

        assert listener._queue.qsize() == gateway.QUEUE_SIZE


async def test_raw_flood_is_bounded_before_the_event_loop_runs(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    async with RawListener(hass) as listener:
        loop = asyncio.get_running_loop()
        scheduled = Mock(wraps=loop.call_soon_threadsafe)
        with monkeypatch.context() as patch:
            patch.setattr(loop, "call_soon_threadsafe", scheduled)
            for _packet in range(gateway.QUEUE_SIZE * 100):
                rfxtrx.transport.parse(RAW_PACKET)
        assert scheduled.call_count <= 1
        assert listener._queue.qsize() == gateway.QUEUE_SIZE
        assert listener.packets_dropped == gateway.QUEUE_SIZE * 99


async def test_receive_overflow_stops_capture_and_restores_protocols(
    hass: HomeAssistant, rfxtrx
) -> None:
    with pytest.raises(GatewayError, match="buffer overflowed"):
        async with RawListener(hass) as listener:
            for _packet in range(gateway.QUEUE_SIZE + 1):
                rfxtrx.transport.parse(RAW_PACKET)
            await listener.next_packet(0.1)
    assert len(rfxtrx.transport.sent) == 2
    assert "parse" not in vars(rfxtrx.transport)
    assert listener._queue.empty()


async def test_packet_from_reader_thread_is_received(
    hass: HomeAssistant, rfxtrx
) -> None:
    async with RawListener(hass) as listener:
        await hass.async_add_executor_job(rfxtrx.transport.parse, RAW_PACKET)
        assert await listener.next_packet(0.1) == RAW_PACKET


async def test_stale_hook_cannot_enqueue_after_capture_closes(
    hass: HomeAssistant, rfxtrx
) -> None:
    async with RawListener(hass) as listener:
        hook = rfxtrx.transport.parse
    await hass.async_add_executor_job(hook, RAW_PACKET)
    assert listener._queue.empty()


async def test_scanner_survives_reader_thread_flood(
    hass: HomeAssistant, rfxtrx
) -> None:
    scanner = Scanner(hass)
    await scanner.async_start()
    task = scanner._task
    hook = rfxtrx.transport.parse
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while scanner.running:
            ticks += 1
            await asyncio.sleep(0)

    def flood():
        for _packet in range(gateway.QUEUE_SIZE * 100):
            hook(RAW_PACKET)

    beat = asyncio.create_task(heartbeat())
    try:
        await hass.async_add_executor_job(flood)
        await asyncio.wait_for(asyncio.shield(task), 2)
        assert scanner.error is not None
        assert "buffer overflowed" in scanner.error
        assert ticks > 0
        assert not scanner.running
        assert len(rfxtrx.transport.sent) == 2
        assert "parse" not in vars(rfxtrx.transport)
    finally:
        await scanner.async_stop()
        await beat


async def test_a_bad_packet_does_not_break_the_reader(
    hass: HomeAssistant, rfxtrx
) -> None:
    """The hook runs on pyRFXtrx's thread; an exception there would kill it."""
    transport = rfxtrx.transport
    async with RawListener(hass):
        assert transport.parse(object()) is None


async def test_protocols_are_restored_after_a_failure(
    hass: HomeAssistant, rfxtrx
) -> None:
    with pytest.raises(RuntimeError):
        async with RawListener(hass):
            raise RuntimeError("capture blew up")

    assert len(rfxtrx.transport.sent) == 2
    assert "parse" not in rfxtrx.transport.__dict__


async def test_cancel_during_mode_entry_restores_and_releases(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    entered = asyncio.Event()
    original = RawListener._async_set_protocols

    async def set_protocols(listener, protocols, band=None):
        await original(listener, protocols, band)
        if len(rfxtrx.transport.sent) == 1:
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(RawListener, "_async_set_protocols", set_protocols)
    task = asyncio.create_task(RawListener(hass).__aenter__())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(rfxtrx.transport.sent) == 2
    assert "parse" not in vars(rfxtrx.transport)
    async with RawListener(hass):
        pass


async def test_cancel_waits_for_pending_write_before_restore(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def write(function, *args):
        if not rfxtrx.transport.sent:
            started.set()
            await release.wait()
        function(*args)

    original_executor = hass.async_add_executor_job
    monkeypatch.setattr(
        hass, "async_add_executor_job",
        lambda function, *args: (
            asyncio.create_task(write(function, *args))
            if function == rfxtrx.transport.send
            else original_executor(function, *args)
        ),
    )
    task = asyncio.create_task(RawListener(hass).__aenter__())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    with pytest.raises(GatewayError, match="Already listening"):
        async with RawListener(hass):
            pass
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(rfxtrx.transport.sent) == 2
    assert rfxtrx.transport.sent[-1][7:11] != rfxtrx.transport.sent[0][7:11]
    assert "parse" not in vars(rfxtrx.transport)


async def test_overlapping_capture_is_rejected_without_touching_first(
    hass: HomeAssistant, rfxtrx
) -> None:
    async with RawListener(hass) as first:
        hook = rfxtrx.transport.parse
        with pytest.raises(GatewayError, match="Already listening"):
            async with RawListener(hass):
                pass
        assert rfxtrx.transport.parse is hook
        assert len(rfxtrx.transport.sent) == 1
        rfxtrx.transport.parse(RAW_PACKET)
        assert await first.next_packet(0.1) == RAW_PACKET


async def test_existing_transport_hook_is_restored(
    hass: HomeAssistant, rfxtrx
) -> None:
    original = lambda packet: packet
    rfxtrx.transport.parse = original
    async with RawListener(hass):
        pass
    assert rfxtrx.transport.parse is original


async def test_multipart_sends_are_atomic_even_when_cancelled(
    hass: HomeAssistant,
) -> None:
    sent = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def service(call):
        sent.append(call.data["event"])
        if call.data["event"] == "A0":
            started.set()
            await release.wait()

    hass.services.async_register("rfxtrx", "send", service)
    first = asyncio.create_task(gateway.async_send(hass, ["A0", "A1"]))
    await started.wait()
    second = asyncio.create_task(gateway.async_send(hass, ["B0", "B1"]))
    first.cancel()
    await asyncio.sleep(0)
    assert sent == ["A0"]
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert sent == ["A0", "A1", "B0", "B1"]


async def test_failed_mode_entry_restores_and_releases(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    original = rfxtrx.transport.send

    def fail_after_write(packet):
        original(packet)
        if len(rfxtrx.transport.sent) == 1:
            raise OSError("Mode write failed")

    monkeypatch.setattr(rfxtrx.transport, "send", fail_after_write)
    with pytest.raises(GatewayError, match="Mode write failed"):
        async with RawListener(hass):
            pass
    assert len(rfxtrx.transport.sent) == 2
    assert "parse" not in vars(rfxtrx.transport)
    async with RawListener(hass):
        pass


async def test_repeated_cancellation_waits_for_restore(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    restoring = asyncio.Event()
    release = asyncio.Event()
    original = RawListener._async_set_protocols

    async def set_protocols(listener, protocols, band=None):
        if protocols == ["arc", "x10"]:
            restoring.set()
            await release.wait()
        await original(listener, protocols, band)

    monkeypatch.setattr(RawListener, "_async_set_protocols", set_protocols)

    async def capture():
        async with RawListener(hass):
            pass

    task = asyncio.create_task(capture())
    await restoring.wait()
    for _attempt in range(2):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    with pytest.raises(GatewayError, match="Already listening"):
        async with RawListener(hass):
            pass
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(rfxtrx.transport.sent) == 2
    assert "parse" not in vars(rfxtrx.transport)
    async with RawListener(hass):
        pass


async def test_scanner_excludes_learning_and_watch(
    hass: HomeAssistant, rfxtrx
) -> None:
    scanner = Scanner(hass)
    handler = CommandSubentryFlowHandler()
    handler.hass = hass
    await scanner.async_start()
    hook = rfxtrx.transport.parse
    try:
        with pytest.raises(GatewayError, match="Already listening"):
            await handler._capture()
        with pytest.raises(HomeAssistantError, match="Already listening"):
            await async_listen(hass, 1)
        assert rfxtrx.transport.parse is hook
        assert len(rfxtrx.transport.sent) == 1
        assert scanner.running
    finally:
        await scanner.async_stop()
    assert len(rfxtrx.transport.sent) == 2
    assert "parse" not in vars(rfxtrx.transport)
