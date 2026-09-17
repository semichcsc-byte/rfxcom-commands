"""Offline capture preserves rejected packets and receiver mode."""

import io
import subprocess
import sys
from pathlib import Path

import pytest
import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import rfx_capture
from test_config_flow import FakeListener
from custom_components.rfxcom_commands import config_flow


def test_tool_can_import_serial_in_a_fresh_process():
    tool = Path(rfx_capture.__file__).resolve()
    result = subprocess.run(
        [sys.executable, "-c", "import runpy,sys; "
         "sys.path.insert(0, sys.argv[2]); runpy.run_path(sys.argv[1]); "
         "import serial,select; assert callable(select.select)",
         str(tool), str(tool.parent)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


class SerialReceiver:
    def __init__(self):
        self.mode = bytes.fromhex("0d00000003530088002700000000")
        self.previous = self.mode
        self.pending = bytearray()
        self.raw = bytes.fromhex("087f000001019004b0")
        self.interrupt = False
        self.normalise_raw = False
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass

    def write(self, packet):
        self.commands.append(bytes(packet))
        if packet[4] == 3:
            self.mode = bytes(packet)
            if self.normalise_raw and packet[7:11] == bytes([255] * 4):
                mode = bytearray(packet)
                mode[7:11] = bytes.fromhex("80400000")
                self.mode = bytes(mode)
            stale = bytearray(14)
            stale[0:2] = bytes([13, 1])
            stale[4] = 3
            self.pending.extend(stale)
        elif packet[4] == 2:
            reply = bytearray(14)
            reply[0:2] = bytes([13, 1])
            reply[3:5] = bytes([1, 2])
            reply[5] = self.mode[5]
            reply[7:11] = self.mode[7:11]
            reply[13] = self.mode[6]
            self.pending.extend(reply)
        elif packet[4] == 7:
            self.pending.extend(self.raw)

    def read(self, size):
        if self.interrupt and not self.pending:
            self.interrupt = False
            raise KeyboardInterrupt
        chunk = self.pending[:min(size, 2)]
        del self.pending[:len(chunk)]
        return bytes(chunk)


def test_serial_capture_saves_rejected_raw_and_restores(monkeypatch, tmp_path):
    receiver = SerialReceiver()
    monkeypatch.setattr(serial, "Serial", lambda *args, **kwargs: receiver)
    output = io.StringIO()
    packets = rfx_capture.packets_from_port("fake", 0.01, True, output)
    assert receiver.raw in packets
    assert receiver.mode == receiver.previous
    assert all(packet[1] == 0 for packet in receiver.commands)
    logfile = tmp_path / "capture.log"
    logfile.write_text(output.getvalue())
    assert rfx_capture.packets_from_log(logfile) == packets


def test_serial_capture_restores_after_interrupt(monkeypatch):
    receiver = SerialReceiver()
    receiver.interrupt = True
    monkeypatch.setattr(serial, "Serial", lambda *args, **kwargs: receiver)
    with pytest.raises(KeyboardInterrupt):
        rfx_capture.packets_from_port("fake", 1, True)
    assert receiver.mode == receiver.previous


def test_serial_capture_accepts_firmware_normalisation(monkeypatch):
    receiver = SerialReceiver()
    receiver.normalise_raw = True
    monkeypatch.setattr(serial, "Serial", lambda *args, **kwargs: receiver)
    packets = rfx_capture.packets_from_port("fake", 0.01, True)
    assert receiver.raw in packets
    assert receiver.mode == receiver.previous


def test_real_fan_capture_decodes_all_four_presses():
    packets = rfx_capture.packets_from_log(
        Path(__file__).with_name("fan_remote_capture.txt")
    )
    bursts = rfx_capture.raw_bursts(packets)
    commands = [rfx_capture.decode(burst) for burst in bursts]
    assert [command.bits for command in commands] == [
        "000001001011011001001111010000",
        "000001001011011001001100100011",
    ] * 2
    assert all(command.frames_seen == 8 for command in commands)
    assert all(command.trustworthy for command in commands)
    assert all(len(command.pulses) == 60 for command in commands)


@pytest.mark.parametrize("press", range(4))
async def test_learning_accepts_each_real_fan_press(hass, monkeypatch, press):
    packets = rfx_capture.packets_from_log(
        Path(__file__).with_name("fan_remote_capture.txt")
    )
    burst = rfx_capture.raw_bursts(packets)[press]
    monkeypatch.setattr(
        config_flow, "RawListener", lambda hass: FakeListener(hass, packets=burst)
    )
    handler = config_flow.CommandSubentryFlowHandler()
    handler.hass = hass
    command = await handler._capture()
    assert command is not None
    assert command.frames_seen == 8
    assert command.bits == (
        "000001001011011001001111010000" if press % 2 == 0
        else "000001001011011001001100100011"
    )