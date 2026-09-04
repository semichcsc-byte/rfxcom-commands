"""Tests for the bridge to the RFXtrx the core integration owns.

These stand in for the part that cannot be exercised without hardware: the
hook into pyRFXtrx's transport, and switching the receive protocols over the
open connection.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rfxcom_commands import gateway
from custom_components.rfxcom_commands.gateway import (
    GatewayError,
    RawListener,
    current_protocols,
    find_entry,
)

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
