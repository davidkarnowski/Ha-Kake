# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SimELM — the transport that serves a *generated* car instead of a real one.

Replay's tests guard honesty about recorded data. These guard the stronger
claim the simulator makes: that a running model can drive the whole stack, and
that nothing it produces can be mistaken for a reading from a vehicle.

Everything here runs against the contract surface (tests/sim_stub.py), so the
transport cannot quietly grow a dependency on the core's internals. Where the
real core is importable it is checked against the same contract at the end.
"""

import asyncio
import json
import os
import time

import pytest

from conftest import ROOT  # noqa: F401  (sys.path is set up there)

import elm327  # noqa: E402
from elm327 import SimELM, _cra_filter, passive_capture, set_uds_target  # noqa: E402
from sim_stub import StubSim, make_sim  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sim():
    return make_sim(vehicle="leaf_ze0", seed=1)


@pytest.fixture
def elm(sim):
    e = SimELM(sim=sim, scenario="idle", seed=1)
    run(e.connect(log=lambda *a: None))
    return e


# ── the ELM327 state machine ─────────────────────────────────────────────

def test_it_looks_like_an_adapter(elm):
    assert elm.adapter_type == "sim"
    assert elm.simulated is True
    assert run(elm.send("ATI")) == ["ELM327 v1.5"]
    assert run(elm.send("ATZ")) == ["ELM327 v1.5"]


def test_at_setup_commands_are_accepted_silently(elm):
    for cmd in ("ATE0", "ATL1", "ATH1", "ATS1", "ATSP6", "ATFCSD 30 00 20", "ATFCSM1"):
        assert run(elm.send(cmd)) == []


def test_atsh_atcra_atcaf_are_remembered(elm):
    run(set_uds_target(elm, "79B", "7BB"))
    assert (elm.tx, elm.rx, elm.caf) == ("79B", "7BB", True)
    run(elm.send("ATCAF0"))
    assert elm.caf is False
    run(elm.send("ATAR"))
    assert elm.rx is None


def test_a_uds_request_reaches_the_model(elm):
    run(set_uds_target(elm, "79B", "7BB"))
    lines = run(elm.send("2101"))
    assert lines and lines[0].startswith("7BB ")


def test_unknown_requests_answer_no_data_and_are_counted(elm):
    """The transport invents nothing. Only the model may make a value up, and
    the model says so; a request it has no answer for gets what a silent car
    would give."""
    run(set_uds_target(elm, "79B", "7BB"))
    assert run(elm.send("22FFFF")) == ["NO DATA"]
    assert "79B:22FFFF" in elm.misses


def test_the_atcra_filter_is_applied_like_hardware():
    lines = ["7BB 10 29", "7BC 10 29", "421 08 00"]
    assert _cra_filter("7BB", lines) == ["7BB 10 29"]
    assert _cra_filter(None, lines) == lines


def test_passive_capture_drives_atma_through_the_model(elm):
    frames = run(passive_capture(elm, "421", seconds=0.5))
    assert frames and all(f.startswith("421 ") for f in frames)
    assert elm.caf is False, "passive_capture must leave the adapter in CAF0"


def test_a_silent_car_produces_no_data_not_a_guess(elm, sim):
    sim.set(**{"fault.car_asleep": True})
    run(set_uds_target(elm, "79B", "7BB"))
    assert run(elm.send("2101")) == ["NO DATA"]
    assert run(passive_capture(elm, "421", seconds=0.2)) == []


def test_a_uds_round_trip_survives_the_real_decoders(elm, sim):
    """model → encode → adapter → decode → compare. This is the round trip the
    whole exercise is for, and also the limit of it: it proves the encoder and
    the decoder agree about the byte layout. It would *not* have caught the
    group-05 ÷1024-vs-×0.001 bug, because an error shared by both sides is
    invisible from in here. A green run is not verification against a car.
    """
    from leaf_decoders import decode_group01, parse_isotp

    sim.set(soc=42.5, current_a=-80, capacity_ah=61.25)
    run(set_uds_target(elm, "79B", "7BB"))
    got = decode_group01(parse_isotp(run(elm.send("2101"))))
    assert got["soc"] == pytest.approx(42.5, abs=0.01)
    assert got["capacity_ah"] == pytest.approx(61.25, abs=0.001)
    assert got["hv_current1_a"] == pytest.approx(-80.0, abs=0.01)


def test_a_fault_knob_changes_what_comes_off_the_wire(elm, sim):
    """The argument for a simulator in one test: a degraded cell cannot be
    ordered up in a real car, and here it is, in the decoded cell voltages."""
    from leaf_decoders import decode_group02, parse_isotp

    run(set_uds_target(elm, "79B", "7BB"))
    healthy = decode_group02(parse_isotp(run(elm.send("2102"))))
    sim.set(**{"fault.cell_degraded": True})
    degraded = decode_group02(parse_isotp(run(elm.send("2102"))))
    assert max(healthy) - min(healthy) < max(degraded) - min(degraded)
    assert min(degraded) < min(healthy) - 100


# ── the clock ────────────────────────────────────────────────────────────

def test_time_advances_the_model_once_per_command(elm, sim):
    before = sim.steps
    elm.advance(elm._last + 1.0)
    assert sim.steps == before + 1
    assert elm.sim_time == pytest.approx(1.0)


def test_speed_scales_the_clock(sim):
    e = SimELM(sim=sim, speed=10)
    run(e.connect(log=lambda *a: None))
    e.advance(e._last + 1.0)
    assert e.sim_time == pytest.approx(10.0)


def test_the_clock_only_moves_when_asked(elm, sim):
    """A paused reader must not silently drive the car for an hour: the model
    advances from advance() and nowhere else, so real time passing with no
    traffic on the bus changes nothing until the next command."""
    steps, t = sim.steps, sim.t
    time.sleep(0.05)
    assert (sim.steps, sim.t) == (steps, t)
    run(elm.send("ATI"))
    assert sim.steps > steps and sim.t > t


# ── labelling: nothing here can pass for a real reading ──────────────────

def test_the_marker_names_the_scenario_and_seed(elm):
    m = elm.marker()
    assert m["simulated"] is True
    assert m["sim_scenario"] == "idle" and m["sim_seed"] == 1
    assert m["sim_vehicle"] == "leaf_ze0"


def test_connect_log_shouts_that_it_is_simulated(sim):
    lines = []
    run(SimELM(sim=sim, scenario="drive", seed=7).connect(log=lines.append))
    blob = " ".join(lines)
    assert "SIMULATOR" in blob and "SIMULATED DATA" in blob
    assert "drive" in blob and "seed: 7" in blob


def test_adapter_name_says_sim(sim):
    e = SimELM(sim=sim, scenario="degraded_pack")
    assert "sim" in e.adapter_name and "degraded_pack" in e.adapter_name
    assert e.adapter_port.startswith("sim:")


# ── detect_adapter wiring ────────────────────────────────────────────────

@pytest.fixture
def stub_factory(monkeypatch):
    seen = {}

    def fake(vehicle=None, scenario=None, seed=None, knobs=None):
        seen.update(vehicle=vehicle, scenario=scenario, seed=seed, knobs=knobs)
        return make_sim(vehicle=vehicle or "leaf_ze0", knobs=knobs, seed=seed, scenario=scenario)

    monkeypatch.setattr(elm327, "make_simulator", fake)
    return seen


def test_detect_adapter_sim_reads_the_environment(monkeypatch, stub_factory):
    monkeypatch.setenv("HAKAKE_SIM_SCENARIO", "drive")
    monkeypatch.setenv("HAKAKE_SIM_SEED", "42")
    monkeypatch.setenv("HAKAKE_SIM_SPEED", "5")
    monkeypatch.setenv("HAKAKE_SIM_KNOBS", json.dumps({"soc": 15}))
    e = run(elm327.detect_adapter(prefer="sim", log=lambda *a: None))
    assert e.adapter_type == "sim" and e.simulated is True
    assert e.scenario == "drive" and e.seed == 42 and e.speed == 5.0
    assert stub_factory["knobs"] == {"soc": 15}
    assert e.sim.get_knobs()["soc"] == 15
    run(e.close())


def test_bad_knob_env_is_a_clear_error_not_a_traceback(monkeypatch, stub_factory):
    monkeypatch.setenv("HAKAKE_SIM_KNOBS", "{not json")
    with pytest.raises(ConnectionError, match="HAKAKE_SIM_KNOBS"):
        run(elm327.detect_adapter(prefer="sim", log=lambda *a: None))


def test_auto_detect_never_picks_the_simulator(monkeypatch, stub_factory):
    """The rule replay set, restated. If auto-detect could fall back to a
    model, a dashboard with a flat battery and a dead adapter would quietly
    show a healthy imaginary car."""
    monkeypatch.setattr(elm327, "_find_serial_port", lambda: None)

    async def no_ble(self, log=print):
        raise ConnectionError("no BLE in tests")

    monkeypatch.setattr(elm327.BleELM, "connect", no_ble)
    with pytest.raises(ConnectionError) as e:
        run(elm327.detect_adapter(prefer=None, log=lambda *a: None))
    assert "sim" not in str(e.value).lower().replace("simulated", "")


def test_missing_core_is_a_readable_error(monkeypatch):
    """--adapter sim without simulator/ must say what to do, not ImportError."""
    import builtins
    real = builtins.__import__

    def blocked(name, *a, **kw):
        if name == "simulator" or name.startswith("simulator."):
            raise ImportError("No module named 'simulator'")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ConnectionError, match="simulator core"):
        elm327.make_simulator(vehicle="leaf_ze0")


# ── the serial-port override that makes the pty usable ───────────────────

def test_serial_port_env_overrides_the_usb_glob(monkeypatch):
    monkeypatch.setenv("HAKAKE_SERIAL_PORT", "/dev/ttys999")
    assert elm327._find_serial_port() == "/dev/ttys999"


# ── the pty front end, and the real serial transport behind it ───────────

def test_sim_serial_is_a_real_serial_transport_that_still_says_simulated():
    """`--adapter sim --sim-serial /dev/ttysNNN` is the honest way to point the
    dashboard at a pty. Going through `--adapter usb` instead would work — and
    would write generated rows into web/leaf_battery.db, next to 12,000 real
    ones. So the mode stays "sim": real pyserial, real tty, simulated stamp,
    throwaway database."""
    e = elm327.SimSerialELM(port="/dev/ttys999")
    assert isinstance(e, elm327.SerialELM)
    assert e.simulated is True and e.adapter_type == "usb"
    m = e.marker()
    assert m["simulated"] is True and m["sim_scenario"] == "pty"
    assert "/dev/ttys999" in m["sim_transport"]


def test_detect_adapter_sim_serial_uses_the_serial_transport(monkeypatch):
    seen = {}

    async def fake_connect(self):
        seen["port"] = self.port
        self.adapter_port = self.port
        self.adapter_name = "ELM327 v1.5"

    monkeypatch.setenv("HAKAKE_SIM_SERIAL", "/dev/ttys999")
    monkeypatch.setattr(elm327.SimSerialELM, "connect", fake_connect)
    e = run(elm327.detect_adapter(prefer="sim", log=lambda *a: None))
    assert isinstance(e, elm327.SimSerialELM) and seen["port"] == "/dev/ttys999"
    assert e.simulated is True


# ── the pty front end ────────────────────────────────────────────────────

def test_pty_publishes_a_real_device_and_speaks_elm327(sim):
    """The strongest form of 'looks like a car': a real tty, opened by the
    real serial transport, answering ELM327."""
    import serial as pyserial
    from hakake_sim import PtyServer

    pty = PtyServer(SimELM(sim=sim), monitor_window=0.1, log=lambda *a: None)
    path = pty.open()
    try:
        assert os.path.exists(path)
        ser = pyserial.Serial(path, 38400, timeout=1)
        try:
            ser.write(b"ATI\r")
            pty.pump(0.5)
            assert b"ELM327" in ser.read(64)
        finally:
            ser.close()
    finally:
        pty.close()


def test_pty_wire_format_is_what_serialelm_parses(sim):
    from hakake_sim import PtyServer

    pty = PtyServer(SimELM(sim=sim), monitor_window=0.1, log=lambda *a: None)
    out = pty.feed(b"ATE0\rATSH 79B\rATCRA 7BB\r2101\r")
    assert out.endswith(b">")
    assert out.count(b">") == 4, "one prompt per command"
    assert b"OK\r" in out                      # AT commands answer OK
    assert b"7BB 10 29" in out                 # the model's line, verbatim
    assert pty.feed(b"\r") == b"\r>", "a bare CR interrupts monitor mode"


def test_pty_handles_a_command_split_across_reads(sim):
    from hakake_sim import PtyServer

    pty = PtyServer(SimELM(sim=sim), log=lambda *a: None)
    assert pty.feed(b"AT") == b""              # nothing until the CR
    assert b"ELM327" in pty.feed(b"I\r")


# ── the real core, when it is there ──────────────────────────────────────

def _core():
    """The real core, or None while it is still being written.

    `make_sim` is the contract's entry point, so a half-built package that
    imports but does not export it counts as not there yet.
    """
    try:
        import simulator as s
    except ImportError:
        return None
    return s if hasattr(s, "make_sim") else None


@pytest.mark.skipif(_core() is None, reason="simulator core not present yet")
def test_the_real_core_satisfies_the_contract():
    """Whatever the core does inside, these calls are what everything else in
    the project is written against."""
    s = _core()
    sim = s.make_sim(vehicle="leaf_ze0", knobs={}, seed=1, scenario=None)
    for name in ("step", "state", "set", "get_knobs", "knob_schema",
                 "respond", "frames", "load_scenario", "faults"):
        assert callable(getattr(sim, name)), f"contract requires sim.{name}()"
    assert hasattr(sim, "seed") and hasattr(sim, "vehicle")
    sim.step(1.0)
    assert isinstance(sim.state(), dict)
    schema = sim.knob_schema()
    assert isinstance(schema, dict) and schema
    for k, spec in schema.items():
        assert {"type", "default", "help"} <= set(spec), f"knob {k} under-described"
    json.dumps(sim.state(), default=str)       # must be JSON-serialisable
    with pytest.raises(ValueError):
        sim.set(**{"no_such_knob_at_all": 1})


@pytest.mark.skipif(_core() is None, reason="simulator core not present yet")
def test_the_real_core_drives_the_transport_end_to_end():
    s = _core()
    e = SimELM(sim=s.make_sim(vehicle="leaf_ze0", knobs={}, seed=1, scenario=None),
               vehicle="leaf_ze0", seed=1)
    run(e.connect(log=lambda *a: None))
    run(set_uds_target(e, "79B", "7BB"))
    lines = run(e.send("2101"))
    assert lines and lines != ["NO DATA"], "the Leaf model must answer 79B/2101"
    run(e.close())
