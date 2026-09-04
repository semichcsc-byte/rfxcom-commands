"""Tests for the live scanner."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import CAPTURE, EXPECTED_BITS  # noqa: E402
from test_config_flow import FakeListener  # noqa: E402

from custom_components.rfxcom_commands import capture as capture_module  # noqa: E402
from custom_components.rfxcom_commands import scanner as scanner_module  # noqa: E402
from custom_components.rfxcom_commands.const import DOMAIN  # noqa: E402

SCANNER = "switch.rfxcom_commands_scanner"
LAST_CODE = "sensor.rfxcom_commands_last_code"
LAST_REPEATS = "sensor.rfxcom_commands_last_code_repeats"
BAND = "select.rfxcom_commands_scan_band"
CODES_HEARD = "sensor.rfxcom_commands_codes_heard"


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
        lambda hass, band=None: FakeListener(hass, packets=CAPTURE * 2, band=band),
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
    assert state.attributes["recent"] == [
        {"bits": EXPECTED_BITS, "heard": 2, "repeats": 4, "button": EXPECTED_BITS}
    ]
    assert state.attributes["raw_packets"] == 4
    # One code on its own has nothing to be compared against.
    assert state.attributes["address"] == ""

    # The repeat count and the readable form are the whole point of it, so
    # they get their own sensors rather than hiding in the attributes.
    assert hass.states.get(LAST_REPEATS).state == "4"
    assert hass.states.get("sensor.rfxcom_commands_last_code_hex").state == (
        "0x012D916A"
    )
    assert hass.states.get("sensor.rfxcom_commands_last_code_jitter").state == "1.4"
    assert hass.states.get("sensor.rfxcom_commands_last_code_encoding").state == "pwm"

    # The window closed on its own, so the receiver is not left deaf.
    assert hass.states.get(SCANNER).state == "off"


async def test_switching_the_scanner_off_stops_it(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    monkeypatch.setattr(scanner_module, "MAX_SCAN_SECONDS", 60)
    monkeypatch.setattr(
        scanner_module,
        "RawListener",
        lambda hass, band=None: FakeListener(hass, packets=[], band=band),
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


async def test_codes_that_never_repeat_look_like_rolling(
    hass: HomeAssistant,
) -> None:
    """A rolling code cannot be replayed by anything, so learning one would
    produce a button that silently does nothing."""
    scanner = scanner_module.Scanner(hass)

    address = "1010101010"
    for tail in ("0001", "1010", "0110"):
        scanner._remember(address + tail, 4)

    assert scanner.address == address
    assert not scanner.repeating
    assert scanner.looks_like_rolling


async def test_a_code_heard_twice_is_not_rolling(hass: HomeAssistant) -> None:
    """Repeating is the whole difference, so one repeat settles it."""
    scanner = scanner_module.Scanner(hass)

    for bits in ("10101010100001", "10101010100010", "10101010100011"):
        scanner._remember(bits, 4)
    scanner._remember("10101010100001", 4)

    assert scanner.repeating
    assert not scanner.looks_like_rolling


async def test_two_codes_are_not_enough_to_suspect_rolling(
    hass: HomeAssistant,
) -> None:
    """A remote with an alternating bit sends two and replays perfectly."""
    scanner = scanner_module.Scanner(hass)

    scanner._remember("10101010100001", 4)
    scanner._remember("10101010100010", 4)

    assert not scanner.looks_like_rolling


async def test_the_band_is_chosen_before_listening(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    """A band is a property of the whole device, so it is only moved while the
    scanner runs and only settable while it is stopped."""
    listeners: list[FakeListener] = []

    def _listener(hass: HomeAssistant, band: int | None = None) -> FakeListener:
        listeners.append(FakeListener(hass, packets=CAPTURE, band=band))
        return listeners[-1]

    monkeypatch.setattr(scanner_module, "RawListener", _listener)
    await setup_integration(hass)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": BAND, "option": "868.00MHz"},
        blocking=True,
    )
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SCANNER}, blocking=True
    )
    await hass.async_block_till_done()

    assert listeners[0].band == 0x55

    monkeypatch.setattr(scanner_module, "MAX_SCAN_SECONDS", 60)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SCANNER}, blocking=True
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": BAND, "option": "315MHz"},
            blocking=True,
        )


async def test_every_scanner_entity_is_created(hass: HomeAssistant, rfxtrx) -> None:
    """A platform that raises takes only its own entity down, silently."""
    await setup_integration(hass)
    for entity_id in (SCANNER, LAST_CODE, LAST_REPEATS, BAND, CODES_HEARD):
        assert hass.states.get(entity_id) is not None, entity_id
