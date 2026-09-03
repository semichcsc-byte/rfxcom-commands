# The RFXCOM raw RF packet (type 0x7F)

Notes from working out how to capture and replay a remote the RFXCOM cannot
decode. Everything here was verified against an RFX-433EMC, hardware 4.1,
firmware 1052, driving a ceiling fan light remote.

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
packets, because a packet holds at most 252 bytes. Reassemble in packet-index
order and you have the complete waveform.

### Enabling it

This is the part with no documentation. Raw reporting is off by default and
there is no setting for it — not in the RFXCOM web interface, not in Home
Assistant, not in pyRFXtrx.

What switches it on is the **receive protocol list**. Enabling every protocol
flips `msg3` of the status response from `0x00` to `0x80`, and the device starts
emitting `0x7F` packets instead of `0x03` undecoded ones.

Which individual bit is responsible was not narrowed down. Enabling everything
works, and it is what this integration does during learning.

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

**All the frames must decode identically.** This is the single most useful
check in the whole process. A remote repeats itself; interference does not. If
the frames disagree, another 433 MHz device transmitted during the capture and
the result should be thrown away rather than saved.

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

**Repeats matter more than they should.** At `repeats=5` this remote worked
intermittently — the light would turn on and then refuse to turn off. At
`repeats=10` it became reliable. The likely cause is frequency: a Broadlink
measured this remote at **433.83 MHz** while the RFXtrx transmits on **433.92**,
and 90 kHz is enough to sit at the edge of a cheap receiver's filter. The
RFXtrx only offers 433.92, 433.42 and 434.50, so there is nothing to tune.

If a command is unreliable at 10 repeats, the answer is antenna placement, not
configuration.

**The RFXtrx does not hear its own transmissions.** Convenient — no feedback
loop to guard against — but it also means a transmission cannot be confirmed by
watching for the event.

**A toggle button gives the same code every time.** Worth stating because it is
tempting to assume otherwise when a replay appears not to work. Captures of the
"turn on" press and the "turn off" press of the same toggle were byte-identical.
There is no alternating bit to reproduce; if a replay does nothing, the problem
is the link, not the code.

**Undecoded payloads cannot identify a button.** The 2-byte fragment was the
same for two different buttons on the same remote, and varied between presses of
one button. Raw mode is the only reliable way to tell buttons apart.

## References

- [RFXCOM SDK](http://www.rfxcom.com/) — official packet documentation
- [node-rfxcom](https://github.com/rfxcom/node-rfxcom) — `lib/rawtx.js` documents
  the transmit constraints; the rest of `lib/` is the clearest reference for the
  decoded packet types
- [pyRFXtrx](https://github.com/Danielhiversen/pyRFXtrx) — the library Home
  Assistant uses
