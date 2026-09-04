"""Names for the RFXCOM packet types, as the SDK and RFXmngr use them.

Only the receiving side matters here: this exists so a capture can be read
without the SDK PDF open next to it.
"""

from __future__ import annotations

PACKET_NAMES = {
    0x00: "Interface control",
    0x01: "Interface message",
    0x02: "Transmit response",
    0x03: "Undecoded",
    0x10: "Lighting1",
    0x11: "Lighting2",
    0x12: "Lighting3",
    0x13: "Lighting4",
    0x14: "Lighting5",
    0x15: "Lighting6",
    0x16: "Chime",
    0x17: "Fan",
    0x18: "Curtain1",
    0x19: "Blinds1",
    0x1A: "RFY",
    0x1B: "HomeConfort",
    0x1C: "Edisio",
    0x1D: "ActivLink",
    0x1E: "FunkBus",
    0x1F: "Hunter fan",
    0x20: "Security1",
    0x21: "Security2",
    0x28: "Camera1",
    0x30: "Remote",
    0x31: "Blinds2",
    0x40: "Thermostat1",
    0x41: "Thermostat2",
    0x42: "Thermostat3",
    0x43: "Thermostat4",
    0x44: "Thermostat5",
    0x48: "Radiator1",
    0x4E: "BBQ",
    0x4F: "Temperature and rain",
    0x50: "Temperature",
    0x51: "Humidity",
    0x52: "Temperature and humidity",
    0x54: "Temperature, humidity and barometer",
    0x55: "Rain",
    0x56: "Wind",
    0x57: "UV",
    0x58: "Date and time",
    0x59: "Energy (ELEC1)",
    0x5A: "Energy (ELEC2/3)",
    0x5B: "Energy (ELEC4)",
    0x5C: "Energy (ELEC5)",
    0x5D: "Weight",
    0x60: "Cartelectronic",
    0x70: "RFXSensor",
    0x71: "RFXMeter",
    0x73: "Water level",
    0x74: "Lightning",
    0x76: "Weather",
    0x77: "Solar",
    0x7F: "Raw RF",
}

# Subtypes of the Undecoded packet (0x03): the protocol family the receiver
# thought it heard, without being able to decode it.
UNDECODED_SUBTYPES = {
    0x00: "AC", 0x01: "ARC", 0x02: "ATI", 0x03: "Hideki", 0x04: "LaCrosse",
    0x05: "AD", 0x06: "Mertik", 0x07: "Oregon1", 0x08: "Oregon2",
    0x09: "Oregon3", 0x0A: "ProGuard", 0x0B: "Visonic", 0x0C: "NEC",
    0x0D: "FS20", 0x0F: "Blinds", 0x10: "Rubicson", 0x11: "AE",
    0x12: "FineOffset", 0x13: "RGB", 0x14: "RTS", 0x15: "SelectPlus",
    0x16: "HomeConfort",
}


def packet_name(packet: bytes) -> str:
    """A human-readable description of what a packet is."""
    if len(packet) < 3:
        return "Malformed"
    name = PACKET_NAMES.get(packet[1], f"Unknown (0x{packet[1]:02x})")
    if packet[1] == 0x03:
        family = UNDECODED_SUBTYPES.get(packet[2], f"0x{packet[2]:02x}")
        return f"{name} ({family})"
    return f"{name} subtype {packet[2]}"


def ha_code(packet: bytes) -> str:
    """The code to paste into the RFXCOM integration's `event_code` field.

    The same string RFXmngr labels "HA code": the packet as hex. Home
    Assistant identifies a device by packet type, subtype and id, so the
    sequence number it contains does not affect which device is created.
    """
    return packet.hex()
