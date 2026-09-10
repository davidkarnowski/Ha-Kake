# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The simulated CAN bus (simulator/canbus.py) and the sim-can transport.

The model's ECUs on a python-can `virtual` channel, the native CAN façade
on the same channel. What is pinned: the surveyed periods (±20 %), the
EV-CAN payload shapes round-tripping through their own decoders, a UDS
answer through the façade decoding to the same record the ELM sim path
gives for the same model state, the negative response for anything but a
read, flow control honoured, the sleeping car's silence, the `bus_load`
knob, and that the transport files its rows under `sim` with the simulated
stamp.

Everything here is evidence about the code. Every EV-CAN byte the rig
emits is ASSERTED from public documentation; a green run says the encoder
and its decoder agree, not that the car does.
"""

import asyncio
import os
import subprocess
import sys
import threading
import time

import can
import pytest

from conftest import ROOT  # noqa: E402

import cantransport as ct  # noqa: E402
import elm327  # noqa: E402
import reader as rd  # noqa: E402
from elm327 import SimELM, configure_uds, passive_capture, set_uds_target  # noqa: E402
from leaf_decoders import decode_reading, parse_isotp  # noqa: E402
from simulator import make_sim  # noqa: E402
from simulator import canbus as cb  # noqa: E402

_chan = [0]


def channel():
    _chan[0] += 1
    return f"hakake-simcan-test-{os.getpid()}-{_chan[0]}"


def run(coro):
    return asyncio.run(coro)


def quiet_sim(**knobs):
    k = {"noise": 0.0}
    k.update(knobs)
    return make_sim(vehicle="leaf_ze0", knobs=k, seed=1)


def facade(chan, bus="car"):
    src = ct.LocalSource(interface="virtual", channel=chan, bus=bus, response_timeout=0.3,
                         log=lambda *a: None)
    return ct.CanFacade(src, settle=0, log=lambda *a: None)


class Spy(threading.Thread):
    """Counts frames per id on a channel."""

    def __init__(self, chan):
        super().__init__(daemon=True)
        self.bus = can.Bus(interface="virtual", channel=chan, receive_own_messages=False)
        self.counts = {}
        self.errors = 0
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            m = self.bus.recv(0.02)
            if m is None:
                continue
            if m.is_error_frame:
                self.errors += 1
                continue
            k = f"{m.arbitration_id:03X}"
            self.counts[k] = self.counts.get(k, 0) + 1

    def stop(self):
        self.halt.set()
        self.join(1)
        self.bus.shutdown()


# ── the survey and the schedule ──────────────────────────────────────────

def test_the_period_tables_add_up_to_the_surveyed_totals():
    """memo §4.9: ≈1,690 frames/s on Car-CAN, ≈790 on EV-CAN."""
    assert cb.expected_fps("car") == pytest.approx(1690, rel=0.05)
    assert cb.expected_fps("ev") == pytest.approx(790, rel=0.05)
    assert cb.expected_fps("car", bus_load=0.0) < 0.3 * cb.expected_fps("car")
    modelled = {"174", "180", "284", "292", "355", "421", "358", "385", "5C5", "60D", "625", "5A9", "5B3"}
    assert not modelled & set(cb.filler_ids("car", "leaf_ze0"))
    assert set(cb.EV_MODELLED).isdisjoint(cb.filler_ids("ev", "leaf_ze0"))
    for it in rd.ITEMS.values():                                   # every passive item the profile polls
        if it["kind"] == "passive":
            assert it["id"].upper() in cb.CAR_PERIODS_MS["leaf_ze0"], it["id"]


def test_the_schedule_is_deterministic_and_periodic():
    s = cb.FrameSchedule("car", "leaf_ze0", bus_load=1.0, t0=0.0)
    counts = {}
    t = 0.0
    while t < 10.0:
        for cid in s.due(t):
            counts[cid] = counts.get(cid, 0) + 1
        t = round(t + 0.001, 6)
    assert counts["174"] == pytest.approx(1000, abs=2)          # 10 ms
    assert counts["284"] == pytest.approx(500, abs=2)           # 20 ms
    assert counts["421"] == pytest.approx(167, abs=2)           # 60 ms (surveyed)
    assert counts["5A9"] == pytest.approx(20, abs=1)            # 500 ms
    assert counts["002"] == pytest.approx(1000, abs=2)          # a filler at full load
    half = cb.FrameSchedule("car", "leaf_ze0", bus_load=0.5, t0=0.0)
    c2 = {}
    t = 0.0
    while t < 10.0:
        for cid in half.due(t):
            c2[cid] = c2.get(cid, 0) + 1
        t = round(t + 0.001, 6)
    assert c2["002"] == pytest.approx(500, abs=2) and c2["174"] == pytest.approx(1000, abs=2)
    off = cb.FrameSchedule("car", "leaf_ze0", bus_load=0.0, t0=0.0)
    ids = set()
    for k in range(2000):
        ids.update(off.due(k * 0.001))
    assert "002" not in ids and "174" in ids


def test_a_stalled_schedule_resyncs_instead_of_bursting():
    s = cb.FrameSchedule("car", "leaf_ze0", bus_load=0.0, t0=0.0)
    s.due(0.0)
    burst = s.due(5.0)                                         # five seconds later
    assert burst.count("174") == 1
    assert s.next["174"] > 5.0


# ── the live ECU ─────────────────────────────────────────────────────────

def test_live_frame_rates_are_within_20_percent_of_the_survey():
    chan = channel()
    spy = Spy(chan)
    spy.start()
    ecu = cb.SimCanEcu(quiet_sim(), bus="car", channel=chan, bus_load=0.0, clock=False)
    ecu.start()
    try:
        time.sleep(1.5)
    finally:
        ecu.stop()
        spy.stop()
    n = spy.counts
    assert n["174"] == pytest.approx(150, rel=0.2)             # 10 ms
    assert n["284"] == pytest.approx(75, rel=0.2)              # 20 ms
    assert n["421"] == pytest.approx(25, rel=0.2)              # 60 ms
    assert n["60D"] == pytest.approx(15, rel=0.2)              # 100 ms
    assert "002" not in n, "filler at bus_load 0"
    assert ecu.sent == sum(n.values())


def test_full_load_puts_about_the_surveyed_volume_on_each_bus():
    chan_c, chan_e = channel(), channel()
    sim = quiet_sim()
    sc, se = Spy(chan_c), Spy(chan_e)
    sc.start()
    se.start()
    car = cb.SimCanEcu(sim, bus="car", channel=chan_c, bus_load=1.0, clock=False).start()
    ev = cb.SimCanEcu(sim, bus="ev", channel=chan_e, bus_load=1.0, clock=False).start()
    try:
        time.sleep(1.0)
    finally:
        car.stop()
        ev.stop()
        sc.stop()
        se.stop()
    assert sum(sc.counts.values()) == pytest.approx(cb.expected_fps("car"), rel=0.2)
    assert sum(se.counts.values()) == pytest.approx(cb.expected_fps("ev"), rel=0.2)
    assert "1DB" in se.counts and se.counts["1DB"] == pytest.approx(100, rel=0.2)
    assert "1DB" not in sc.counts and "174" not in se.counts    # two buses, two id sets


def test_the_ecu_is_the_models_clock_when_asked():
    sim = quiet_sim()
    t0 = sim.model.t
    ecu = cb.SimCanEcu(sim, bus="car", channel=channel(), bus_load=0.0, clock=True).start()
    time.sleep(0.3)
    ecu.stop()
    assert sim.model.t > t0 + 0.2
    frozen = quiet_sim()
    ecu = cb.SimCanEcu(frozen, bus="car", channel=channel(), bus_load=0.0, clock=False).start()
    time.sleep(0.2)
    ecu.stop()
    assert frozen.model.t == 0.0


def test_the_bus_load_knob_scales_the_filler_live():
    chan = channel()
    sim = quiet_sim(bus_load=1.0)
    assert "bus_load" in sim.knob_schema() and sim.knob_schema()["bus_load"]["category"] == "rig"
    spy = Spy(chan)
    spy.start()
    ecu = cb.SimCanEcu(sim, bus="car", channel=chan, clock=False).start()
    try:
        time.sleep(0.4)
        assert "002" in spy.counts, "filler at the knob's default 1.0"
        sim.set(bus_load=0.0)
        time.sleep(0.1)
        spy.counts.clear()
        time.sleep(0.4)
        assert "002" not in spy.counts and spy.counts.get("174", 0) > 20
    finally:
        ecu.stop()
        spy.stop()
    assert sim.state()["bus_load"] == 0.0


def test_a_sleeping_car_puts_nothing_on_the_bus_and_answers_nothing():
    chan = channel()
    sim = quiet_sim(**{"fault.car_asleep": True})
    spy = Spy(chan)
    spy.start()
    ecu = cb.SimCanEcu(sim, bus="car", channel=chan, clock=False).start()
    elm = facade(chan)
    run(elm.connect(log=lambda *a: None))
    try:
        time.sleep(0.3)
        assert spy.counts == {}
        run(configure_uds(elm, "79B", "7BB"))
        assert run(elm.send("2101", timeout=2.0)) == ["NO DATA"]
    finally:
        run(elm.close())
        ecu.stop()
        spy.stop()


def test_bus_noise_puts_error_frames_on_the_bus_and_the_facade_counts_them():
    chan = channel()
    sim = quiet_sim(**{"fault.bus_noise": True})
    ecu = cb.SimCanEcu(sim, bus="car", channel=chan, clock=False).start()
    elm = facade(chan)
    run(elm.connect(log=lambda *a: None))
    try:
        time.sleep(0.5)
        assert elm.errors > 0 and elm.frames > elm.errors
    finally:
        run(elm.close())
        ecu.stop()


# ── payload shapes (EV-CAN, ASSERTED) ────────────────────────────────────

def test_ev_payloads_round_trip_through_their_own_decoders():
    st = quiet_sim(soc=61.7, gear="D", start_state="ready", speed_mph=35.0, accel_pedal_pct=40.0).state()
    d = cb.enc_1db(st)
    assert len(d) == 8 and d[7] == cb.crc8_nissan(d[:7])
    back = cb.decode_1db(d)
    assert back["current_a"] == pytest.approx(st["current_a"], abs=0.5)     # 0.5 A / LSB
    assert back["pack_v"] == pytest.approx(st["pack_v"], abs=0.5)           # 0.5 V / LSB
    assert back["discharging"] is True and back["main_relay_on"] is True
    assert st["current_a"] < 0 and d[0] & 0x80, "discharge is negative, two's complement"
    r = cb.decode_1da(cb.enc_1da(st))
    assert r["rpm"] == pytest.approx(cb.motor_rpm(35.0), abs=1.0)
    assert r["torque_nm"] > 0 and r["inverter_v"] == pytest.approx(st["pack_v"], abs=0.5)
    rev = quiet_sim(gear="R", start_state="ready", speed_mph=4.0, accel_pedal_pct=20.0).state()
    assert cb.decode_1da(cb.enc_1da(rev))["rpm"] < 0
    assert cb.decode_55b(cb.enc_55b(st))["soc"] == pytest.approx(61.7, abs=0.1)
    g = cb.decode_11a(cb.enc_11a(st))
    assert g["gear"] == "D" and g["eco"] is False and g["car_on"] == 3
    eco = cb.decode_11a(cb.enc_11a(quiet_sim(gear="Eco").state()))
    assert eco["gear"] == "D" and eco["eco"] is True
    lim = cb.decode_1dc(cb.enc_1dc(st))
    assert 80 < lim["discharge_limit_kw"] < 100 and 0 <= lim["charge_limit_kw"] <= 30
    assert lim["charger_max_kw"] == pytest.approx(0.0, abs=0.11)
    q = cb.decode_1d4(cb.enc_1d4(st))
    assert q["torque_request_nm"] > 0 and q["hv_on"] is True and q["charging"] is False
    braking = quiet_sim(gear="D", start_state="ready", speed_mph=30.0, brake_pct=50.0).state()
    assert cb.decode_1d4(cb.enc_1d4(braking))["torque_request_nm"] < 0
    b = cb.decode_5bc(cb.enc_5bc(st))
    assert b["soh_pct"] == round(st["soh"]) and b["temp_avg_c"] == round(st["temp_avg_c"])
    assert 0 < b["gids"] < 300 and b["charge_minutes"] is None
    for cid in cb.EV_MODELLED:
        assert len(cb.frame_bytes("ev", cid, st)) == 8


def test_car_payloads_come_from_the_one_encoder_with_their_dlc():
    st = quiet_sim(gear="N").state()
    assert cb.frame_bytes("car", "421", st) == [0x18, 0x00, 0x00]      # DLC 3, the encoder's byte
    assert len(cb.frame_bytes("car", "355", st)) == 7
    assert len(cb.frame_bytes("car", "625", st)) == 6
    f = cb.frame_bytes("car", "002", st, counter=5)
    assert len(f) == 8 and f[7] & 0x0F == 5                           # filler with the counter nibble
    assert cb.frame_bytes("car", "245", st) == cb.FILLER_BYTES["245"][:7] + [0xE2 & 0xF0 | 0]
    assert cb.frame_bytes("car", "1D5", st) == cb.FILLER_BYTES["1D5"]  # 5 bytes, as this car sends it
    assert cb.frame_bytes("car", "7FF", st) is None
    assert cb.crc8_nissan(b"") == 0 and cb.crc8_nissan(bytes(7)) == 0
    assert cb.crc8_nissan(b"\x01") == 0x85


# ── UDS through the façade ───────────────────────────────────────────────

def _lines_via_can(chan, sim, cmds, target=("79B", "7BB")):
    elm = facade(chan)
    run(elm.connect(log=lambda *a: None))
    try:
        run(configure_uds(elm, *target))
        return {c: run(elm.send(c, timeout=5.0)) for c in cmds}, elm
    finally:
        run(elm.close())


def _lines_via_elm(sim, cmds, target=("79B", "7BB")):
    e = SimELM(sim=sim, seed=1)
    e._last = float("inf")                       # never step: the same frozen state as the ECU's
    run(set_uds_target(e, *target))
    return {c: run(e.send(c)) for c in cmds}


def test_a_uds_answer_through_the_facade_decodes_like_the_elm_sim_path():
    """model → ECU frames → façade → decode == model → SimELM lines → decode."""
    chan = channel()
    sim = quiet_sim(soc=42.5, current_a=-80.0, capacity_ah=61.25)
    ecu = cb.SimCanEcu(sim, bus="car", channel=chan, bus_load=0.0, clock=False).start()
    try:
        cmds = ("2101", "2102", "2104", "2105", "2106")
        via_can, elm = _lines_via_can(chan, sim, cmds)
        via_elm = _lines_via_elm(sim, cmds)
        for c in cmds:
            assert via_can[c] == via_elm[c], c                       # byte for byte, frame for frame
            assert via_can[c][0].startswith("7BB ")
        rc, re_ = decode_reading(via_can), decode_reading(via_elm)
        assert rc == re_
        assert rc["soc"] == pytest.approx(42.5, abs=0.01)
        assert rc["hv_current2_a"] == pytest.approx(-80.0, abs=0.05)
        assert len(rc["cells"]) == 96
        assert not elm.misses
        assert ecu.fc and ecu.fc[-1] == (0, 0)                       # the façade's STmin 0
    finally:
        ecu.stop()


def test_the_hvac_amp_answers_on_764_and_unknown_groups_get_the_amps_nrc():
    chan = channel()
    sim = quiet_sim(hvac_on=True, hvac_fan_speed=3, cabin_temp_c=27)
    ecu = cb.SimCanEcu(sim, bus="car", channel=chan, bus_load=0.0, clock=False).start()
    try:
        via_can, _ = _lines_via_can(chan, sim, ("2110", "2199"), target=("744", "764"))
        via_elm = _lines_via_elm(sim, ("2110", "2199"), target=("744", "764"))
        assert via_can["2110"] == via_elm["2110"] and via_can["2110"][0].startswith("764 10 ")
        assert via_can["2199"] == ["764 03 7F 21 12"] == via_elm["2199"]
    finally:
        ecu.stop()


def test_anything_but_a_read_gets_a_negative_response_not_data():
    chan = channel()
    sim = quiet_sim()
    ecu = cb.SimCanEcu(sim, bus="car", channel=chan, bus_load=0.0, clock=False).start()
    spy = can.Bus(interface="virtual", channel=chan, receive_own_messages=False)
    try:
        for svc, req in ((0x2E, [0x03, 0x2E, 0x01, 0x00]), (0x10, [0x02, 0x10, 0x03]),
                         (0x27, [0x02, 0x27, 0x01]), (0x31, [0x04, 0x31, 0x01, 0xFF, 0x00])):
            spy.send(can.Message(arbitration_id=0x79B, data=bytes(req + [0] * (8 - len(req))),
                                 is_extended_id=False))
            t0 = time.monotonic()
            got = None
            while time.monotonic() - t0 < 1.0:
                m = spy.recv(0.05)
                if m is not None and m.arbitration_id == 0x7BB:
                    got = bytes(m.data)
                    break
            assert got is not None and got[:4] == bytes([0x03, 0x7F, svc, 0x11]), hex(svc)
        assert [s for _, s in ecu.refused] == [0x2E, 0x10, 0x27, 0x31]
        assert sim.get_knobs() == quiet_sim().get_knobs()             # the model was never touched
    finally:
        spy.shutdown()
        ecu.stop()


def test_the_ecu_honours_block_size_and_separation_time():
    chan = channel()
    sim = quiet_sim()
    ecu = cb.SimCanEcu(sim, bus="car", channel=chan, bus_load=0.0, clock=False).start()
    elm = facade(chan)
    run(elm.connect(log=lambda *a: None))
    try:
        run(configure_uds(elm, "79B", "7BB"))
        run(elm.send("ATFCSD 30 04 05"))                           # BS 4, STmin 5 ms
        t0 = time.monotonic()
        lines = run(elm.send("2102", timeout=5.0))
        elapsed = time.monotonic() - t0
        assert len(lines) == 29 and len(parse_isotp(lines)) >= 196
        blocks = [fc for fc in ecu.fc if fc == (4, 5)]
        assert len(blocks) >= 7, ecu.fc                            # 28 consecutive frames in blocks of 4
        assert elapsed >= 28 * 0.005 * 0.8                        # STmin actually waited
    finally:
        run(elm.close())
        ecu.stop()


def test_a_uds_pair_comes_from_the_profile_not_from_the_rig():
    sim = quiet_sim()
    ecu = cb.SimCanEcu(sim, bus="car", channel=channel(), clock=False)
    assert ecu.targets == {0x79B: ("79B", "7BB"), 0x744: ("744", "764")}
    assert ecu.uds is True
    ev = cb.SimCanEcu(sim, bus="ev", channel=channel(), clock=False)
    assert ev.uds is False                                        # EV-CAN answers nothing: listen-only there
    assert cb.SimCanEcu(sim, bus="car", channel=channel(), targets={}).targets == {}


# ── the transport: --adapter sim --sim-can ───────────────────────────────

def test_the_sim_can_facade_is_labelled_sim_and_stamps_every_record():
    sim = quiet_sim()
    elm = run(ct.open_sim_can(log=lambda *a: None, mode="car", sim=sim, control_port=None, bus_load=0.2,
                              scenario="idle", seed=7))
    try:
        assert isinstance(elm, ct.SimCanFacade) and isinstance(elm.source, ct.SimCanSource)
        assert elm.adapter_type == "sim" and elm.simulated is True
        assert elm.SPEED == ct.CanFacade.SPEED and elm.PASSIVE_INSTANT is True
        assert any(c.isdigit() for c in elm.adapter_name)
        m = elm.marker()
        assert m["simulated"] is True and m["sim_scenario"] == "idle" and m["sim_seed"] == 7
        assert m["sim_vehicle"] == "leaf_ze0" and m["sim_bus_load"] == 0.2
        assert m["can_bus"] == "car" and m["listen_only"] is False
        assert m["sim_transport"].startswith("can sim-can:")
        assert len(elm.source.ecus) == 1 and elm.source.ecus[0].bus == "car"
        assert rd.sim_db().endswith("sim_leaf_ze0.db")            # where `--adapter sim` files its rows
    finally:
        run(elm.close())
    assert elm.source.ecus == []


def test_detect_adapter_sim_with_sim_can_builds_the_can_facade(monkeypatch):
    monkeypatch.setenv("HAKAKE_SIM_CAN", "car")
    monkeypatch.setenv("HAKAKE_SIM_SCENARIO", "idle")
    monkeypatch.setenv("HAKAKE_SIM_SEED", "3")
    monkeypatch.setenv("HAKAKE_SIM_KNOBS", '{"bus_load": 0.1, "noise": 0}')
    monkeypatch.delenv("HAKAKE_SIM_CONTROL_PORT", raising=False)
    monkeypatch.delenv("HAKAKE_SIM_SERIAL", raising=False)
    said = []
    elm = run(elm327.detect_adapter(prefer="sim", log=said.append))
    try:
        assert isinstance(elm, ct.SimCanFacade) and elm.adapter_type == "sim"
        assert elm.seed == 3 and elm.scenario == "idle"
        assert elm.sim.get_knobs()["bus_load"] == 0.1
        blob = " ".join(said)
        assert "SIMULATED DATA" in blob and "ASSERTED" in blob
    finally:
        run(elm.close())


def test_sim_can_mode_parses_the_environment(monkeypatch):
    monkeypatch.delenv("HAKAKE_SIM_CAN", raising=False)
    assert ct.sim_can_mode() is None
    assert ct.sim_can_mode("1") == "car" and ct.sim_can_mode("car") == "car" and ct.sim_can_mode("ev") == "ev"
    assert ct.sim_can_mode("0") is None
    with pytest.raises(ConnectionError):
        ct.sim_can_mode("both")


def test_can_interface_sim_through_adapter_can_is_refused_with_advice(monkeypatch):
    """Generated rows must never land in web/leaf_battery.db: the reader picks
    the database from --adapter, so the simulated bus is `--adapter sim --sim-can`."""
    monkeypatch.delenv("HAKAKE_CAN_BUS", raising=False)
    monkeypatch.delenv("HAKAKE_CAN_INTERFACE", raising=False)
    with pytest.raises(ConnectionError, match="--sim-can"):
        run(ct.open_can({"can_bus": "car", "can_interface": "sim"}, log=lambda *a: None))


def test_auto_detect_never_picks_the_simulated_bus(monkeypatch):
    monkeypatch.setenv("HAKAKE_SIM_CAN", "car")
    monkeypatch.setattr(elm327, "_find_serial_port", lambda: None)

    async def no_ble(self, log=print):
        raise ConnectionError("no BLE in tests")

    monkeypatch.setattr(elm327.BleELM, "connect", no_ble)
    with pytest.raises(ConnectionError):
        run(elm327.detect_adapter(prefer=None, log=lambda *a: None))


def test_one_reader_cycle_over_the_simulated_bus_stores_a_simulated_row(isolated_reader, leaf_profile, tmp_store):
    sim = quiet_sim(gear="D", start_state="ready", speed_mph=30.0, accel_pedal_pct=30.0)
    elm = run(ct.open_sim_can(log=lambda *a: None, mode="car", sim=sim, control_port=None, bus_load=1.0))
    try:
        time.sleep(0.3)
        run(rd.configure_vehicle(elm))
        r = rd.Reader(interval=0, adapter_pref="sim", store=tmp_store, budget=1.5)
        r.speed = elm.SPEED
        r.passive_instant = elm.PASSIVE_INSTANT
        rec, alive = run(r.poll_once(elm))
        assert alive is True
        assert rec["gear"] == "D" and rec["current_a"] < -20 and rec["soc"] > 0
        assert rec["speed_mph"] == pytest.approx(30.0, abs=0.5)
        assert rec["timing"]["p421"] < 0.05 and rec["timing"]["lbc01"] < 0.5
        rec.update(elm.marker())
        rid = tmp_store.insert_reading(rec, adapter=elm.adapter_type)
        row = tmp_store.conn.execute("SELECT adapter, extra FROM readings WHERE id=?", (rid,)).fetchone()
        assert row["adapter"] == "sim" and '"simulated": true' in row["extra"]
        assert '"sim_bus_load": 1.0' in row["extra"]
    finally:
        run(elm.close())


def test_the_ev_channel_is_listen_only_and_carries_the_asserted_ids():
    sim = quiet_sim(gear="D", start_state="ready", speed_mph=20.0, accel_pedal_pct=30.0)
    elm = run(ct.open_sim_can(log=lambda *a: None, mode="ev", sim=sim, control_port=None, bus="ev",
                              bus_load=0.0))
    try:
        assert elm.listen_only is True and elm.bus == "ev"
        assert {e.bus for e in elm.source.ecus} == {"car", "ev"}
        time.sleep(0.3)
        run(configure_uds(elm, "79B", "7BB"))
        assert run(elm.send("2101", timeout=2.0)) == ["NO DATA"]        # nothing is sent on EV-CAN
        assert elm.source.ecus[0].requests == []
        lines = run(passive_capture(elm, "1DB", 0.2))
        assert lines and lines[0].startswith("1DB ")
        d = cb.decode_1db(bytes(int(b, 16) for b in lines[-1].split()[1:]))
        assert d["current_a"] < -10 and 300 < d["pack_v"] < 420
        assert elm.marker()["listen_only"] is True and elm.marker()["can_bus"] == "ev"
    finally:
        run(elm.close())


def test_sim_can_and_sim_serial_are_exclusive_flags():
    p = subprocess.run([sys.executable, os.path.join(ROOT, "web", "reader.py"), "--adapter", "sim",
                        "--sim-can", "--sim-serial", "/dev/null"], capture_output=True, text=True,
                       cwd=ROOT, timeout=60)
    assert p.returncode == 2 and "pick one" in p.stderr
    for script in ("web/reader.py", "web/app.py"):
        h = subprocess.run([sys.executable, os.path.join(ROOT, script), "--help"], capture_output=True,
                           text=True, cwd=ROOT, timeout=60).stdout
        assert "--sim-can" in h
