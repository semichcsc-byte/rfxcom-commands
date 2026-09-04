"""Tests for the watch action."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import CAPTURE, EXPECTED_BITS  # noqa: E402
from test_config_flow import FakeListener  # noqa: E402

from custom_components.rfxcom_commands import capture as capture_module  # noqa: E402
from custom_components.rfxcom_commands import services  # noqa: E402
from custom_components.rfxcom_commands.const import (  # noqa: E402
    ATTR_SECONDS,
    DOMAIN,
    EVENT_RAW_COMMAND,
    SERVICE_WATCH,
)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(capture_module, "POLL_INTERVAL", 0.01)


@pytest.fixture
def rfxtrx(hass: HomeAssistant):
    entry = MockConfigEntry(domain="rfxtrx", data={}, options={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


async def test_watch_reports_and_announces_what_it_hears(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    monkeypatch.setattr(
        services,
        "RawListener",
        lambda hass: FakeListener(hass, packets=CAPTURE * 2),
    )

    entry = MockConfigEntry(
        domain=DOMAIN, title="RFXCOM Commands", data={}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    announced = []
    hass.bus.async_listen(EVENT_RAW_COMMAND, lambda event: announced.append(event.data))

    result = await hass.services.async_call(
        DOMAIN,
        SERVICE_WATCH,
        {ATTR_SECONDS: 1},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert [record["bits"] for record in result["heard"]] == [EXPECTED_BITS]
    assert result["heard"][0]["times"] == 2
    assert result["raw_packets"] == 4

    # Fired as they arrive, so they can be watched live.
    assert [record["bits"] for record in announced] == [EXPECTED_BITS] * 2


async def test_the_integration_page_can_show_what_was_heard(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    """Developer tools is not where people are; the integration page is."""
    monkeypatch.setattr(
        services, "RawListener", lambda hass: FakeListener(hass, packets=CAPTURE)
    )

    entry = MockConfigEntry(
        domain=DOMAIN, title="RFXCOM Commands", data={}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {ATTR_SECONDS: 1}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["step_id"] == "heard"
    assert EXPECTED_BITS in result["description_placeholders"]["report"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"again": False}
    )
    # Nothing to save, so it must not rewrite the entry and force a reload.
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "watched"
