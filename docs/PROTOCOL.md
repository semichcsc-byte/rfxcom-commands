# The RFXCOM raw RF packet (type 0x7F)

Implementation notes and measured behavior from an RFX-433EMC, hardware 4.1.
Tests include a ceiling-fan light and separate fan ON/OFF commands. Historical
notes recorded firmware 1052; the later USB status reported firmware byte 0x34.
Do not treat either value as a universal firmware requirement or assume that
every receiver variant implements RAW mode identically.

## The problem

The RFXtrx decodes a fixed set of protocols. A remote outside that set produces
an **Undecoded** packet (type `0x03`):

```
05 03 0C 24 05 F8
│  │  │  │  └──┴── payload
│  │  │  └──────── sequence number
│  │  └─────────── subtype 0x0C = "NEC"
│  └────────────── packet type 0x03 = undecoded
└───────────────── length
```

Two bytes of payload. A real frame is far longer than that, so this is a
fragment, not the command. Worse, the fragment is not stable: the same button
pressed repeatedly produced `05f8`, `0578` and `05a8` on different presses —
single-bit reception errors in a value too short to have any redundancy.

It is enough to notice that *a* button was pressed. It is not enough to tell
which button, and nowhere near enough to reproduce the signal.

A dead end for control, and the reason to look for something else.

## Raw mode

RFXtrx firmware can report the pulse train itself, as packet type `0x7F`:

```
EC 7F 01 30 01 01 77 04 59 01 7F 04 72 ...
│  │  │  │  │  └──┴──┴──┴──┴──┴──┴───── pulse durations, 16-bit big-endian, µs
│  │  │  │  └───────────────────────── 1 = last packet of the burst, 0 = more follow
│  │  │  └──────────────────────────── sequence number
│  │  └─────────────────────────────── packet index within the burst, 0 to 3
│  └────────────────────────────────── packet type 0x7F = raw
└───────────────────────────────────── length (excludes itself)
```

One button press is a **burst**: the pulse train is split across up to four
packets, each carrying up to 124 pulse durations (253 bytes including the
length byte). Reassemble in packet-index order to obtain the captured portion
of the waveform; the receiver may reach capacity before the physical press ends.

The fan capture in `tests/fan_remote_capture.txt` (17 September 2026) contains
four groups of four full packets, all with flag zero. Index 3 therefore also
closes a capacity-limited capture; waiting only for a nonzero flag discards it.
The final RF frame may be partial, so frame agreement is still required.

All four presses contain eight agreeing 30-bit frames. Their codes alternate
`000001001011011001001111010000` and `000001001011011001001100100011`.
USB transmission tests on the same day, using eight repeats and confirmed by
the user watching the fan, identified the first code as ON and the second as
OFF: A left the already-running fan on, B stopped it, and A started it again.
All three sends received transmit-OK acknowledgements, and the receiver mode
was verified unchanged after each send. This confirms those observed actions,
not long-term RF reliability. Save separate ON and OFF buttons rather than
using a single-code toggle switch for this remote.
The shorter-gap captures exposed a clustering bug: long pulses slightly
outnumbered short pulses, making the overall median a long pulse. Cluster the
two symbol lengths before selecting the short duration; otherwise the roughly
6100 us separators are mistaken for long symbols and all frames merge into one.

### Enabling it

This is the part with no documentation. Raw reporting is off by default and
there is no setting for it — not in the RFXCOM web interface, not in Home
Assistant, not in pyRFXtrx.

The integration requests every protocol known to pyRFXtrx in the **receive
protocol list**. Observed status bits vary with firmware: during the USB test,
both `ffffff03` and `ffffffff` requests were reported back as `80400000`, yet
RAW packets arrived. A status-mask mismatch alone does not prove failure;
receiving `0x7F` packets is the evidence that RAW reception is active.

Which individual bit is responsible was not narrowed down. Enabling everything
works, and it is what this integration does during learning.

The connection's original band, output power and protocol settings are preserved
for restoration. Offline get-status reads must match command 0x02 and the
requested sequence; set-mode also returns interface responses. The USB tool
checks exact restoration at exit. The HA gateway uses the already-open
connection and attempts restoration; it does not verify the returned mode mask.
If the transport disappears, restoration can fail and is logged.

Two consequences worth knowing:

- **It replaces undecoded reporting.** You get `0x7F` *instead of* `0x03`, not
  as well. Any automation keyed on `packet_type: 3` stops firing.
- **Transmit does not need it.** Raw mode is a receive setting. Once a command
  is captured, you can restore your normal protocol list and still transmit raw
  packets. This is what lets the integration leave your configuration alone.

### Why Home Assistant never sees these packets

`RFXtrxTransport.parse()` in pyRFXtrx calls `lowlevel.parse(data)`, which
returns `None` for any packet type it has no class for — and it has no class for
`0x7F`. `Connect._connect_internal` then skips the callback, so no
`rfxtrx_event` is ever fired and the bytes are gone.

They are visible in the debug log, because `_receive_packet` logs the raw bytes
before parsing:

```yaml
logger:
  logs:
    RFXtrx: debug
```

For a real integration, logs are not an interface. The hook this project uses
instead: `_receive_packet` calls `self.parse(pkt)`, so assigning `parse` on the
*instance* shadows the class method and sees every packet before pyRFXtrx
discards it.

## Decoding a capture

The example below is a ceiling fan light remote. Yours will have different
numbers; the shape is usually the same.

### 1. Reassemble the burst

Concatenate the pulse durations from each packet of the burst, in packet-index
order. This one gives 360 values.

### 2. Find the symbol lengths

The durations cluster hard:

```
~378 µs   short symbol
~1135 µs  long symbol
~6800 µs  frame separator
11000 µs  end of burst
```

On/off keying with roughly a 1:3 ratio. Anything above about three times the
long symbol is a separator rather than data.

### 3. Split into frames and check they agree

Cut at the separators. A press repeats the same frame several times:

```
burst of 360 pulses → 6 frames of 59 pulses
```

The decoder requires at least three usable frames. It selects the longest frame
length appearing at least three times, then compares every normalized mark and
space at that length. Those candidates must agree. Partial frames of other
lengths do not vote. Disagreement can mean interference, reception errors or
unsupported framing; it does not identify the cause by itself.

### 4. Read the bits

Pulses alternate mark and space. The mark length carries the bit:

```
long mark, short space   → 1
short mark, long space   → 0
```

59 pulses is 29 whole pairs plus a trailing mark, whose space is the frame
separator — so 30 bits:

```
000001001011011001000101101010
```

### 5. Normalise

Rebuild the frame from the ideal symbol lengths rather than replaying the
measured ones. Reception jitter is not worth reproducing, and a single
mis-measured pulse would otherwise be baked into every future transmission.

Append the separator so the frame tiles cleanly when repeated. The result must
have an even number of pulses.

PWM payload bits are derived from mark lengths. Non-PWM signals instead use a
signature of all normalized marks and spaces; this distinguishes patterns but
is not a protocol decoder. The encoding label is heuristic. Protocols requiring
other timings or framing can still be unsupported.

## Transmitting

The transmit packet has the same layout, with one difference: the byte that
flags "last packet" on receive carries the **repeat count** on transmit, 1 to
10, and is only set on the final packet.

```
7C 7F 00 00 0A 01 7C 04 6F 01 7C 04 6F ...
│  │  │  │  │  └──┴──┴──┴───────────────── pulses
│  │  │  │  └───────────────────────────── repeats = 10
│  │  │  └──────────────────────────────── sequence number (any value)
│  │  └─────────────────────────────────── packet index
│  └────────────────────────────────────── raw
└───────────────────────────────────────── length
```

Constraints, from the firmware:

- at most **124 pulses per packet**, **4 packets**, so 496 pulses total
- the pulse count must be **even**
- each duration must fit in 16 bits, 1 to 65535
- repeats between 1 and 10

Send it with the built-in action:

```yaml
action: rfxtrx.send
data:
  event: "7c7f00000a017c046f017c046f..."
```

A successful transmission is acknowledged:

```
04 02 01 00 00
│  │  │  │  └── 0x00 = ACK, transmit OK
│  │  │  └───── sequence number
│  │  └──────── subtype
│  └─────────── packet type 0x02 = transmit response
└────────────── length
```

The ACK confirms the RFXtrx transmitted. It says nothing about whether anything
received it.

## Things that cost time

**Repeat behavior is appliance-specific.** Early light-toggle tests improved
when moving from five to ten repeats. Later fan ON/OFF tests worked with eight.
The integration now uses the number of agreeing captured frames, capped at ten.
Increasing repeats is not a universal fix and can cause multiple actions.
A Broadlink measured this remote near 433.83 MHz; the RFXtrx reports a 433.92 MHz
band. Frequency offset is one possible explanation for marginal reception,
not a confirmed diagnosis. RAW packets contain no carrier measurement.

**The RFXtrx does not hear its own transmissions.** Convenient — no feedback
loop to guard against — but it also means a transmission cannot be confirmed by
watching for the event.

**One physical button does not imply one code.** The earlier light toggle sent
the same code for both actions. The later fan button alternated separate ON and
OFF commands, confirmed by physical replay. Do not infer toggle semantics from
the button layout or classify changing codes as rolling codes without evidence.

**Undecoded payloads cannot identify a button.** The 2-byte fragment was the
same for two different buttons on the same remote, and varied between presses of
one button. Raw mode is the only reliable way to tell buttons apart.

## Concurrency and capture limits

RAW reception uses a thread-safe queue of 64 packets on the reader thread.
No per-packet callback is scheduled on Home Assistant's event loop. The consumer
checks the queue asynchronously; overflow aborts capture with an explicit error
and enters protocol restoration. A stale parser hook cannot enqueue after exit.

Capture processing yields to the event loop each iteration, accepts at most
2,000 packets and keeps at most four packets in a partial burst. Scanner duration
is capped at 600 seconds; learning uses 20 seconds and watch at most 120 seconds.
All capture entry points share an exclusive lock for the HA instance. Mode writes
and learned transmissions share a send lock; complete multipart commands cannot
interleave with other sends from this integration. External native service calls
do not participate in that command-level lock.

Cancellation waits for an in-flight write before restoring settings or releasing
the lock, since cancelling an executor future cannot stop a physical serial
write. A permanently stalled transport remains a recovery risk. These controls
and regression tests address known defects; they do not establish the cause of
all previously observed Core freezes.

## Demo screenshots

The following captures use the real HA 2026.9 frontend and an isolated test
instance, not a production installation. The learning flow receives the first
ON burst from [the recorded fixture](../tests/fan_remote_capture.txt). Its eight
agreeing frames yield code `0x012D93D0`:

[![Real learning form displaying the recorded ON command and eight agreeing frames](images/learn-command.png)](images/learn-command.png)

The scanner example consumes the recorded ON and OFF bursts. It shows eight
RAW packets in total, two signatures and eight agreeing frames for the final
OFF code `0x012D9323`. The scanner has stopped. Receiver band is simulated status
metadata; none of these screenshots demonstrate live radio performance.

[![Stopped scanner with recorded fan RF diagnostics and separate ON and OFF buttons](images/scanner.png)](images/scanner.png)

See [the user manual](../README.md#regenerating-screenshots) for reproducible
generation commands, and [the integration-page screenshot](images/commands.png)
for the resulting saved commands. No production credentials or devices are used.

## References

- [RFXCOM SDK](http://www.rfxcom.com/) — official packet documentation
- [node-rfxcom](https://github.com/rfxcom/node-rfxcom) — `lib/rawtx.js` documents
  the transmit constraints; the rest of `lib/` is the clearest reference for the
  decoded packet types
- [pyRFXtrx](https://github.com/Danielhiversen/pyRFXtrx) — the library Home
  Assistant uses
