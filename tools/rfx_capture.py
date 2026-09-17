#!/usr/bin/env python3
"""Read what an RFXCOM hears, the way RFXmngr does, outside Home Assistant.

For every packet received it prints the packet type and the **HA code** — the
hex string that goes into the `event_code` field of the RFXCOM integration,
which is exactly what RFXmngr shows you.

Raw RF packets (type 0x7F) get further treatment: those carry pulse timings
rather than a decoded message, so the bursts are reassembled into a command and
turned into a payload for the `rfxtrx.send` action. That is the only route for
a remote the firmware cannot decode, and it needs every receive protocol
enabled first.

Two sources:

  --log FILE    read `[RFXtrx] Recv:` lines from a Home Assistant log. Set the
                `RFXtrx` logger to debug first:

                    logger:
                      logs:
                        RFXtrx: debug

    --port DEV    talk to the device directly, like RFXmngr. Needs exclusive
                                access to that serial port. A receiver moved to another
                                computer does not require stopping Home Assistant.

    --save-log FILE  preserve every packet, including rejected raw captures.

    python3 rfx_capture.py --log home-assistant.log
    python3 rfx_capture.py --port /dev/ttyUSB0 --seconds 30 --save-log capture.log
    python3 rfx_capture.py --log home-assistant.log --raw-only
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TextIO

sys.path.append(
    str(Path(__file__).resolve().parent.parent / "custom_components" / "rfxcom_commands")
)

from packets import ha_code, packet_name
from rawrf import (  # noqa: E402
    MAX_PACKETS,
    RawRFError,
    decode,
    is_last_packet,
    is_raw_packet,
)

RECV = re.compile(r"Recv:\s*((?:0x[0-9a-fA-F]{2}\s*)+)")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Noise while listening: the device's own replies, not received RF.
INTERFACE_TYPES = {0x00, 0x01, 0x02}

# Every receive protocol, which is what unlocks raw reporting.
# See docs/PROTOCOL.md for why this is the switch.
SET_ALL_PROTOCOLS = bytes(
    [0x0D, 0x00, 0x00, 0x00, 0x03, 0x53, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00]
)


def packets_from_log(path: Path) -> list[bytes]:
    packets = []
    for line in path.read_text(errors="replace").splitlines():
        match = RECV.search(ANSI.sub("", line))
        if match:
            packets.append(bytes(int(x, 16) for x in match.group(1).split()))
    return packets


def read_packet(link, deadline: float) -> bytes | None:
    head = link.read(1)
    if not head:
        return None
    body = bytearray()
    while len(body) < head[0]:
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out reading a serial packet")
        body.extend(link.read(head[0] - len(body)))
    return head + body


def receiver_mode(link) -> bytes:
    link.write(b"\x0D\x00\x00\x01\x02" + bytes(9))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        packet = read_packet(link, deadline)
        if (
            packet is not None and len(packet) >= 14
            and packet[1] == 0x01 and packet[3:5] == b"\x01\x02"
        ):
            mode = bytearray(14)
            mode[0] = 0x0D
            mode[4] = 0x03
            mode[5] = packet[5]
            mode[6] = packet[13]
            mode[7:11] = packet[7:11]
            return bytes(mode)
    raise RuntimeError("No receiver status; cannot safely preserve its mode")


def packets_from_port(
    port: str, seconds: float, enable_raw: bool, save_log: TextIO | None = None
) -> list[bytes]:
    try:
        import serial  # noqa: PLC0415
    except ImportError:
        sys.exit("pyserial is needed for --port: pip install pyserial")
    with serial.Serial(port, 38400, timeout=0.2, write_timeout=2) as link:
        previous = receiver_mode(link)
        try:
            if enable_raw:
                raw_mode = bytearray(SET_ALL_PROTOCOLS)
                raw_mode[5:7] = previous[5:7]
                link.write(raw_mode)
                reported = receiver_mode(link)
                if reported[5] != previous[5]:
                    raise RuntimeError("Receiver unexpectedly changed band")
                print(
                    f"Reported receive mode: {reported[7:11].hex()}; "
                    "RAW reception is confirmed only by 0x7F packets.",
                    file=sys.stderr,
                )
            link.write(b"\x0D\x00\x00\x03\x07" + bytes(9))
            print(f"Listening for {seconds:.0f}s. Press a button.", file=sys.stderr)
            packets: list[bytes] = []
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                packet = read_packet(link, deadline)
                if packet is None or len(packet) < 5:
                    continue
                if save_log is not None:
                    save_log.write("[RFXtrx] Recv: " + " ".join(
                        f"0x{value:02x}" for value in packet
                    ) + "\n")
                    save_log.flush()
                packets.append(packet)
                if len(packets) >= 2000:
                    print("Packet limit reached; stopping capture.", file=sys.stderr)
                    break
            return packets
        finally:
            if enable_raw:
                link.write(previous)
                if receiver_mode(link) != previous:
                    raise RuntimeError("Receiver mode restoration was not confirmed")
                print("Previous receiver mode restored and verified.", file=sys.stderr)


def raw_bursts(packets: list[bytes]) -> list[list[bytes]]:
    """Group raw packets into bursts, by packet index within the burst."""
    bursts, current = [], []
    for packet in packets:
        if not is_raw_packet(packet):
            continue
        index = packet[2]
        if index == 0:
            current = [packet]
        elif current and index == len(current) and len(current) < MAX_PACKETS:
            current.append(packet)
        else:
            current = []
            continue
        if is_last_packet(packet):
            bursts.append(current)
            current = []
    return bursts


def _fields(packet: bytes) -> list[str]:
    """Per-field detail, the way RFXmngr breaks a packet down.

    pyRFXtrx already knows every packet layout, so use it when it is installed
    rather than reimplementing the SDK. Without it the packet type and the HA
    code still get reported, which is the part that matters.
    """
    try:
        import RFXtrx  # noqa: PLC0415
    except ImportError:
        return []

    try:
        event = RFXtrx.RFXtrxTransport.parse(bytearray(packet))
    except Exception:  # noqa: BLE001 - a packet we cannot read is not fatal
        return []
    if event is None or getattr(event, "device", None) is None:
        return []

    device = event.device
    lines = [
        f"Sequence nbr : {packet[3]}",
        f"Type         : {device.type_string}",
        f"Id           : {device.id_string}",
    ]
    for name, value in sorted(getattr(event, "values", {}).items()):
        lines.append(f"{name:<13}: {value}")
    return lines


def report_decoded(packets: list[bytes], show_interface: bool) -> None:
    """List each distinct decoded packet with its HA code, as RFXmngr does."""
    seen: dict[str, tuple[bytes, int]] = {}
    for packet in packets:
        if is_raw_packet(packet):
            continue
        if not show_interface and packet[1] in INTERFACE_TYPES:
            continue
        # The sequence number changes every packet, so key on the rest.
        key = packet[:3].hex() + packet[4:].hex()
        found, count = seen.get(key, (packet, 0))
        seen[key] = (found, count + 1)

    if not seen:
        return
    print(f"Decoded packets ({len(seen)} distinct)\n")
    undecoded = 0
    for found, count in seen.values():
        print(f"  {packet_name(found)}  x{count}")
        print(f"    HA code: {ha_code(found)}")
        for line in _fields(found):
            print(f"      {line}")
        if found[1] == 0x03:
            undecoded += 1
    print()
    if undecoded > 1:
        print(
            f"{undecoded} distinct Undecoded codes. The receiver heard something\n"
            "it cannot decode, and the few bytes it reports are not stable enough\n"
            "to tell buttons apart — pressing one button can produce several of\n"
            "these. Use the raw capture below instead.\n"
        )


def report_raw(packets: list[bytes], repeats: int) -> None:
    """Reassemble raw bursts into replayable commands."""
    bursts = raw_bursts(packets)
    if not bursts:
        return

    print(f"Raw RF ({len(bursts)} burst(s))\n")
    seen: dict[str, int] = {}
    for index, burst in enumerate(bursts):
        try:
            command = decode(burst)
        except RawRFError as err:
            print(f"  burst {index}: {err}")
            continue
        first = command.bits not in seen
        seen[command.bits] = seen.get(command.bits, 0) + 1
        print(
            f"  burst {index}: {command.bits}"
            f"  short={command.short}us long={command.long}us"
            f" gap={command.gap}us frames={command.frames_seen}"
        )
        if first:
            events = command.events(repeats=repeats)
            for number, event in enumerate(events, 1):
                label = "    rfxtrx.send"
                if len(events) > 1:
                    label += f" packet {number}"
                print(f"{label}: {event}")
    print()
    if len(seen) > 1:
        print("More than one distinct command was captured; press one button at a time.\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--log", type=Path, help="Home Assistant log to read")
    source.add_argument("--port", help="serial device, e.g. /dev/ttyUSB0")
    parser.add_argument("--seconds", type=float, default=30, help="listen time for --port")
    parser.add_argument("--repeats", type=int, default=10, help="repeats in the transmit packet")
    parser.add_argument("--raw-only", action="store_true", help="skip the decoded packets")
    parser.add_argument("--save-log", type=Path, help="save every serial packet to a new log file")
    parser.add_argument(
        "--no-raw-mode",
        action="store_true",
        help="with --port, leave the protocol selection alone",
    )
    parser.add_argument(
        "--interface", action="store_true", help="also show the device's own replies"
    )
    args = parser.parse_args()

    if args.save_log and not args.port:
        parser.error("--save-log requires --port")
    if not 0 < args.seconds <= 120:
        parser.error("--seconds must be between 0 and 120")
    with (
        args.save_log.open("x", encoding="ascii")
        if args.save_log else nullcontext(None)
    ) as output:
        packets = (
            packets_from_log(args.log)
            if args.log
            else packets_from_port(args.port, args.seconds, not args.no_raw_mode, output)
        )
    if not packets:
        print("No packets found.", file=sys.stderr)
        return 1

    print(f"{len(packets)} packet(s)\n")
    if not args.raw_only:
        report_decoded(packets, show_interface=args.interface)
    report_raw(packets, repeats=args.repeats)

    if not any(is_raw_packet(p) for p in packets):
        print(
            "No raw (0x7F) packets. The device only reports those with every "
            "receive protocol enabled, which is what a remote the firmware "
            "cannot decode needs.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
