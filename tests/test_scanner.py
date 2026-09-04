"""Tests for the live scanner."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import CAPTURE, EXPECTED_BITS  # noqa: E402
from test_config_flow import FakeListener  # noqa: E402

from custom_components.rfxcom_commands import capture as capture_module  # noqa: E402
from custom_components.rfxcom_commands import scanner as scanner_module  # noqa: E402
from custom_components.rfxcom_commands.const import DOMAIN  # noqa: E402

SCANNER = "switch.rfxcom_commands_scanner"
LAST_CODE = "sensor.rfxcom_commands_last_code"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(capture_module, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(scanner_module, "MAX_SCAN_SECONDS", 1)


@pytest.fixture
def rfxtrx(hass: HomeAssistant):
    entry = MockConfigEntry(domain="rfxtrx", data={}, options={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


async def setup_integration(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="RFXCOM Commands", data={}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_the_scanner_publishes_codes_as_they_arrive(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    monkeypatch.setattr(
        scanner_module,
        "RawListener",
        lambda hass: FakeListener(hass, packets=CAPTURE * 2),
    )
    await setup_integration(hass)

    assert hass.states.get(SCANNER).state == "off"
    assert hass.states.get(LAST_CODE).state == "unknown"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SCANNER}, blocking=True
    )
    await hass.async_block_till_done()

    state = hass.states.get(LAST_CODE)
    assert state.state == EXPECTED_BITS
    assert state.attributes["recent"] == [{"bits": EXPECTED_BITS, "times": 2}]
    assert state.attributes["raw_packets"] == 4

    # The window closed on its own, so the receiver is not left deaf.
    assert hass.states.get(SCANNER).state == "off"


async def test_switching_the_scanner_off_stops_it(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    monkeypatch.setattr(scanner_module, "MAX_SCAN_SECONDS", 60)
    monkeypatch.setattr(
        scanner_module, "RawListener", lambda hass: FakeListener(hass, packets=[])
    )
    entry = await setup_integration(hass)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SCANNER}, blocking=True
    )
    assert entry.runtime_data.scanner.running
    assert hass.states.get(SCANNER).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SCANNER}, blocking=True
    )
    assert not entry.runtime_data.scanner.running
    assert hass.states.get(SCANNER).state == "off"
