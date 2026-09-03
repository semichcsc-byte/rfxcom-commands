#!/usr/bin/env python3
"""Capture and decode RFXCOM raw RF commands outside Home Assistant.

Two sources:

  --log FILE    read `[RFXtrx] Recv:` lines from a Home Assistant log. Enable
                raw reporting first (all receive protocols) and set the
                `RFXtrx` logger to debug.

  --port DEV    talk to the device directly. Only works while nothing else
                holds the serial port, so stop Home Assistant or unplug it
                from that instance first.

Prints the decoded frame and the hex payload for the `rfxtrx.send` action.

    python3 rfx_capture.py --log home-assistant.log
    python3 rfx_capture.py --port /dev/ttyUSB0 --seconds 30
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "rfxcom_commands"))

from rawrf import (  # noqa: E402
    RawRFError,
    decode,
    is_last_packet,
    is_raw_packet,
)

RECV = re.compile(r"Recv:\s*((?:0x[0-9a-fA-F]{2}\s*)+)")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Every receive protocol, which is what unlocks raw reporting. Sent as a
# "set mode" command; see docs/PROTOCOL.md.
SET_ALL_PROTOCOLS = bytes(
    [0x0D, 0x00, 0x00, 0x00, 0x03, 0x53, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00]
)


def packets_from_log(path: Path) -> list[bytes]:
    packets = []
    for line in path.read_text(errors="replace").splitlines():
        match = RECV.search(ANSI.sub("", line))
        if not match:
            continue
        packet = bytes(int(x, 16) for x in match.group(1).split())
        if is_raw_packet(packet):
            packets.append(packet)
    return packets


def packets_from_port(port: str, seconds: float) -> list[bytes]:
    try:
        import serial  # noqa: PLC0415
    except ImportError:
        sys.exit("pyserial is needed for --port: pip install pyserial")
    import time  # noqa: PLC0415

    with serial.Serial(port, 38400, timeout=1) as link:
        link.write(bytes([0x0D] + [0x00] * 13))  # reset
        time.sleep(0.4)
        link.reset_input_buffer()
        link.write(SET_ALL_PROTOCOLS)
        time.sleep(0.4)
        link.write(b"\x0D\x00\x00\x03\x07" + bytes(9))  # start receiver
        time.sleep(0.4)
        link.reset_input_buffer()

        print(f"Listening for {seconds:.0f}s. Press and hold a button.", file=sys.stderr)
        packets: list[bytes] = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            head = link.read(1)
            if not head:
                continue
            length = head[0]
            if length < 4:
                continue
            body = link.read(length)
            if len(body) < length:
                continue
            packet = head + body
            if is_raw_packet(packet):
                packets.append(packet)
                if is_last_packet(packet):
                    print("  captured a burst", file=sys.stderr)
        return packets


def bursts(packets: list[bytes]) -> list[list[bytes]]:
    out, current = [], []
    for packet in packets:
        current.append(packet)
        if is_last_packet(packet):
            out.append(current)
            current = []
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--log", type=Path, help="Home Assistant log to read")
    source.add_argument("--port", help="serial device, e.g. /dev/ttyUSB0")
    parser.add_argument("--seconds", type=float, default=30, help="listen time for --port")
    parser.add_argument("--repeats", type=int, default=10, help="repeats in the transmit packet")
    args = parser.parse_args()

    packets = (
        packets_from_log(args.log) if args.log else packets_from_port(args.port, args.seconds)
    )
    groups = bursts(packets)
    if not groups:
        print("No raw (0x7F) packets found.", file=sys.stderr)
        print(
            "The device only reports them with every receive protocol enabled.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(groups)} burst(s) captured\n")
    seen: dict[str, int] = {}
    for index, burst in enumerate(groups):
        try:
            command = decode(burst)
        except RawRFError as err:
            print(f"burst {index}: {err}")
            continue
        seen[command.bits] = seen.get(command.bits, 0) + 1
        print(
            f"burst {index}: {command.bits}  "
            f"short={command.short}us long={command.long}us gap={command.gap}us "
            f"frames={command.frames_seen}"
        )
        if seen[command.bits] == 1:
            for number, event in enumerate(command.events(repeats=args.repeats), 1):
                label = f"  rfxtrx.send packet {number}" if len(command.events()) > 1 else "  rfxtrx.send"
                print(f"{label}: {event}")

    if len(seen) > 1:
        print("\nMore than one distinct command was captured; press one button at a time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
