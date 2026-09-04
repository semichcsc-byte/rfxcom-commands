"""Config and subentry flows: set up the gateway, then learn commands."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector
from homeassistant.util import slugify

from .const import (
    CONF_AREA_ID,
    CONF_BITS,
    CONF_EVENTS,
    CONF_KIND,
    CONF_PULSES,
    CONF_RELEARN,
    CONF_REPEATS,
    CONF_TEST,
    DEFAULT_REPEATS,
    DOMAIN,
    KIND_BUTTON,
    KIND_SWITCH,
    LEARN_TIMEOUT,
    MAX_PACKETS_PER_CAPTURE,
    MIN_FRAMES,
    POLL_INTERVAL,
    SUBENTRY_TYPE_COMMAND,
)
from .gateway import GatewayError, RawListener, async_send, find_entry
from .rawrf import (
    MAX_PACKETS,
    Command,
    RawRFError,
    build_packets,
    decode,
    is_last_packet,
)

_LOGGER = logging.getLogger(__name__)

KIND = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[KIND_BUTTON, KIND_SWITCH],
        mode=selector.SelectSelectorMode.LIST,
        translation_key=CONF_KIND,
    )
)


def _explain_failure(listener: RawListener, *, decode_error: str | None) -> str:
    """Say which of the several ways this can fail actually happened.

    "Nothing was received" was reported even when the receiver was busy, which
    sent the search off in entirely the wrong direction.
    """
    if listener.packets_seen == 0:
        return (
            "Nothing was received at all. Check that the remote is within a few "
            "metres of the RFXCOM and that it transmits on the same band."
        )
    if listener.raw_seen == 0:
        return (
            f"The RFXCOM received {listener.packets_seen} transmissions but "
            "reported none of them as raw pulse data, so it did not switch into "
            "raw mode. Its firmware may not support raw reporting, or the mode "
            "change did not take. Try again; if it keeps happening, this is a "
            "bug worth reporting."
        )
    detail = f" The last one failed because: {decode_error}" if decode_error else ""
    return (
        f"{listener.raw_seen} raw packets arrived but none of them formed a "
        f"readable transmission.{detail}"
    )


class RFXCOMCommandsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up against the RFXtrx that the core integration already owns."""

    VERSION = 1

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_COMMAND: CommandSubentryFlowHandler}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        try:
            find_entry(self.hass)
        except GatewayError:
            return self.async_abort(reason="no_gateway")

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

        return self.async_create_entry(title="RFXCOM Commands", data={})


class CommandSubentryFlowHandler(ConfigSubentryFlow):
    """Learn one command and turn it into a button."""

    def __init__(self) -> None:
        self._task: asyncio.Task[Command | None] | None = None
        self._command: Command | None = None
        self._error: str | None = None
        self._subentry_id: str | None = None
        self._defaults: dict[str, Any] = {}

    @property
    def _entry(self) -> ConfigEntry:
        return self._get_entry()

    @callback
    def async_remove(self) -> None:
        """Stop listening as soon as the dialog closes.

        Home Assistant calls this synchronously, so the task can only be
        cancelled, not awaited. That is enough: the listener restores the
        receiver from its own cleanup, which is shielded against the
        cancellation.
        """
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    # --- entry points ----------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Confirm before listening, so the capture window starts when ready."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))
        return await self.async_step_learn()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        self._subentry_id = subentry.subentry_id
        self._defaults = {
            "name": subentry.title,
            CONF_KIND: subentry.data.get(CONF_KIND, KIND_BUTTON),
            CONF_AREA_ID: subentry.data.get(CONF_AREA_ID),
        }

        if user_input is None:
            return self._show_details(step_id="reconfigure")

        if user_input.get(CONF_TEST):
            try:
                await async_send(
                    self.hass,
                    [
                        packet.hex()
                        for packet in build_packets(
                            list(subentry.data[CONF_PULSES]),
                            repeats=DEFAULT_REPEATS,
                        )
                    ],
                )
            except Exception as err:  # noqa: BLE001 - report and let them retry
                return self._show_details(
                    step_id="reconfigure",
                    user_input=user_input,
                    errors={"base": "send_failed"},
                    placeholders={"error": str(err)},
                )
            return self._show_details(step_id="reconfigure", user_input=user_input)

        if user_input.get(CONF_RELEARN):
            self._defaults |= {
                "name": user_input["name"],
                CONF_KIND: user_input.get(CONF_KIND, KIND_BUTTON),
                CONF_AREA_ID: user_input.get(CONF_AREA_ID),
            }
            return await self.async_step_learn()

        # Keep the captured pulses; only the presentation changed.
        return self._save(user_input, pulses=list(subentry.data[CONF_PULSES]),
                          bits=subentry.data.get(CONF_BITS, ""))

    # --- learning --------------------------------------------------------

    async def async_step_learn(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if self._task is None:
            self._task = self.hass.async_create_task(self._capture())

        if not self._task.done():
            return self.async_show_progress(
                step_id="learn",
                progress_action="learn",
                progress_task=self._task,
            )

        try:
            self._command = self._task.result()
        except (RawRFError, GatewayError) as err:
            self._error = str(err)
        except Exception as err:  # noqa: BLE001 - surfaced to the user verbatim
            _LOGGER.exception("Learning failed")
            self._error = str(err)
        finally:
            self._task = None

        if self._error or self._command is None:
            return self.async_show_progress_done(next_step_id="failed")
        return self.async_show_progress_done(next_step_id="name")

    async def _capture(self) -> Command | None:
        """Wait for one transmission that corroborates itself.

        Bursts are assembled strictly by packet index. A burst can never hold
        more than four packets, so anything that does not fit that shape is a
        lost or interleaved packet and the partial burst is dropped. Raw mode
        reports every RF transmission in earshot, so this loop has to stay
        bounded no matter how much traffic arrives.
        """
        async with RawListener(self.hass) as listener:
            deadline = time.monotonic() + LEARN_TIMEOUT
            burst: list[bytes] = []
            seen = 0
            last_decode_error: str | None = None

            while time.monotonic() < deadline:
                # Yield unconditionally. Awaiting a queue that already has an
                # item does not reach the event loop, so a fast enough stream
                # of packets would otherwise let this loop starve Home
                # Assistant for the whole capture window.
                await asyncio.sleep(0)

                packet = await listener.next_packet(timeout=POLL_INTERVAL)
                if packet is None:
                    continue

                seen += 1
                if seen > MAX_PACKETS_PER_CAPTURE:
                    break  # too busy to keep listening; give up

                index = packet[2]
                _LOGGER.debug(
                    "raw packet #%d: index=%d seq=%d last=%s pulses=%d",
                    seen, index, packet[3], bool(packet[4]), (len(packet) - 5) // 2,
                )
                if index == 0:
                    if burst:
                        _LOGGER.debug("  a new burst started before %d finished",
                                      len(burst))
                    burst = [packet]
                elif burst and index == len(burst) and len(burst) < MAX_PACKETS:
                    burst.append(packet)
                else:
                    # Out of order: another transmission cut across this one.
                    _LOGGER.debug(
                        "  out of order: expected index %d, dropping the burst",
                        len(burst),
                    )
                    burst = []
                    continue

                if not is_last_packet(packet):
                    continue

                try:
                    command = decode(burst, min_frames=MIN_FRAMES)
                except RawRFError as err:
                    _LOGGER.debug("  burst of %d packet(s) rejected: %s",
                                  len(burst), err)
                    last_decode_error = str(err)
                    burst = []
                    continue
                _LOGGER.debug("  accepted: %s", command.bits)
                return command

            _LOGGER.debug(
                "Capture over: %d packets from the receiver, %d of them raw",
                listener.packets_seen, listener.raw_seen,
            )
            self._error = _explain_failure(listener, decode_error=last_decode_error)
            return None

    async def async_step_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="failed",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "error": self._error
                    or (
                        "Nothing was received. Check that the remote is within a "
                        "few metres of the RFXCOM and that it transmits on the "
                        "same band."
                    )
                },
            )
        self._error = None
        self._command = None
        return await self.async_step_learn()

    # --- naming and saving ----------------------------------------------

    async def async_step_name(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        if user_input is None:
            return self._show_details(step_id="name")

        if user_input.get(CONF_TEST):
            assert self._command is not None
            try:
                await async_send(
                    self.hass,
                    self._command.events(repeats=DEFAULT_REPEATS),
                )
            except Exception as err:  # noqa: BLE001 - report and let them retry
                return self._show_details(
                    step_id="name", user_input=user_input, errors={"base": "send_failed"},
                    placeholders={"error": str(err)},
                )
            return self._show_details(step_id="name", user_input=user_input)

        assert self._command is not None
        return self._save(
            user_input,
            pulses=list(self._command.pulses),
            bits=self._command.bits,
        )

    def _show_details(
        self,
        *,
        step_id: str,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
        placeholders: dict[str, str] | None = None,
    ) -> SubentryFlowResult:
        current = {**self._defaults, **(user_input or {})}
        kind = current.get(CONF_KIND, KIND_BUTTON)
        entity_id = self._entity_id_for(current.get("name", ""), kind)
        fields: dict[Any, Any] = {}
        if step_id == "reconfigure":
            # Read-only, purely so the id can be selected and copied.
            fields[
                vol.Optional(CONF_ENTITY_ID, description={"suggested_value": entity_id})
            ] = selector.TextSelector(selector.TextSelectorConfig(read_only=True))
        fields |= {
            vol.Required("name", default=current.get("name", "")): str,
            vol.Required(CONF_KIND, default=kind): KIND,
            vol.Optional(
                CONF_AREA_ID, description={"suggested_value": current.get(CONF_AREA_ID)}
            ): selector.AreaSelector(),
        }
        fields[vol.Optional(CONF_TEST, default=False)] = bool
        if step_id == "reconfigure":
            fields[vol.Optional(CONF_RELEARN, default=False)] = bool

        described = {
            "bits": self._command.bits if self._command else "",
            "frames": str(self._command.frames_seen if self._command else 0),
            "entity_id": entity_id,
        }
        described |= placeholders or {}
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders=described,
        )

    def _entity_id_for(self, name: str, kind: str) -> str:
        """The entity id, so it is visible before anything is saved.

        A form cannot re-render as the name is typed, so on a new command this
        is only exact once the name is known -- after ticking Test, or on
        reconfigure. Until then it shows the shape the id will take.
        """
        registry = er.async_get(self.hass)
        if self._subentry_id is not None:
            for record in er.async_entries_for_config_entry(
                registry, self._entry.entry_id
            ):
                if record.unique_id == self._subentry_id and record.domain == kind:
                    return record.entity_id
        if not name:
            return f"{kind}.{slugify(self._entry.title)}_<name>"
        return registry.async_generate_entity_id(kind, f"{self._entry.title} {name}")

    def _save(
        self, user_input: dict[str, Any], *, pulses: list[int], bits: str
    ) -> SubentryFlowResult:
        # Always the firmware maximum. A lower count only makes a marginal link
        # worse, and sending the command a second time to compensate would read
        # as a second press -- which cancels itself out on a toggle.
        data = {
            CONF_PULSES: pulses,
            CONF_EVENTS: [
                p.hex() for p in build_packets(pulses, repeats=DEFAULT_REPEATS)
            ],
            CONF_REPEATS: DEFAULT_REPEATS,
            CONF_BITS: bits,
            CONF_KIND: user_input.get(CONF_KIND, KIND_BUTTON),
            CONF_AREA_ID: user_input.get(CONF_AREA_ID),
        }
        title = user_input["name"]

        if self._subentry_id is not None:
            self._apply_area(self._subentry_id, data[CONF_AREA_ID])
            return self.async_update_and_abort(
                self._entry,
                self._get_reconfigure_subentry(),
                title=title,
                data=data,
            )
        return self.async_create_entry(title=title, data=data)

    def _apply_area(self, subentry_id: str, area_id: str | None) -> None:
        """Move the existing button when the area changes on reconfigure."""
        registry = er.async_get(self.hass)
        for record in er.async_entries_for_config_entry(
            registry, self._entry.entry_id
        ):
            if record.unique_id == subentry_id:
                registry.async_update_entity(record.entity_id, area_id=area_id)
                break
