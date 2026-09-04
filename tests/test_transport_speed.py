# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Transport timing: the serial send path, ATBRD negotiation, and the
transport-aware scheduler estimate. No hardware — a fake serial device.

Measured on a CH340 ELM327 v1.5 clone on 2026-09-03 (tools/bench_transport.py):
the chip answers ATI in 11 ms at 38400 and 5 ms at 115200, but the old send
path took 107 ms for the same command — 41 ms lost to a 50 ms poll tick and
50 ms to a post-prompt sleep that a byte stream does not need. These tests pin
the three fixes so the milliseconds cannot quietly come back.
"""

import asyncio

import pytest

from conftest import ROOT  # noqa: F401  (sys.path)

import elm327
import reader as rd


class FakeSerial:
    """Just enough pyserial to exercise _send_sync and the ATBRD handshake."""

    def __init__(self, answers=None, baudrate=38400):
        self.baudrate = baudrate
        self.timeout = 1
        self.is_open = True
        self.written = []
        self.answers = dict(answers or {})
        self._buf = b""
        self.reads = 0

    # — the parts the transport uses —
    def reset_input_buffer(self):
        self._buf = b""

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False

    def write(self, data):
        self.written.append(data)
        cmd = data.decode("ascii", "replace").strip().upper()
        self._buf += self.answers.get(cmd, b"OK\r\r>")

    def read_until(self, expected=b"\n", size=None):
        self.reads += 1
        i = self._buf.find(expected)
        if i < 0:
            out, self._buf = self._buf, b""
            return out
        out, self._buf = self._buf[:i + len(expected)], self._buf[i + len(expected):]
        return out

    @property
    def in_waiting(self):
        return len(self._buf)

    def read(self, n):
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


def serial_elm(fake, baud=elm327.SERIAL_BAUD):
    elm = elm327.SerialELM(port="/dev/fake")
    elm.ser = fake
    elm.baud = baud
    return elm


# ── the send path ────────────────────────────────────────────────────────

def test_send_returns_at_the_prompt_and_does_not_sleep_afterwards():
    """`wait` is a BLE affordance: on a byte stream '>' ends the answer.

    The reader asks for wait=0.05 on every poll. Honouring it turned a 5 ms
    command into a 56 ms one on real hardware, so the serial path ignores it.
    """
    fake = FakeSerial({"ATI": b"ELM327 v1.5\r\r>"})
    elm = serial_elm(fake)
    loop = asyncio.new_event_loop()
    try:
        t0 = loop.time()
        lines = loop.run_until_complete(elm.send("ATI", wait=0.5, timeout=2.0))
        elapsed = loop.time() - t0
    finally:
        loop.close()
    assert lines == ["ELM327 v1.5"]
    assert elapsed < 0.2, f"send slept for `wait` ({elapsed:.2f}s)"


def test_send_reads_blocking_not_by_polling():
    """One read_until, bounded by the serial timeout — no 50 ms sleep loop."""
    fake = FakeSerial({"ATRV": b"12.1V\r\r>"})
    elm = serial_elm(fake)
    assert asyncio.run(elm.send("ATRV", wait=0, timeout=3.0)) == ["12.1V"]
    assert fake.reads == 1
    assert fake.timeout == 3.0          # the timeout is the bound, not a tick


def test_monitor_mode_still_returns_what_arrived_without_a_prompt():
    """ATMA never sends '>': the read runs out its timeout and keeps the frames."""
    fake = FakeSerial({"ATMA": b"421 08 00 00\r421 08 00 00\r"})
    elm = serial_elm(fake)
    lines = asyncio.run(elm.send("ATMA", wait=0, timeout=0.2))
    assert lines == ["421 08 00 00", "421 08 00 00"]


# ── ATBRD negotiation ────────────────────────────────────────────────────

def test_set_baud_negotiates_and_moves_the_link():
    fake = FakeSerial({"ATBRD 23": b"OK\rELM327 v1.5\r", "": b"OK\r\r>"})
    elm = serial_elm(fake)
    assert asyncio.run(elm.set_baud(115200)) is True
    assert elm.baud == 115200 and fake.baudrate == 115200


def test_set_baud_tolerates_the_echoed_command():
    """ATZ turns echo back on, so the OK can be one line further down."""
    fake = FakeSerial({"ATBRD 23": b"ATBRD 23\rOK\rELM327 v1.5\r", "": b"OK\r\r>"})
    elm = serial_elm(fake)
    assert asyncio.run(elm.set_baud(115200)) is True
    assert elm.baud == 115200


def test_set_baud_reverts_when_the_adapter_goes_silent():
    """A clone that answers OK and then nothing must leave us at 38400."""
    fake = FakeSerial({"ATBRD 23": b"OK\r"})
    elm = serial_elm(fake)
    assert asyncio.run(elm.set_baud(115200)) is False
    assert elm.baud == 38400 and fake.baudrate == 38400


def test_set_baud_gives_up_on_an_unsupported_divisor():
    fake = FakeSerial({"ATBRD 04": b"?\r\r>"})
    elm = serial_elm(fake)
    assert asyncio.run(elm.set_baud(1000000)) is False
    assert elm.baud == 38400


def test_atz_drops_the_link_back_to_default_and_renegotiates():
    """configure_uds() starts with ATZ, which resets the chip to 38400.

    Without following it down, a negotiated link goes deaf on the first thing
    a vehicle profile does.
    """
    fake = FakeSerial({"ATZ": b"ELM327 v1.5\r\r>",
                       "ATI": b"ELM327 v1.5\r\r>",
                       "ATBRD 23": b"OK\rELM327 v1.5\r",
                       "": b"OK\r\r>"})
    elm = serial_elm(fake, baud=115200)
    fake.baudrate = 115200
    asyncio.run(elm.send("ATZ", wait=0, timeout=8.0))
    assert elm.baud == 115200 and fake.baudrate == 115200
    assert b"ATBRD 23\r" in fake.written       # it really re-negotiated


def test_target_baud_from_env(monkeypatch):
    # Default is the power-up rate: ATBRD is the one change that leaves state
    # on the device across runs, so speeding the wire is opt-in (see the
    # docstring on serial_target_baud). Regression, 2026-09-03.
    monkeypatch.delenv("HAKAKE_SERIAL_BAUD", raising=False)
    assert elm327.serial_target_baud() == elm327.SERIAL_BAUD
    monkeypatch.setenv("HAKAKE_SERIAL_BAUD", "115200")
    assert elm327.serial_target_baud() == 115200
    monkeypatch.setenv("HAKAKE_SERIAL_BAUD", "off")
    assert elm327.serial_target_baud() == elm327.SERIAL_BAUD
    monkeypatch.setenv("HAKAKE_SERIAL_BAUD", "230400")
    assert elm327.serial_target_baud() == 230400
    monkeypatch.setenv("HAKAKE_SERIAL_BAUD", "nonsense")
    assert elm327.serial_target_baud() == elm327.SERIAL_BAUD


def test_simulated_serial_never_negotiates_baud():
    """A pseudo-terminal has no wire rate and the simulator has no ATBRD."""
    assert elm327.SimSerialELM.negotiate_baud is False
    assert elm327.SerialELM.negotiate_baud is True


# ── transport-aware scheduling ───────────────────────────────────────────

def test_transport_speed_attributes_are_declared():
    assert elm327.BleELM.SPEED == 1.0            # the reference the `est`s were timed on
    assert elm327.SerialELM.SPEED < 1.0


def test_estimate_scales_with_the_transport(leaf_profile, tmp_store):
    r = rd.Reader(interval=0, adapter_pref=None, store=tmp_store, budget=1.5)
    ble = r.estimate("lbc02")
    r.speed = elm327.SerialELM.SPEED
    usb = r.estimate("lbc02")
    assert ble == pytest.approx(rd.ITEMS["lbc02"]["est"])
    assert usb == pytest.approx(ble * elm327.SerialELM.SPEED)


def test_passive_dwell_does_not_scale_with_the_transport(leaf_profile, tmp_store):
    """ATMA runs for a wall-clock `secs`; no link makes that shorter."""
    r = rd.Reader(interval=0, adapter_pref=None, store=tmp_store, budget=1.5)
    r.speed = 0.1
    it = rd.ITEMS["p5B3"]
    assert r.estimate("p5B3") == pytest.approx(it["secs"] + 0.25 * 0.1)
    assert r.estimate("p5B3") > it["secs"]


def test_a_faster_transport_packs_more_into_one_cycle(leaf_profile, tmp_store):
    """The point of the multiplier: the slow lane stopped starving on USB."""
    r = rd.Reader(interval=0, adapter_pref=None, store=tmp_store, budget=1.5)
    r.refresh_items()
    slow_uds = [i for i in r._items
                if rd.ITEMS[i]["period"] and rd.TARGETS[rd.ITEMS[i]["kind"]]]
    ble_cost = sum(r.estimate(i) for i in slow_uds)
    r.speed = elm327.SerialELM.SPEED
    usb_cost = sum(r.estimate(i) for i in slow_uds)
    assert ble_cost > r.budget          # over BLE they cannot all fit
    assert usb_cost < r.budget          # over USB they comfortably do
