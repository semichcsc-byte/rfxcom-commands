"""Tests for the raw RF decoder.

The fixture is a real burst captured from a ceiling fan light remote, kept
verbatim so that a refactor which quietly changes the decoding is caught.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "rfxcom_commands")
)

from rawrf import (  # noqa: E402
    MAX_PULSES,
    RawRFError,
    build_packets,
    decode,
    is_last_packet,
    is_raw_packet,
    packet_pulses,
)

# One press, two packets, four repeated frames.
from fixtures import CAPTURE, EXPECTED_BITS  # noqa: E402

class TestCapture(unittest.TestCase):
    def test_recognises_raw_packets(self) -> None:
        for packet in CAPTURE:
            self.assertTrue(is_raw_packet(packet))
        self.assertFalse(is_last_packet(CAPTURE[0]))
        self.assertTrue(is_last_packet(CAPTURE[1]))

    def test_rejects_other_packet_types(self) -> None:
        # An "undecoded" packet, which is what the device reports when raw
        # reporting is off.
        self.assertFalse(is_raw_packet(bytes.fromhex("05030c2405f8")))

    def test_pulses_are_pairs_of_bytes(self) -> None:
        self.assertEqual(len(packet_pulses(CAPTURE[0])), (len(CAPTURE[0]) - 5) // 2)


class TestDecode(unittest.TestCase):
    def setUp(self) -> None:
        self.command = decode(CAPTURE)

    def test_bits(self) -> None:
        self.assertEqual(self.command.bits, EXPECTED_BITS)

    def test_symbol_durations(self) -> None:
        # OOK at roughly 1:3, with a frame separator far above both.
        self.assertAlmostEqual(self.command.short, 378, delta=15)
        self.assertAlmostEqual(self.command.long, 1135, delta=25)
        self.assertGreater(self.command.gap, self.command.long * 3)

    def test_pulses_are_normalised_to_two_symbols(self) -> None:
        body = self.command.pulses[:-1]
        self.assertEqual(set(body), {self.command.short, self.command.long})

    def test_pulse_train_is_transmittable(self) -> None:
        self.assertEqual(len(self.command.pulses) % 2, 0)
        self.assertLessEqual(len(self.command.pulses), MAX_PULSES)
        self.assertEqual(self.command.pulses[-1], self.command.gap)

    def test_agreeing_frames_are_required(self) -> None:
        with self.assertRaises(RawRFError):
            decode(CAPTURE, min_frames=99)


class TestBuildPackets(unittest.TestCase):
    def test_single_packet_layout(self) -> None:
        pulses = [380, 1135] * 30
        (packet,) = build_packets(pulses, repeats=10, seq=0)
        self.assertEqual(packet[0], len(packet) - 1)  # length excludes itself
        self.assertEqual(packet[1], 0x7F)
        self.assertEqual(packet[2], 0)  # first packet of the burst
        self.assertEqual(packet[4], 10)  # repeats, set on the last packet
        self.assertEqual(packet[5:7], bytes([0x01, 0x7C]))  # 380 big-endian

    def test_splits_across_packets_and_flags_only_the_last(self) -> None:
        packets = build_packets([380, 1135] * 130, repeats=5)
        self.assertEqual(len(packets), 3)
        self.assertEqual([p[2] for p in packets], [0, 1, 2])
        self.assertEqual([p[4] for p in packets[:-1]], [0, 0])
        self.assertEqual(packets[-1][4], 5)

    def test_events_replay_the_captured_frame(self) -> None:
        """The hex handed to `rfxtrx.send` must carry the pulses we decoded."""
        command = decode(CAPTURE)
        (event,) = command.events(repeats=10)
        packet = bytes.fromhex(event)
        self.assertEqual(packet_pulses(packet), list(command.pulses))

    def test_rejects_impossible_input(self) -> None:
        with self.assertRaises(RawRFError):
            build_packets([380, 1135], repeats=0)
        with self.assertRaises(RawRFError):
            build_packets([380], repeats=5)  # odd count
        with self.assertRaises(RawRFError):
            build_packets([380, 70000], repeats=5)  # will not fit in 16 bits
        with self.assertRaises(RawRFError):
            build_packets([380, 1135] * (MAX_PULSES // 2 + 1), repeats=5)


if __name__ == "__main__":
    unittest.main()
