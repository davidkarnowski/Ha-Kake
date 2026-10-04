# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The native CAN transport: an ELM327-speaking façade over a frame source.

No hardware, no network: python-can's `virtual` interface carries the frames
(every Bus on one channel sees the others' sends), a fake ECU thread answers
`21 NN` with the *raw frames* of a real capture (tests/fixtures/), and a spy
Bus proves that a listen-only bus never transmits.

The strongest statement here is the fixture round trip: the fake ECU puts the
recorded `7BB` frames on the virtual bus one by one, honouring the flow control
the transport sends, and the lines that come out of `send("2101")` are the
fixture's lines byte for byte — so `parse_isotp()` and `decode_reading()`
cannot tell which transport fed them.

Everything here is evidence about the code. Nothing in this file has touched
a CANable or a car (2026-09-09: the board had not arrived).
"""

import asyncio
import json
import os
import threading
import time
import types

import can
import pytest

from conftest import ROOT, FIXTURES  # noqa: E402  (sys.path is set up there)

import cantransport as ct  # noqa: E402
import elm327  # noqa: E402
import reader as rd  # noqa: E402
from elm327 import configure_uds, passive_capture, set_uds_target  # noqa: E402
from leaf_decoders import decode_reading, parse_isotp  # noqa: E402

with open(os.path.join(FIXTURES, "lbc_raw_20260824.json")) as f:
    LBC = json.load(f)["groups"]
with open(os.path.join(FIXTURES, "probe_20260824_185139.json")) as f:
    _PROBE = json.load(f)
HVAC = _PROBE["hvac"]
PASSIVE = _PROBE["passive"]

_chan = [0]


def channel():
    """A fresh virtual channel per test, so frames never cross between tests."""
    _chan[0] += 1
    return f"hakake-test-{os.getpid()}-{_chan[0]}"


def vbus(chan):
    return can.Bus(interface="virtual", channel=chan, receive_own_messages=False)


def run(coro):
    return asyncio.run(coro)


def frames_of(lines):
    out = []
    for line in lines:
        p = line.split()
        out.append((int(p[0], 16), bytes(int(b, 16) for b in p[1:])))
    return out


def msg(cid, data):
    return can.Message(arbitration_id=cid, data=data, is_extended_id=False)


class FakeECU(threading.Thread):
    """An ECU on the virtual bus that answers `21 NN` with recorded frames.

    It speaks just enough ISO-TP to be a real counterpart: single frame in,
    the fixture's first frame out, wait for our flow control (and record
    what it said), then the consecutive frames back to back. It does not
    reassemble anything — the raw recorded lines are the whole point."""

    def __init__(self, chan, rxid, txid, table):
        super().__init__(daemon=True)
        self.bus = vbus(chan)
        self.rxid, self.txid = rxid, txid
        self.table = {k.upper(): v for k, v in table.items()}
        self.fc = []                # (BS, STmin, DLC) of every flow-control frame we got
        self.requests = []          # (hex request, DLC)
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            m = self.bus.recv(0.02)
            if m is None or m.arbitration_id != self.rxid:
                continue
            d = bytes(m.data)
            if not d or (d[0] & 0xF0) != 0x00 or d[0] < 1:
                continue
            req = d[1:1 + d[0]].hex().upper()
            self.requests.append((req, m.dlc))
            lines = self.table.get(req)
            if not lines:
                continue                                   # an ECU that does not know: silence
            frames = frames_of(lines)
            cid, data = frames[0]
            self.bus.send(msg(cid, data))
            if (data[0] & 0xF0) == 0x10:
                t0 = time.monotonic()
                while time.monotonic() - t0 < 1.0:
                    f = self.bus.recv(0.02)
                    if f is not None and f.arbitration_id == self.rxid and (f.data[0] & 0xF0) == 0x30:
                        self.fc.append((f.data[1], f.data[2], f.dlc))
                        break
                else:
                    continue                               # no flow control: abandon, as an ECU would
                for cid, data in frames[1:]:
                    self.bus.send(msg(cid, data))

    def stop(self):
        self.halt.set()
        self.join(2)
        self.bus.shutdown()


class Broadcaster(threading.Thread):
    """Puts one recorded frame per id on the bus every `period` seconds."""

    def __init__(self, chan, lines, period=0.02):
        super().__init__(daemon=True)
        self.bus = vbus(chan)
        self.frames = frames_of(lines)
        self.period = period
        self.halt = threading.Event()
        self.sent = 0

    def run(self):
        while not self.halt.is_set():
            for cid, data in self.frames:
                self.bus.send(msg(cid, data))
                self.sent += 1
            time.sleep(self.period)

    def stop(self):
        self.halt.set()
        self.join(2)
        self.bus.shutdown()


def facade(chan, bus="car", listen_only=False, **kw):
    src = ct.LocalSource(bus_factory=lambda: vbus(chan), bus=bus, listen_only=listen_only,
                         response_timeout=kw.pop("response_timeout", 0.2), log=lambda *a: None)
    return ct.CanFacade(src, settle=0, log=lambda *a: None, **kw)


@pytest.fixture
def rig():
    """A façade, a fake LBC and a fake HVAC amp on one virtual channel."""
    chan = channel()
    lbc = FakeECU(chan, 0x79B, 0x7BB, LBC)
    hvac = FakeECU(chan, 0x744, 0x764, HVAC)
    lbc.start()
    hvac.start()
    elm = facade(chan)
    run(elm.connect(log=lambda *a: None))
    try:
        yield elm, lbc, hvac, chan
    finally:
        run(elm.close())
        lbc.stop()
        hvac.stop()


# ── line shapes ──────────────────────────────────────────────────────────

def test_line_format_is_the_elm_line():
    """Exactly what SerialELM / BleELM hand the decoders: id, then bytes, DLC preserved."""
    assert ct.can_line("421", b"\x08\x00\x00") == "421 08 00 00"
    assert ct.can_line("385", bytes.fromhex("0000958F9292F0")) == "385 00 00 95 8F 92 92 F0"
    assert ct.can_line("421", b"") == "421"
    assert ct.id_hex(0x421) == "421" and ct.id_hex(0x2A) == "02A"
    assert ct.id_hex(0x18DAF110, extended=True) == "18DAF110"


# ── the ELM command surface ──────────────────────────────────────────────

def test_ati_answers_a_name_with_a_digit_and_satisfies_probe_alive(rig):
    elm = rig[0]
    for cmd in ("ATI", "ATZ", "AT@1"):
        r = run(elm.send(cmd))
        assert r and any(c.isdigit() for c in r[0]), (cmd, r)
    assert run(rd.Reader.probe_alive(rd.Reader.__new__(rd.Reader), elm)) is True


def test_at_setup_commands_are_accepted_silently(rig):
    elm = rig[0]
    for cmd in ("ATE0", "ATL1", "ATH1", "ATS1", "ATSP6", "ATFCSM1", "ATST 32", "ATD"):
        assert run(elm.send(cmd, wait=0)) == []
    assert run(elm.send("")) == []                      # the poke that ends ATMA


def test_the_adapter_state_is_remembered_and_atz_resets_it(rig):
    elm = rig[0]
    run(elm.send("ATSH 79B")); run(elm.send("ATCRA 7BB")); run(elm.send("ATCAF0"))
    run(elm.send("ATFCSH 79B")); run(elm.send("ATFCSD 30 00 20"))
    assert (elm.tx, elm.rx, elm.caf, elm.fc_tx) == ("79B", "7BB", False, "79B")
    assert (elm.fc_bs, elm.fc_stmin) == (0x00, 0x20)
    run(elm.send("ATAR"))
    assert elm.rx is None
    run(elm.send("ATZ"))
    assert (elm.tx, elm.rx, elm.caf, elm.fc_stmin) == (None, None, True, 0)


def test_the_facade_asks_for_the_serial_separation_time():
    """5 ms, not 0: the Leaf's HVAC amp drops consecutive frames at STmin 0
    (car, 2026-10-03), the LBC does not mind either way."""
    assert ct.CanFacade.STMIN == "05" == elm327.STMIN_SERIAL
    assert ct.CanFacade.SPEED < elm327.SerialELM.SPEED < elm327.BleELM.SPEED
    assert ct.CanFacade.PASSIVE_INSTANT is True
    assert ct.CanFacade.adapter_type == "can"


def test_configure_uds_leaves_the_facade_targeting_the_lbc_with_stmin_5(rig):
    elm = rig[0]
    run(configure_uds(elm, "79B", "7BB"))
    assert (elm.tx, elm.rx, elm.caf, elm.fc_tx) == ("79B", "7BB", True, "79B")
    assert (elm.fc_bs, elm.fc_stmin) == (0, 5)


# ── UDS through the fake ECU: the fixture round trip ─────────────────────

@pytest.mark.parametrize("cmd", ["2101", "2102", "2104"])
def test_uds_answers_are_the_recorded_frames_byte_for_byte(rig, cmd):
    elm, lbc = rig[0], rig[1]
    run(configure_uds(elm, "79B", "7BB"))
    lines = run(elm.send(cmd, wait=0.05, timeout=10.0))
    assert lines == LBC[cmd]
    assert parse_isotp(lines) == parse_isotp(LBC[cmd])
    assert lines[0].startswith("7BB 10 ")            # raw first frame, never a rebuilt payload


def test_the_decoders_cannot_tell_the_transport_from_the_elm(rig):
    elm = rig[0]
    run(configure_uds(elm, "79B", "7BB"))
    got = {c: run(elm.send(c, timeout=10.0)) for c in ("2101", "2102", "2104")}
    assert decode_reading(got) == decode_reading({c: LBC[c] for c in got})
    assert decode_reading(got)["soc"] > 0 and len(decode_reading(got)["cells"]) == 96


def test_the_flow_control_we_send_carries_the_atfcsd_bytes_padded_like_the_elm(rig):
    """ATFCSD 30 00 20 → BS 0x00, STmin 0x20 on the wire; the ELM pads every
    frame to 8 bytes (ATV0) and the request the LBC already accepts is
    `02 21 01 00 00 00 00 00`, so the transport does the same."""
    elm, lbc = rig[0], rig[1]
    run(configure_uds(elm, "79B", "7BB"))
    run(elm.send("ATFCSD 30 00 20"))
    assert run(elm.send("2102", timeout=10.0)) == LBC["2102"]
    assert lbc.fc[-1] == (0x00, 0x20, 8)
    assert lbc.requests[-1] == ("2102", 8)
    run(elm.send("ATFCSD 30 00 00"))
    run(elm.send("2101", timeout=10.0))
    assert lbc.fc[-1] == (0x00, 0x00, 8)


def test_can_isotp_stmin_overrides_atfcsd_when_set():
    chan = channel()
    lbc = FakeECU(chan, 0x79B, 0x7BB, LBC)
    lbc.start()
    elm = facade(chan, stmin_override=0)
    run(elm.connect(log=lambda *a: None))
    try:
        run(configure_uds(elm, "79B", "7BB"))
        run(elm.send("ATFCSD 30 00 20"))
        assert elm.fc_stmin == 0x20 and elm.stmin == 0
        assert run(elm.send("2101", timeout=10.0)) == LBC["2101"]
        assert lbc.fc[-1][:2] == (0x00, 0x00)
    finally:
        run(elm.close())
        lbc.stop()


def test_atsh_selects_which_ecu_answers(rig):
    """The HVAC amp answers 2110 on 764; group 2100 is absent from the capture
    (the amp stayed quiet), so that is NO DATA — silence from the ECU, never
    an invented answer. The same thing the replay transport does."""
    elm = rig[0]
    run(configure_uds(elm, "79B", "7BB"))
    run(set_uds_target(elm, "744", "764"))
    lines = run(elm.send("2110", timeout=4.0))
    assert lines == HVAC["2110"] and lines[0].startswith("764 ")
    assert "2100" not in HVAC
    assert run(elm.send("2100", timeout=4.0)) == ["NO DATA"]
    assert "744:2100" in elm.misses
    run(set_uds_target(elm, "79B", "7BB"))
    assert run(elm.send("2101", timeout=10.0)) == LBC["2101"]


def test_a_silent_ecu_costs_the_response_timeout_not_the_item_timeout(rig):
    """Over the ELM a silent ECU is ~200 ms then NO DATA; the item's 10 s is
    only the serial ceiling. A sleeping car must not cost 10 s per item here."""
    elm = rig[0]
    run(configure_uds(elm, "79B", "7BB"))
    t0 = time.monotonic()
    assert run(elm.send("2199", timeout=10.0)) == ["NO DATA"]
    elapsed = time.monotonic() - t0
    assert 0.15 < elapsed < 1.0, elapsed


def test_a_request_before_any_target_is_no_data_not_a_crash(rig):
    elm = rig[0]
    assert run(elm.send("2101")) == ["NO DATA"]


def test_garbage_answers_a_question_mark(rig):
    assert run(rig[0].send("hello")) == ["?"]


# ── read-only, enforced here too ─────────────────────────────────────────

@pytest.mark.parametrize("cmd", ["2E01", "1003", "04", "2701", "3101FF00", "3D"])
def test_a_non_read_service_never_reaches_the_bus(rig, cmd):
    elm, lbc = rig[0], rig[1]
    run(configure_uds(elm, "79B", "7BB"))
    before = len(lbc.requests)
    assert run(elm.send(cmd)) == ["NO DATA"]
    time.sleep(0.05)
    assert len(lbc.requests) == before
    assert elm.refused[-1] == f"79B:{cmd.upper()}"


@pytest.mark.parametrize("cmd", ["2101", "0100", "03", "07"])
def test_the_read_services_this_project_sends_are_allowed(rig, cmd):
    elm = rig[0]
    run(configure_uds(elm, "79B", "7BB"))
    run(elm.send(cmd, timeout=1.0))
    assert not elm.refused


def test_refusals_are_logged_once_per_distinct_request():
    chan = channel()
    said = []
    src = ct.LocalSource(bus_factory=lambda: vbus(chan), bus="car", log=said.append)
    elm = ct.CanFacade(src, settle=0, log=said.append)
    run(elm.connect(log=said.append))
    try:
        run(elm.send("ATSH 79B")); run(elm.send("ATCRA 7BB"))
        for _ in range(3):
            run(elm.send("2E01"))
        assert sum("2E01" in s for s in said) == 1
    finally:
        run(elm.close())


# ── passive frames: ATMA from the table ──────────────────────────────────

def test_passive_capture_returns_at_once_with_what_arrived():
    chan = channel()
    bc = Broadcaster(chan, [PASSIVE["421"][0], PASSIVE["385"][0]], period=0.02)
    elm = facade(chan)
    run(elm.connect(log=lambda *a: None))
    bc.start()
    try:
        time.sleep(0.15)
        t0 = time.monotonic()
        lines = run(passive_capture(elm, "421", 0.2))
        elapsed = time.monotonic() - t0
        assert elapsed < 0.05, f"ATMA dwelt for {elapsed:.3f}s"
        assert lines and all(l == "421 08 00 00" for l in lines)
        assert 3 <= len(lines) <= 12                     # ~0.2 s of a 20 ms broadcast
        tp = run(passive_capture(elm, "385", 0.3, set_caf=False))
        assert tp and tp[0] == PASSIVE["385"][0]         # DLC 7, not padded
        assert elm.freshness()["421"] < 0.1
    finally:
        bc.stop()
        run(elm.close())


def test_atma_window_and_drain():
    """The window is the caller's timeout — the same `secs` the dwell used —
    and what ATMA returns it forgets, so a value is never read twice."""
    src = ct.LocalSource(bus_factory=lambda: None, bus="car", log=lambda *a: None)
    elm = ct.CanFacade(src, settle=0, log=lambda *a: None)
    now = time.monotonic()
    with elm.lock:
        elm.recent["421"].append((now - 5.0, b"\x00\x00\x00"))     # stale
        elm.recent["421"].append((now - 0.1, b"\x08\x00\x00"))
        elm.recent["421"].append((now - 0.05, b"\x10\x00\x00"))
        elm.recent["358"].append((now - 0.07, b"\x01"))
    elm.rx = "421"
    assert elm.monitor(0.2) == ["421 08 00 00", "421 10 00 00"]
    assert elm.monitor(0.2) == []                       # drained
    elm.rx = None
    assert elm.monitor(1.0) == ["358 01"]                # no filter: everything, oldest first
    elm._on_frame(1.0, "60d", b"\x00\x06", {})           # ids normalise to upper case
    elm.rx = "60D"
    assert elm.monitor(1.0) == ["60D 00 06"]
    elm._on_frame(1.0, "7BB", b"", {"err": True})
    assert elm.errors == 1 and "7BB" not in elm.latest   # error frames are counted, not shown


# ── listen-only: the EV bus never transmits ──────────────────────────────

def test_the_ev_bus_is_listen_only_whatever_the_config_says():
    src = ct.LocalSource(bus_factory=lambda: None, bus="ev", listen_only=False)
    assert src.listen_only is True
    assert ct.LocalSource(bus_factory=lambda: None, bus="car").listen_only is False
    assert ct.can_config({"can_bus": "ev", "can_listen_only": False})["listen_only"] is True
    with pytest.raises(ValueError):
        ct.LocalSource(bus_factory=lambda: None, bus="both")


def test_listen_only_refuses_every_request_and_puts_nothing_on_the_bus():
    chan = channel()
    spy = vbus(chan)                                     # receives only what OTHERS send
    lbc = FakeECU(chan, 0x79B, 0x7BB, LBC)
    lbc.start()
    elm = facade(chan, bus="ev")
    run(elm.connect(log=lambda *a: None))
    try:
        assert elm.listen_only is True
        run(configure_uds(elm, "79B", "7BB"))
        assert run(elm.send("2101", timeout=10.0)) == ["NO DATA"]
        assert run(elm.send("0100", timeout=2.0)) == ["NO DATA"]
        assert spy.recv(0.1) is None                     # not one frame, from anyone
        assert lbc.requests == []
        assert elm.refused == ["79B:2101", "79B:0100"]
        # ...but it still hears the bus
        spy.send(msg(0x1DB, bytes.fromhex("07E6000000000000")))
        time.sleep(0.05)
        run(elm.send("ATCRA 1DB"))
        assert run(elm.send("ATMA", timeout=0.5)) == ["1DB 07 E6 00 00 00 00 00 00"]
        assert elm.marker() == {"can_bus": "ev", "listen_only": True}
    finally:
        run(elm.close())
        lbc.stop()
        spy.shutdown()


def test_the_source_itself_refuses_when_listen_only():
    src = ct.LocalSource(bus_factory=lambda: None, bus="ev")
    assert run(src.send_uds("79B", "7BB", b"\x21\x01", 0, 0, 1.0)) == {"ok": False, "error": "refused"}
    src = ct.LocalSource(bus_factory=lambda: None, bus="car")
    assert run(src.send_uds("79B", "7BB", b"\x2e\x01", 0, 0, 1.0)) == {"ok": False, "error": "refused"}


class StubGsDevice:
    """A gs_usb device that records what is done to it (gs_usb.GsUsb's surface)."""

    def __init__(self, feature):
        self.device_capability = types.SimpleNamespace(feature=feature, fclk_can=48_000_000)
        self.calls = []
        self.device_flags = None

    def set_timing(self, **kw):
        self.calls.append("timing")

    def start(self, flags):
        self.calls.append(("start", flags))
        self.device_flags = flags & self.device_capability.feature

    def stop(self):
        self.calls.append("stop")


def test_a_listen_only_gs_usb_bus_starts_once_silent_and_never_sends():
    gs = pytest.importorskip("gs_usb.constants")
    LO, TS = gs.GS_CAN_MODE_LISTEN_ONLY, gs.GS_CAN_MODE_HW_TIMESTAMP
    dev = StubGsDevice(feature=LO | TS)
    bus = ct.silent_gs_usb_class()(channel="canable", bitrate=500000, device=dev)
    assert dev.calls == ["timing", ("start", LO | TS)]            # one start, silent from the first
    with pytest.raises(can.CanOperationError):
        bus.send(msg(0x79B, b"\x02\x21\x01"))
    bus.shutdown()
    bus.shutdown()
    assert dev.calls == ["timing", ("start", LO | TS), "stop"]    # no normal-mode restart on the way out


def test_a_gs_usb_device_without_listen_only_is_refused_before_any_start():
    gs = pytest.importorskip("gs_usb.constants")
    dev = StubGsDevice(feature=gs.GS_CAN_MODE_HW_TIMESTAMP)
    with pytest.raises(ConnectionError, match="listen-only"):
        ct.silent_gs_usb_class()(channel="canable", bitrate=500000, device=dev)
    assert dev.calls == []


def test_the_ev_bus_on_gs_usb_never_uses_the_normal_mode_opener(monkeypatch):
    gsmod = pytest.importorskip("can.interfaces.gs_usb")
    opened = []

    class Normal:
        def __init__(self, **kw):
            opened.append("normal")

    class Silent:
        def __init__(self, **kw):
            opened.append("silent")
    monkeypatch.setattr(gsmod, "GsUsbBus", Normal)
    monkeypatch.setattr(ct, "silent_gs_usb_class", lambda: Silent)
    ct.LocalSource(bus="ev", interface="gs_usb", log=lambda *a: None)._open_gs_usb(None)
    ct.LocalSource(bus="car", interface="gs_usb", log=lambda *a: None)._open_gs_usb(None)
    assert opened == ["silent", "normal"]


def test_socketcan_ev_bus_refuses_unless_the_kernel_says_listen_only(monkeypatch):
    monkeypatch.setattr(ct, "socketcan_is_listen_only", lambda ch: False)
    src = ct.LocalSource(interface="socketcan", channel="can0", bus="ev", log=lambda *a: None)
    with pytest.raises(ConnectionError, match="listen-only on"):
        src._open()


# ── finding the board ────────────────────────────────────────────────────

class Dev:
    def __init__(self, vid, pid):
        self.idVendor, self.idProduct = vid, pid


def test_firmware_is_classified_from_usb_ids():
    assert ct.classify_usb([Dev(0x16D0, 0x117E)])[0] == "slcan"
    assert ct.classify_usb([Dev(0x1D50, 0x606F)])[0] == "gs_usb"
    assert ct.classify_usb([Dev(0x0483, 0xDF11)])[0] == "dfu"
    assert ct.classify_usb([Dev(0x05AC, 0x0001)]) == (None, None)
    assert ct.classify_usb([Dev(0x0483, 0xDF11), Dev(0x16D0, 0x117E)])[0] == "slcan"   # a real one beside a stuck one


def test_a_board_stuck_in_dfu_mode_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(ct, "sniff_canable", lambda devices=None: ("dfu", Dev(0x0483, 0xDF11)))
    src = ct.LocalSource(interface="auto", bus="car", log=lambda *a: None)
    with pytest.raises(ConnectionError, match="DFU"):
        src._open()


def test_no_board_at_all_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(ct, "sniff_canable", lambda devices=None: (None, None))
    monkeypatch.setattr(ct, "find_slcan_port", lambda channel="", dev=None: None)
    src = ct.LocalSource(interface="auto", bus="car", log=lambda *a: None)
    with pytest.raises(ConnectionError, match="no CANable"):
        src._open()


def test_slcan_listen_only_opens_with_m1_then_o():
    """Stock canable2-fw has no `L`; silent mode is `M1` before `O`."""
    writes = []
    b = ct.HakakeSlcanBus.__new__(ct.HakakeSlcanBus)
    b._listen_only = True
    b._write = writes.append
    b.open()
    assert writes == ["M1", "O"]
    writes.clear()
    b._listen_only = False
    b.open()
    assert writes == ["O"]


# ── liveness and reconnect ───────────────────────────────────────────────

def test_a_dead_source_fails_the_probe_and_raises_on_the_next_request(rig):
    elm, lbc = rig[0], rig[1]
    run(configure_uds(elm, "79B", "7BB"))
    assert run(rd.Reader.probe_alive(rd.Reader.__new__(rd.Reader), elm)) is True
    elm.source._sink.on_error(OSError("USB device gone"))   # what the Notifier thread reports
    assert elm.liveness()["online"] is False
    assert run(elm.send("ATI")) == []                        # no digit: probe_alive → False
    assert run(rd.Reader.probe_alive(rd.Reader.__new__(rd.Reader), elm)) is False
    with pytest.raises(ConnectionError):
        run(elm.send("2101"))


def test_liveness_reports_frames_and_rate():
    chan = channel()
    bc = Broadcaster(chan, [PASSIVE["421"][0]], period=0.01)
    elm = facade(chan)
    run(elm.connect(log=lambda *a: None))
    bc.start()
    try:
        time.sleep(0.2)
        live = elm.liveness()
        assert live["online"] is True and live["frames"] >= 5
        assert live["bus"] == "car" and live["listen_only"] is False
    finally:
        bc.stop()
        run(elm.close())


# ── configuration and detect_adapter ─────────────────────────────────────

def test_can_config_requires_the_bus(monkeypatch):
    monkeypatch.delenv("HAKAKE_CAN_BUS", raising=False)
    with pytest.raises(ConnectionError, match="can_bus"):
        ct.can_config({})
    with pytest.raises(ConnectionError, match="can_bus"):
        ct.can_config({"can_bus": "ev-can"})
    c = ct.can_config({"can_bus": "car"})
    assert c["interface"] == "auto" and c["bitrate"] == 500000 and c["listen_only"] is False
    assert c["stmin_override"] is None and c["tx_padding"] == 0
    c = ct.can_config({"can_bus": "car", "can_interface": "slcan", "can_channel": "/dev/cu.usbmodem1",
                       "can_isotp_stmin": "0x20", "can_tx_padding": None, "can_listen_only": "yes"})
    assert c["interface"] == "slcan" and c["channel"] == "/dev/cu.usbmodem1"
    assert c["stmin_override"] == 0x20 and c["tx_padding"] is None and c["listen_only"] is True


def test_environment_wins_over_the_config_file(monkeypatch):
    monkeypatch.setenv("HAKAKE_CAN_BUS", "ev")
    monkeypatch.setenv("HAKAKE_CAN_BITRATE", "250000")
    c = ct.can_config({"can_bus": "car", "can_bitrate": 500000})
    assert c["bus"] == "ev" and c["listen_only"] is True and c["bitrate"] == 250000


def test_detect_adapter_can_refuses_to_run_without_a_bus(monkeypatch):
    monkeypatch.delenv("HAKAKE_CAN_BUS", raising=False)
    monkeypatch.setattr(elm327, "_cfg", {"can_interface": "virtual"})
    with pytest.raises(ConnectionError, match="can_bus"):
        run(elm327.detect_adapter(prefer="can", log=lambda *a: None))


def test_detect_adapter_can_builds_the_facade_from_config(monkeypatch):
    monkeypatch.delenv("HAKAKE_CAN_BUS", raising=False)
    chan = channel()
    monkeypatch.setattr(elm327, "_cfg", {"can_interface": "virtual", "can_channel": chan, "can_bus": "car"})
    said = []
    elm = run(elm327.detect_adapter(prefer="can", log=said.append))
    try:
        assert isinstance(elm, ct.CanFacade)
        assert elm.adapter_type == "can" and elm.bus == "car" and elm.listen_only is False
        assert any(c.isdigit() for c in elm.adapter_name)
        assert any("NATIVE CAN" in s for s in said)
    finally:
        run(elm.close())


def test_auto_detect_never_picks_the_can_adapter(monkeypatch):
    """The board cannot tell which pins it is wired to; normal mode on EV-CAN
    would be a transmit-capable node on the battery bus. Asked for, only."""
    monkeypatch.setattr(elm327, "_cfg", {"can_interface": "virtual", "can_bus": "car"})
    monkeypatch.setattr(elm327, "_find_serial_port", lambda: None)

    async def no_ble(self, log=print):
        raise ConnectionError("no BLE here")

    monkeypatch.setattr(elm327.BleELM, "connect", no_ble)
    with pytest.raises(ConnectionError):
        run(elm327.detect_adapter(prefer=None, log=lambda *a: None))


# ── the scheduler ────────────────────────────────────────────────────────

def test_estimate_drops_the_passive_dwell_when_the_transport_says_instant(leaf_profile, tmp_store):
    r = rd.Reader(interval=0, adapter_pref=None, store=tmp_store, budget=1.5)
    r.speed = ct.CanFacade.SPEED
    it = rd.ITEMS["p5B3"]
    assert r.estimate("p5B3") == pytest.approx(it["secs"] + 0.25 * r.speed)
    r.passive_instant = True
    assert r.estimate("p5B3") == pytest.approx(0.25 * r.speed)
    assert r.estimate("lbc02") == pytest.approx(rd.ITEMS["lbc02"]["est"] * r.speed)


def test_reader_adopts_passive_instant_from_the_adapter(isolated_reader, leaf_profile, tmp_store, monkeypatch):
    """Reader.run() reads SPEED and PASSIVE_INSTANT off the adapter it connected."""
    chan = channel()
    elm = facade(chan)

    async def fake_detect(prefer=None, log=None):
        await elm.connect(log=lambda *a: None)
        return elm

    async def fake_configure(e):
        pass

    async def stop_here(self, e):
        raise asyncio.CancelledError

    monkeypatch.setattr(rd, "detect_adapter", fake_detect)
    monkeypatch.setattr(rd, "configure_vehicle", fake_configure)
    monkeypatch.setattr(rd.Reader, "poll_loop", stop_here)
    r = rd.Reader(interval=0, adapter_pref="can", store=tmp_store)
    assert r.passive_instant is False
    with pytest.raises(asyncio.CancelledError):
        run(r.run())
    assert r.passive_instant is True and r.speed == ct.CanFacade.SPEED


# ── the reader, end to end ───────────────────────────────────────────────

def test_one_poll_cycle_over_the_facade_produces_the_dashboard_record(isolated_reader, leaf_profile, tmp_store):
    """The reader's own poll_once over the façade: battery from the fake LBC
    (UDS), gear from the broadcast 0x421 (ATMA from the table), and the
    passive items timed in the milliseconds."""
    chan = channel()
    lbc = FakeECU(chan, 0x79B, 0x7BB, LBC)
    hvac = FakeECU(chan, 0x744, 0x764, HVAC)
    bc = Broadcaster(chan, [PASSIVE[i][0] for i in ("421", "284", "60D", "5C5", "385", "292", "5A9", "5B3", "355")],
                     period=0.02)
    lbc.start(); hvac.start()
    elm = facade(chan)
    run(elm.connect(log=lambda *a: None))
    bc.start()
    try:
        time.sleep(0.3)
        run(rd.configure_vehicle(elm))
        r = rd.Reader(interval=0, adapter_pref="can", store=tmp_store, budget=1.5)
        r.speed = elm.SPEED
        r.passive_instant = elm.PASSIVE_INSTANT
        rec, alive = run(r.poll_once(elm))
        assert alive is True
        assert rec["soc"] > 0 and rec["pack_v"] > 300
        assert rec["gear"] == "P"
        assert "p421" in rec["timing"] and rec["timing"]["p421"] < 0.05
        assert rec["timing"]["lbc01"] < 0.5
        assert elm.adapter_type == "can" and elm.marker() == {"can_bus": "car", "listen_only": False}
    finally:
        bc.stop()
        run(elm.close())
        lbc.stop(); hvac.stop()


def _listen_only_cycle(chan, tmp_store, broadcast):
    """One reader cycle over a listen-only Car-CAN façade beside a fake LBC."""
    lbc = FakeECU(chan, 0x79B, 0x7BB, LBC)
    bc = Broadcaster(chan, [PASSIVE[i][0] for i in ("421", "284", "60D", "5C5", "385", "292", "5A9", "5B3", "355")],
                     period=0.02) if broadcast else None
    lbc.start()
    elm = facade(chan, listen_only=True)
    run(elm.connect(log=lambda *a: None))
    if bc:
        bc.start()
    try:
        time.sleep(0.3)
        run(rd.configure_vehicle(elm))
        r = rd.Reader(interval=0, adapter_pref="can", store=tmp_store, budget=1.5)
        r.speed = elm.SPEED
        r.passive_instant = elm.PASSIVE_INSTANT
        rec, alive = run(r.poll_once(elm))
        return r, rec, alive, lbc.requests, elm.refused
    finally:
        if bc:
            bc.stop()
        run(elm.close())
        lbc.stop()


def test_a_listen_only_car_bus_with_broadcasts_is_awake(isolated_reader, leaf_profile, tmp_store):
    """Car, 2026-10-03: a listen-only CANable on Car-CAN heard 1,700 frames/s
    and the reader still said "car asleep?", because the Leaf's liveness is
    the LBC's answer and a silent controller never asks. The broadcasts decide."""
    r, rec, alive, requests, refused = _listen_only_cycle(channel(), tmp_store, broadcast=True)
    assert alive is True
    assert rec["gear"] == "P"
    assert not any(i.startswith(("lbc", "hvac")) for i in rec["timing"])     # never planned onto the wire
    assert requests == [] and refused == []                                  # not even asked of the façade
    assert r.item_last.get("lbc01") is not None                              # kept on its period


def test_a_listen_only_car_bus_that_is_silent_is_asleep(isolated_reader, leaf_profile, tmp_store):
    r, rec, alive, requests, refused = _listen_only_cycle(channel(), tmp_store, broadcast=False)
    assert alive is False
    assert requests == []


# ── cadence: no padding on native CAN, and the read readout ──────────────

def test_the_cycle_floor_comes_from_the_transport_unless_given(tmp_store):
    """The ELM327s keep the 0.5 s floor; the façade has none, because there
    the cycle is the requests (car, 2026-10-03: a 0.4 s cycle padded to 0.5)."""
    assert ct.CanFacade.MIN_INTERVAL == 0.0
    assert rd.interval_for(ct.CanFacade) == 0.0
    assert rd.interval_for(elm327.SerialELM) == rd.DEFAULT_INTERVAL == 0.5
    assert rd.interval_for(object()) == 0.5
    assert rd.interval_for(ct.CanFacade, explicit=1.0) == 1.0          # --interval wins
    assert rd.interval_for(elm327.SerialELM, explicit=0) == 0.0
    r = rd.Reader(interval=None, adapter_pref="can", store=tmp_store)
    assert r.interval_arg is None and r.interval == 0.5                # until a transport says otherwise


def test_poll_once_publishes_read_duration_and_gap_in_milliseconds(isolated_reader, leaf_profile, tmp_store):
    chan = channel()
    lbc = FakeECU(chan, 0x79B, 0x7BB, LBC)
    lbc.start()
    elm = facade(chan)
    run(elm.connect(log=lambda *a: None))
    try:
        run(rd.configure_vehicle(elm))
        r = rd.Reader(interval=0, adapter_pref="can", store=tmp_store, budget=1.5)
        r.speed, r.passive_instant = elm.SPEED, elm.PASSIVE_INSTANT
        rec1, _ = run(r.poll_once(elm))
        assert rec1["item_dur"]["lbc01"] == rec1["timing"]["lbc01"]
        assert "lbc01" not in rec1["item_gap"]                          # one read: no gap yet
        time.sleep(0.05)
        rec2, _ = run(r.poll_once(elm))
        gap = rec2["item_gap"]["lbc01"]
        assert 0.05 <= gap < 2.0
        assert round(gap, 3) == gap                                      # milliseconds, not tenths
        assert all(round(v, 3) == v for v in rec2["item_age"].values())
        assert any(round(v, 1) != v for v in rec2["item_dur"].values())   # a ~ms read survives unrounded
    finally:
        run(elm.close())
        lbc.stop()


def test_read_duration_and_gap_are_never_stored(tmp_store):
    rid = tmp_store.insert_reading({"soc": 50.0, "timestamp": "2026-10-03T19:40:00Z",
                                    "item_dur": {"lbc02": 0.29}, "item_gap": {"lbc02": 0.43},
                                    "item_age": {"lbc02": 0.1}})
    extra = tmp_store.conn.execute("SELECT extra FROM readings WHERE id = ?", (rid,)).fetchone()[0]
    extra = json.loads(extra or "{}")
    assert "item_dur" not in extra and "item_gap" not in extra
    assert "item_age" in extra                                         # unchanged: only the two new maps are live-only
