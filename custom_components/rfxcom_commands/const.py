"""Constants for the RFXCOM Commands integration."""

DOMAIN = "rfxcom_commands"

RFXTRX_DOMAIN = "rfxtrx"
RFXTRX_DATA_OBJECT = "rfxobject"
RFXTRX_SERVICE_SEND = "send"

CONF_PROTOCOLS = "protocols"

CONF_AREA_ID = "area_id"
CONF_EVENTS = "events"
CONF_BITS = "bits"
CONF_PULSES = "pulses"
CONF_REPEATS = "repeats"
CONF_RELEARN = "relearn"
CONF_TEST = "test"

SUBENTRY_TYPE_COMMAND = "command"

# The firmware caps repeats at 10. Anything lower has been seen to drop
# commands on remotes whose carrier sits slightly off 433.92 MHz, and a repeat
# costs only a few milliseconds, so there is no reason to default lower.
DEFAULT_REPEATS = 10
MIN_REPEATS = 1
MAX_REPEATS = 10

# The device stops sending on its own; these bound our waiting, not the device.
LEARN_TIMEOUT = 45
POLL_INTERVAL = 0.25

# How long to keep collecting after the first burst arrives, so that a held
# button does not get chopped into a burst too short to validate.
BURST_SETTLE = 1.0
