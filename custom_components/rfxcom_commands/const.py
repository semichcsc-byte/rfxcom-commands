"""Constants for the RFXCOM Commands integration."""

DOMAIN = "rfxcom_commands"

RFXTRX_DOMAIN = "rfxtrx"
RFXTRX_DATA_OBJECT = "rfxobject"
RFXTRX_SERVICE_SEND = "send"

CONF_AREA_ID = "area_id"
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

# Hearing the same command this many times means the button is being held, so
# there is no point listening for the rest of the window.
CONFIDENT_REPEATS = 3

# A frame read once is a frame nobody checked. A dropped bit decodes into a
# perfectly plausible command that simply does not work, so a capture only
# counts when the same bits arrive at least this many times.
MIN_SIGHTINGS = 2

# Once something has been captured, this much silence means the button has been
# released. Without it a single press would wait out the whole window.
QUIET_PERIOD = 1.5
