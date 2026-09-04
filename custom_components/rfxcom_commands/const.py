"""Constants for the RFXCOM Commands integration."""

DOMAIN = "rfxcom_commands"

RFXTRX_DOMAIN = "rfxtrx"
RFXTRX_DATA_OBJECT = "rfxobject"
RFXTRX_SERVICE_SEND = "send"

CONF_AREA_ID = "area_id"
CONF_AGAIN = "again"
CONF_EVENTS = "events"
CONF_BITS = "bits"
CONF_KIND = "kind"
CONF_PULSES = "pulses"
CONF_REPEATS = "repeats"
CONF_RELEARN = "relearn"
CONF_TEST = "test"

# A remote whose button toggles cannot be represented by a button entity: the
# state has to live somewhere. The switch sends the same code either way and
# assumes the result, which is the best any one-way remote allows.
KIND_BUTTON = "button"
KIND_SWITCH = "switch"

SUBENTRY_TYPE_COMMAND = "command"

SERVICE_WATCH = "watch"
ATTR_SECONDS = "seconds"
EVENT_RAW_COMMAND = "rfxcom_commands_raw"

# Long enough to press a few buttons, short enough that the core integration is
# not left deaf for long: raw mode stops it decoding anything.
DEFAULT_WATCH_SECONDS = 30
MAX_WATCH_SECONDS = 120

# The firmware caps repeats at 10. Anything lower has been seen to drop
# commands on remotes whose carrier sits slightly off 433.92 MHz, and a repeat
# costs only a few milliseconds, so there is no reason to default lower.
DEFAULT_REPEATS = 10
MIN_REPEATS = 1
MAX_REPEATS = 10

# The device stops sending on its own; these bound our waiting, not the device.
# Short, because closing the dialog is the way out and a press that was going to
# arrive has arrived by now.
LEARN_TIMEOUT = 20
POLL_INTERVAL = 0.25

# Raw mode reports every RF transmission in earshot, so a noisy band can deliver
# packets far faster than a remote does. Give up rather than accumulate.
MAX_PACKETS_PER_CAPTURE = 2000

# A frame read once is a frame nobody checked, so a capture is only accepted
# when several frames of the same transmission agree. A press carries four of
# them, which is why one press is enough and a second is never asked for.
MIN_FRAMES = 3
