"""Decoding and encoding of RFXCOM raw RF packets (packet type 0x7F).

Deliberately free of Home Assistant imports: `tools/rfx_capture.py` reuses this
module, and it is the only part of the integration that is worth unit testing.

An RFXtrx in raw mode reports one button press as a *burst* of up to four
packets, each holding a slice of the pulse train:

    <len> 7F <index 0..3> <seq> <flag> <pulse pairs, 16-bit big-endian>

`flag` is 0 while more packets follow and 1 on the last one. On transmit the
same byte carries the repeat count (1..10) and is only set on the final packet.
Pulse durations are microseconds; a pulse train alternates mark and space.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import median

PACKET_TYPE_RAW = 0x7F

# The firmware caps a burst at ten repeats.
MAX_REPEATS = 10

# How the pulses carry the bits. Only the first is decoded.
ENCODING_PWM = "pwm"
ENCODING_PPM = "ppm"
ENCODING_MANCHESTER = "manchester"
ENCODING_UNKNOWN = "unknown"

# Limits imposed by the RFXtrx firmware.
MAX_PULSES_PER_PACKET = 124
MAX_PACKETS = 4
MAX_PULSES = MAX_PULSES_PER_PACKET * MAX_PACKETS
MAX_DURATION = 0xFFFF

# A frame separator is far longer than a data symbol. Three times the long
# symbol separates the two cleanly on every remote seen so far.
GAP_FACTOR = 3


class RawRFError(Exception):
    """Raised when a capture cannot be turned into a replayable command."""


@dataclass(frozen=True)
class Command:
    """One decoded remote button press."""

    pulses: tuple[int, ...]
    """Exactly one frame plus its trailing gap, normalised. Even length."""

    bits: str
    """The frame as bits, for display. Not used for transmitting."""

    short: int
    long: int
    gap: int
    frames_seen: int

    encoding: str = ENCODING_PWM
    """How the pulses carry the bits. See `classify`."""

    jitter_pct: float = 0.0
    """Mean deviation of the measured pulses from the two ideal lengths.

    A clean capture sits around a few percent. A high figure means the
    receiver is straining, which is what an off-frequency remote looks like.
    """

    def packets(self, repeats: int | None = None, seq: int = 0) -> list[bytes]:
        """Build the packets that replay this command."""
        return build_packets(self.pulses, repeats=repeats or self.repeats, seq=seq)

    def events(self, repeats: int | None = None, seq: int = 0) -> list[str]:
        """Same, as hex strings for the `rfxtrx.send` action."""
        return [p.hex() for p in self.packets(repeats=repeats, seq=seq)]

    @property
    def hex(self) -> str:
        """The bits as hex, which is what everyone else quotes codes in."""
        return f"0x{int(self.bits, 2):0{(len(self.bits) + 3) // 4}X}"

    @property
    def inverted(self) -> str:
        """The same pulses read at the opposite polarity.

        Which way round a protocol calls its bits is arbitrary, so a code that
        matches nothing often matches its own complement.
        """
        return self.bits.translate(str.maketrans("01", "10"))

    @property
    def frame_us(self) -> int:
        """How long one frame takes, without its trailing gap."""
        return sum(self.pulses[:-1])

    @property
    def burst_us(self) -> int:
        """How long the whole press occupies the air.

        Worth knowing: a receiver that coalesces a burst has to hold it for
        this long, and one that does not will count the frames as presses.
        """
        return (self.frame_us + self.gap) * self.frames_seen

    @property
    def trustworthy(self) -> bool:
        """Whether the bits mean anything.

        Only mark-length encoding is decoded. The others are reported so a
        wrong reading is not passed off as a right one.
        """
        return self.encoding == ENCODING_PWM

    @property
    def repeats(self) -> int:
        """How many times to send it: as many as the remote itself sent.

        A receiver that counts presses rather than coalescing a burst will see
        a longer burst as several presses, which on a toggle undoes itself. The
        only length known to work is the one the remote uses.
        """
        return min(max(self.frames_seen, 1), MAX_REPEATS)


def is_raw_packet(packet: bytes) -> bool:
    """Whether this is a raw RF packet we should collect."""
    return len(packet) >= 6 and packet[1] == PACKET_TYPE_RAW


def is_last_packet(packet: bytes) -> bool:
    """Whether this packet closes a burst."""
    return bool(packet[4])


def packet_pulses(packet: bytes) -> list[int]:
    """Pulse durations carried by one raw packet."""
    body = packet[5:]
    return [
        body[i] * 256 + body[i + 1] for i in range(0, len(body) - 1, 2)
    ]


def burst_pulses(burst: list[bytes]) -> list[int]:
    """Pulse durations of a whole burst, in order."""
    pulses: list[int] = []
    for packet in sorted(burst, key=lambda p: p[2]):
        pulses.extend(packet_pulses(packet))
    return pulses


def _symbol_durations(pulses: list[int]) -> tuple[int, int, int]:
    """Split the durations into short symbol, long symbol and gap.

    Two-means on a single dimension: seed with the extremes, assign, recentre.
    Simpler than it sounds because OOK only ever uses two symbol lengths, and
    far more robust than a fixed threshold across different remotes.
    """
    if len(pulses) < 4:
        raise RawRFError("Not enough pulses to work with")

    body = sorted(pulses)
    low, high = body[0], body[-1]
    if high <= low:
        raise RawRFError("Every pulse has the same length; this is not a signal")

    # Gaps would drag the clustering, so set them aside using a first guess.
    provisional = [p for p in body if p < (low + high) / 2]
    if not provisional:
        raise RawRFError("Could not identify a short symbol")
    guess_short = median(provisional)

    shorts = [p for p in body if p < guess_short * 2]
    longs = [p for p in body if guess_short * 2 <= p < guess_short * GAP_FACTOR * 2.5]
    if not shorts or not longs:
        raise RawRFError("Could not separate short and long pulses")

    short = int(median(shorts))
    long = int(median(longs))
    gaps = [p for p in body if p >= long * GAP_FACTOR]
    gap = int(median(gaps)) if gaps else long * 4
    return short, long, gap


def _split_frames(pulses: list[int], gap_threshold: int) -> list[list[int]]:
    """Cut the pulse train at the separators."""
    frames: list[list[int]] = []
    current: list[int] = []
    for pulse in pulses:
        if pulse >= gap_threshold:
            if current:
                frames.append(current)
            current = []
        else:
            current.append(pulse)
    if current:
        frames.append(current)
    return frames


def _frame_bits(frame: list[int], short: int, long: int) -> str:
    """Read the frame as bits, one bit per mark/space pair.

    The mark length carries the bit; a trailing odd pulse is the mark of the
    final bit, whose space is the frame separator.
    """
    midpoint = (short + long) / 2
    return "".join("1" if frame[i] > midpoint else "0" for i in range(0, len(frame), 2))


def classify(frame: list[int], short: int, long: int) -> str:
    """Work out how the pulses carry the bits.

    Only mark-length encoding is decoded here. Saying which of the others a
    capture is beats decoding it as if it were the one we handle, which is
    what produced confident nonsense before.
    """
    midpoint = (short + long) / 2
    marks = [p > midpoint for p in frame[0::2]]
    spaces = [p > midpoint for p in frame[1::2]]

    if len(set(marks)) > 1 and len(set(spaces)) > 1:
        # Both vary. Mark-length encoding keeps every symbol the same width,
        # so a constant mark+space says the bit is in the ratio, not the gap.
        widths = {
            frame[i] + frame[i + 1] for i in range(0, len(frame) - 1, 2)
        }
        spread = (max(widths) - min(widths)) / max(widths) if widths else 1.0
        return ENCODING_PWM if spread < 0.25 else ENCODING_MANCHESTER
    if len(set(marks)) == 1 and len(set(spaces)) > 1:
        return ENCODING_PPM  # fixed pulse, the gap carries the bit
    return ENCODING_UNKNOWN


def _jitter_pct(frame: list[int], short: int, long: int) -> float:
    """How far the measured pulses sit from the two ideal lengths."""
    midpoint = (short + long) / 2
    off = [abs(p - (long if p > midpoint else short)) / (long if p > midpoint else short)
           for p in frame]
    return round(100 * sum(off) / len(off), 1) if off else 0.0


def decode(burst: list[bytes], *, min_frames: int = 3) -> Command:
    """Turn one captured burst into a replayable command.

    Requires the repeated frames within the burst to agree, which is what makes
    a capture trustworthy: a remote repeats itself, noise does not. A single
    press carries several frames, so this needs no second press.
    """
    pulses = burst_pulses(burst)
    short, long, gap = _symbol_durations(pulses)
    frames = _split_frames(pulses, gap_threshold=long * GAP_FACTOR)

    # The first frame is usually clipped: capture starts mid-transmission.
    complete = [f for f in frames if len(f) > 4]
    if len(complete) < min_frames:
        raise RawRFError(
            f"Only {len(complete)} usable frame(s) captured; the transmission "
            "was cut short"
        )

    # The longest length that repeats enough, not the most common one. A lost
    # pulse shortens a frame and no amount of noise lengthens one consistently,
    # so a short reading must never be able to outvote the full-length one --
    # which is what happened in the field, silently, because ties between
    # equally common lengths were broken by set iteration order.
    counts = Counter(len(frame) for frame in complete)
    qualifying = [length for length, count in counts.items() if count >= min_frames]
    if not qualifying:
        raise RawRFError(
            "The captured frames disagree in length, so none of them is "
            "corroborated. Try again."
        )
    reference_length = max(qualifying)
    candidates = [f for f in complete if len(f) == reference_length]

    decoded = {_frame_bits(f, short, long) for f in candidates}
    if len(decoded) != 1:
        raise RawRFError(
            "The captured frames do not agree; another 433 MHz device probably "
            "transmitted at the same time. Try again."
        )
    bits = decoded.pop()

    # Rebuild from the ideal symbol lengths rather than replaying the measured
    # ones: reception jitter is not worth reproducing, and a single mis-read
    # pulse would otherwise be baked into every transmission.
    reference = candidates[0]
    midpoint = (short + long) / 2
    normalised = [long if p > midpoint else short for p in reference]
    normalised.append(gap)
    if len(normalised) % 2:
        raise RawRFError("Captured an odd number of pulses; try again")
    if len(normalised) > MAX_PULSES:
        raise RawRFError(
            f"Frame needs {len(normalised)} pulses, more than the {MAX_PULSES} "
            "the RFXtrx can transmit"
        )

    return Command(
        pulses=tuple(normalised),
        bits=bits,
        short=short,
        long=long,
        gap=gap,
        frames_seen=len(candidates),
        encoding=classify(reference, short, long),
        jitter_pct=_jitter_pct(reference, short, long),
    )


def build_packets(
    pulses: tuple[int, ...] | list[int], *, repeats: int = 10, seq: int = 0
) -> list[bytes]:
    """Build the raw transmit packets for a pulse train."""
    if not 1 <= repeats <= 10:
        raise RawRFError("repeats must be between 1 and 10")
    if len(pulses) % 2:
        raise RawRFError("A pulse train must have an even number of pulses")
    if not pulses or len(pulses) > MAX_PULSES:
        raise RawRFError(f"A pulse train must hold 1 to {MAX_PULSES} pulses")
    if any(not 1 <= p <= MAX_DURATION for p in pulses):
        raise RawRFError(f"Pulse durations must be 1 to {MAX_DURATION} microseconds")

    packets: list[bytes] = []
    chunks = [
        pulses[i : i + MAX_PULSES_PER_PACKET]
        for i in range(0, len(pulses), MAX_PULSES_PER_PACKET)
    ]
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        body = bytearray([repeats if last else 0])
        for pulse in chunk:
            body += bytes((pulse // 256, pulse % 256))
        packets.append(bytes([len(body) + 3, PACKET_TYPE_RAW, index, seq & 0xFF]) + body)
    return packets
