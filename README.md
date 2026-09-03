# RFXCOM Commands

[![Validate](https://github.com/semichcsc-byte/rfxcom-commands/actions/workflows/validate.yml/badge.svg)](https://github.com/semichcsc-byte/rfxcom-commands/actions/workflows/validate.yml)
[![HACS custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

Learn 433 MHz remotes with an RFXCOM and get a Home Assistant button for each
one — including remotes the RFXCOM cannot decode.

## Why this exists

The RFXCOM integration works from a list of known protocols. Point a remote at
it that speaks something else and the best you get is an `Undecoded` event with
a couple of bytes in it, which is not enough to identify the button, let alone
reproduce it.

The device can do better. RFXtrx firmware has a raw mode that reports the
actual pulse timings, and a matching transmit packet that plays arbitrary
timings back. That is the same capture-and-replay trick a Broadlink uses, and
it works on remotes no protocol decoder recognises. It is just not exposed
anywhere: no Home Assistant UI, and the Python library the integration uses
throws the packets away before they reach an event.

This integration turns that raw mode into a learning flow. Press a button, get
an entity.

## What you need

- Home Assistant 2025.3 or newer
- The built-in **RFXCOM RFXtrx** integration already set up and working
- An RFXtrx433 (any variant) or RFX-433EMC

The serial port takes a single reader, so this integration never opens its own
connection. It borrows the one the core integration already has. If RFXCOM is
not set up, this will tell you so and stop.

## Install

### With HACS

1. Open **HACS**
2. Top right **⋮** → **Custom repositories**
3. Repository: `https://github.com/semichcsc-byte/rfxcom-commands`
4. Type: **Integration** → **Add**
5. Search HACS for *RFXCOM Commands*, open it, press **Download**
6. Restart Home Assistant

### Without HACS

Copy the `custom_components/rfxcom_commands` folder into your Home Assistant
`config/custom_components/` folder, so you end up with
`config/custom_components/rfxcom_commands/manifest.json`. Restart Home
Assistant.

## Set up

Go to **Settings → Devices & services → Add integration** and search for
**RFXCOM Commands**. There is nothing to configure — it finds your RFXCOM
integration by itself.

## Learn a command

On the integration's page, press **Learn a command**, then press and hold the
button on your remote for a second or two, within a few metres of the RFXCOM.

Holding matters. The integration only accepts a capture where the repeated
frames of a single press agree with each other. A remote repeats itself and
noise does not, so this is what separates a real capture from a stray
neighbour's doorbell. Too short a press and it will ask you to try again.

Then name the command and choose an area. Tick **Test before saving** to
transmit it and check it does the right thing; nothing is saved until you leave
that box unticked, so try as often as you like.

Each command you save becomes a button entity, grouped under the gateway.

### What happens behind the scenes

Raw reporting is only active when every receive protocol is enabled, so
learning switches them all on, captures, and then puts your previous selection
back. Your RFXCOM reloads twice in the process, which takes a few seconds.

Nothing about your normal protocol list changes permanently, and transmitting
does not depend on it — once a command is learned, its button works with your
usual protocols active.

## Edit a command

Open the command from the integration's page.

Renaming it or moving it to another area keeps the same entity, so dashboards,
automations and scripts carry on working. Tick **Capture the command again** to
recapture and keep everything else.

**Repeats** is how many times each press transmits the command, 1 to 10. Leave
it at 10 unless you have a reason not to. See below.

## If something does not work

**Nothing gets captured.** Hold the remote closer, within a metre or so, and
hold the button down rather than tapping it. Check the batteries. If your
remote is 315 MHz or 868 MHz, an RFXtrx433 will never hear it.

**The button works sometimes.** This is the common one. The RFXtrx transmits on
433.92 MHz and many remotes sit slightly off that — 433.83 is typical. Cheap
receivers have a narrow enough filter that the difference matters, and you get
an intermittent link. Repeats are already at the maximum, so the fix is
physical: move the RFXCOM closer to the device, or reorient its antenna.

**The button does nothing at all.** Learn it again. If a second capture
produces the same bits and still does nothing, the remote is probably using
rolling codes, which cannot be replayed by anyone.

**Learning says the frames disagree.** Something else transmitted at the same
moment. Just try again.

## What this cannot do

- **Read state.** A learned button transmits; it never knows what happened.
  For a toggle-only remote, Home Assistant cannot tell whether the light ended
  up on or off. Pair the button with an `input_boolean` if you need to track
  it, and accept that the two can drift apart.
- **Rolling codes.** Anything that changes its transmission between presses —
  most car remotes, most garage doors, KeeLoq — is replay-proof by design.
- **Hear its own transmissions.** Useful to know: pressing a button here will
  not fire an `rfxtrx_event`, so you cannot use that to confirm a send.

## How it works

The technical detail — the raw packet format, how the frames are decoded, and
how this was worked out — is in [docs/PROTOCOL.md](docs/PROTOCOL.md).

There is also a standalone script for capturing and decoding outside Home
Assistant, which is handy for debugging:

```
python3 tools/rfx_capture.py --log home-assistant.log
python3 tools/rfx_capture.py --port /dev/ttyUSB0 --seconds 30
```

## Licence

MIT
