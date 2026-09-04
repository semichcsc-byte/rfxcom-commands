"""Tests for the config and subentry flows.

The gateway is faked here: what it does with the hardware is covered in
test_gateway.py, and these tests are about the flow that wraps it — the one
that has been the source of every problem in the field.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures import CAPTURE, EXPECTED_BITS  # noqa: E402

from custom_components import rfxcom_commands  # noqa: E402
from custom_components.rfxcom_commands import capture as capture_module  # noqa: E402
from custom_components.rfxcom_commands import config_flow  # noqa: E402
from custom_components.rfxcom_commands.const import (  # noqa: E402
    CONF_EVENTS,
    CONF_KIND,
    CONF_TEST,
    DOMAIN,
    KIND_SWITCH,
)


class FakeListener:
    """Hands out a prepared capture instead of touching a radio."""

    sent: list[list[str]] = []

    def __init__(
        self,
        hass: HomeAssistant,
        packets: list[bytes] | None = None,
        *,
        band: int | None = None,
        packets_seen: int | None = None,
    ) -> None:
        self._packets = list(packets if packets is not None else CAPTURE)
        self.band = band
        self.raw_seen = len(self._packets)
        self.packets_seen = (
            self.raw_seen if packets_seen is None else packets_seen
        )

    async def __aenter__(self) -> FakeListener:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return

    async def next_packet(self, timeout: float) -> bytes | None:
        # Mirror the real listener, which always reaches the event loop.
        await asyncio.sleep(0)
        return self._packets.pop(0) if self._packets else None


@pytest.fixture(autouse=True)
def quick_capture(monkeypatch):
    """Real timings would make every flow test wait out the capture window."""
    monkeypatch.setattr(config_flow, "LEARN_TIMEOUT", 2)
    monkeypatch.setattr(capture_module, "POLL_INTERVAL", 0.01)


@pytest.fixture
def rfxtrx(hass: HomeAssistant):
    """A loaded RFXtrx entry, which is all the config flow checks for."""
    entry = MockConfigEntry(domain="rfxtrx", data={}, options={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


@pytest.fixture
def captures(monkeypatch):
    """Feed the flow a real burst, twice, as a held button would."""
    monkeypatch.setattr(
        config_flow, "RawListener", lambda hass: FakeListener(hass, packets=CAPTURE * 2)
    )


@pytest.fixture
def heard_once(monkeypatch):
    """Feed the flow a single burst, which is not enough to trust."""
    monkeypatch.setattr(
        config_flow, "RawListener", lambda hass: FakeListener(hass, packets=CAPTURE)
    )


@pytest.fixture
def nothing_heard(monkeypatch):
    """Feed the flow silence."""
    monkeypatch.setattr(
        config_flow, "RawListener", lambda hass: FakeListener(hass, packets=[])
    )


@pytest.fixture
def no_transmit(monkeypatch):
    """Record transmissions instead of performing them."""
    sent: list[list[str]] = []

    async def _send(hass, events):
        sent.append(list(events))

    monkeypatch.setattr(config_flow, "async_send", _send)
    monkeypatch.setattr(rfxcom_commands, "async_send", _send)
    return sent


async def setup_integration(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="RFXCOM Commands", data={}, unique_id=DOMAIN
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --- config flow ---------------------------------------------------------


async def test_refuses_to_set_up_without_the_core_integration(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_gateway"


async def test_sets_up_once_the_gateway_exists(hass: HomeAssistant, rfxtrx) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "RFXCOM Commands"


async def test_only_one_instance(hass: HomeAssistant, rfxtrx) -> None:
    await setup_integration(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.ABORT
    # single_config_entry in the manifest, so Home Assistant stops it earlier
    # than the flow's own unique-id check would.
    assert result["reason"] in ("already_configured", "single_instance_allowed")


# --- learning ------------------------------------------------------------


async def start_learning(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Walk as far as the confirmation form and submit it."""
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "command"), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "learn"

    await hass.async_block_till_done()
    return await hass.config_entries.subentries.async_configure(result["flow_id"])


async def test_a_capture_leads_to_naming(
    hass: HomeAssistant, rfxtrx, captures
) -> None:
    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name"
    # The decoded command is shown so the user can see what was captured.
    assert result["description_placeholders"]["bits"] == EXPECTED_BITS


async def test_silence_leads_to_the_failure_step(
    hass: HomeAssistant, rfxtrx, nothing_heard
) -> None:
    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "failed"
    assert "Nothing was received" in result["description_placeholders"]["error"]


async def test_a_single_press_is_enough(
    hass: HomeAssistant, rfxtrx, heard_once
) -> None:
    """One press carries several frames, so nothing more should be asked for."""
    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name"
    assert result["description_placeholders"]["bits"] == EXPECTED_BITS
    assert result["description_placeholders"]["frames"] == "4"


async def test_listening_stops_as_soon_as_it_is_sure(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    """Waiting out the window after the answer arrived just looks broken."""
    listeners: list[FakeListener] = []

    def _listener(hass: HomeAssistant) -> FakeListener:
        listeners.append(FakeListener(hass, packets=CAPTURE * 4))
        return listeners[-1]

    monkeypatch.setattr(config_flow, "RawListener", _listener)

    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)

    assert result["step_id"] == "name"
    # Two bursts were enough; the rest were never read.
    assert listeners[0]._packets


async def test_a_busy_receiver_that_never_went_raw_says_so(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    """Reporting silence while the receiver was busy sends people the wrong way."""
    monkeypatch.setattr(
        config_flow,
        "RawListener",
        lambda hass: FakeListener(hass, packets=[], packets_seen=7),
    )

    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)

    assert result["step_id"] == "failed"
    error = result["description_placeholders"]["error"]
    assert "received 7 transmissions" in error
    assert "did not switch into raw mode" in error


async def test_naming_creates_a_button(
    hass: HomeAssistant, rfxtrx, captures
) -> None:
    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Fan light", CONF_TEST: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Fan light"

    events = result["data"][CONF_EVENTS]
    assert len(events) == 1
    packet = bytes.fromhex(events[0])
    assert packet[1] == 0x7F  # raw transmit
    assert packet[4] == 4  # as many repeats as the remote sent

    await hass.async_block_till_done()
    assert hass.states.get("button.rfxcom_commands_fan_light") is not None


async def test_testing_transmits_without_saving(
    hass: HomeAssistant, rfxtrx, captures, no_transmit
) -> None:
    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Fan light", CONF_TEST: True}
    )
    # Back on the form, nothing saved yet.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "name"
    assert len(entry.subentries) == 0

    assert len(no_transmit) == 1
    assert bytes.fromhex(no_transmit[0][0])[4] == 4


async def test_the_command_is_replayed_as_the_remote_sent_it(
    hass: HomeAssistant, rfxtrx, captures
) -> None:
    """A receiver that counts presses reads a longer burst as several, which on
    a toggle undoes itself, so the only safe length is the remote's own."""
    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Fan light", CONF_TEST: False}
    )
    assert len(result["data"][CONF_EVENTS]) == 1
    assert bytes.fromhex(result["data"][CONF_EVENTS][0])[4] == 4


async def test_a_toggle_button_becomes_a_switch(
    hass: HomeAssistant, rfxtrx, captures, no_transmit
) -> None:
    """A remote whose button toggles has nowhere to keep the state otherwise."""
    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)

    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"name": "Fan light", CONF_KIND: KIND_SWITCH, CONF_TEST: False},
    )
    await hass.async_block_till_done()

    entity_id = "switch.rfxcom_commands_fan_light"
    assert hass.states.get("button.rfxcom_commands_fan_light") is None
    state = hass.states.get(entity_id)
    assert state is not None and state.state == "off"
    assert state.attributes["assumed_state"] is True

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == "on"
    assert len(no_transmit) == 1

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == "off"
    assert len(no_transmit) == 2

    # Asking again after a transmission was lost is the only way back, so it
    # must send rather than decide the state already matches.
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id}, blocking=True
    )
    assert len(no_transmit) == 3


async def test_editing_names_the_entity_and_can_fire_it(
    hass: HomeAssistant, rfxtrx, captures, no_transmit
) -> None:
    """Otherwise a command can only be tried out by hunting down its button."""
    entry = await setup_integration(hass)
    result = await start_learning(hass, entry)
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Fan light", CONF_TEST: False}
    )
    await hass.async_block_till_done()
    subentry_id = next(iter(entry.subentries))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "command"),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    assert (
        result["description_placeholders"]["entity_id"]
        == "button.rfxcom_commands_fan_light"
    )

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Fan light", CONF_TEST: True}
    )
    # Back on the form, and the command was transmitted rather than saved.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert len(no_transmit) == 1
    assert bytes.fromhex(no_transmit[0][0])[4] == 4


async def test_closing_the_dialog_stops_the_capture(
    hass: HomeAssistant, rfxtrx, monkeypatch
) -> None:
    """Home Assistant calls async_remove synchronously, so it cannot be async."""
    entry = await setup_integration(hass)

    # A listener that never produces anything, so the capture is still running.
    monkeypatch.setattr(
        config_flow, "RawListener", lambda hass: FakeListener(hass, packets=[])
    )
    monkeypatch.setattr(config_flow, "LEARN_TIMEOUT", 30)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "command"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    flow = hass.config_entries.subentries._progress[result["flow_id"]]
    assert flow._task is not None and not flow._task.done()

    hass.config_entries.subentries.async_abort(result["flow_id"])
    await asyncio.sleep(0)

    assert flow._task is None or flow._task.cancelled()
