# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One clock, one number, said out loud.

There are two ways to ask the simulator for compressed time and they used to
MULTIPLY. A scenario carries a `clock_scale`; `--speed` on top of it produced
the PRODUCT of the two — an effective 14400x on the charge scenario, so eight
seconds of startup was thirty-two simulated hours and the charge was over
before the first sample. Nothing said so.

Every shipped scenario now runs at real time except `degradation_arc`, which
keeps its 3600x because two years of ageing at 1x is two years of waiting.
That makes it the one with a clock to shadow, so it is the one these tests
load: CLOCKED below, and CLOCK is the number it ships.

The rule, tested here:

  * `--speed X` given explicitly  ->  the effective multiplier IS X. It
    OVERRIDES the scenario's clock_scale; it does not multiply it.
  * no `--speed`                  ->  the scenario's clock_scale knob.
  * neither                       ->  real time.

and the effective number is reported in `state()`, in `/sim/info`, and on the
startup banner, so it can never be silent again.
"""

import json
import os
import subprocess
import sys
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# the one shipped scenario that still carries a clock, and the clock it carries
CLOCKED = "degradation_arc"
CLOCK = 3600.0

from conftest import ROOT                                        # noqa: E402
import hakake_sim                                                # noqa: E402
from simulator import TIME_SCALE_MAX, TIME_SCALE_MIN, make_sim   # noqa: E402


def elapsed(sim, wall_seconds=1.0, ticks=1):
    before = sim.state()["t"]
    for _ in range(ticks):
        sim.step(wall_seconds)
    return sim.state()["t"] - before


# ── the resolution rule ──────────────────────────────────────────────────

def test_with_neither_the_clock_is_real_time():
    sim = make_sim(vehicle="leaf_ze0")
    assert sim.time_scale() == 1.0
    assert elapsed(sim) == pytest.approx(1.0)


def test_a_scenarios_clock_scale_applies_when_speed_is_not_given():
    sim = make_sim(scenario=CLOCKED)                # ships a clock_scale
    assert sim.get_knobs()["clock_scale"] == CLOCK
    assert sim.time_scale() == CLOCK
    assert elapsed(sim) == pytest.approx(CLOCK)
    assert sim.time_scale_info()["source"] == "clock_scale knob"


def test_speed_overrides_clock_scale_instead_of_multiplying_it():
    """The bug, stated as an assertion. 120 and 120 is 120, not 14400."""
    sim = make_sim(scenario=CLOCKED, speed=120.0)
    assert sim.get_knobs()["clock_scale"] == CLOCK, "the knob keeps the scenario's value"
    assert sim.time_scale() == 120.0, "but the effective multiplier is --speed, once"
    assert elapsed(sim) == pytest.approx(120.0)
    assert elapsed(sim, 1.0, 8) == pytest.approx(960.0), "eight seconds is sixteen minutes"


def test_speed_wins_over_a_clock_scale_set_later_too():
    sim = make_sim(vehicle="leaf_ze0", speed=10.0)
    sim.set(clock_scale=500.0)
    assert sim.time_scale() == 10.0
    assert elapsed(sim) == pytest.approx(10.0)


def test_clearing_the_override_hands_the_clock_back_to_the_knob():
    sim = make_sim(scenario=CLOCKED, speed=2.0)
    assert sim.time_scale() == 2.0
    sim.set_speed(None)
    assert sim.time_scale() == CLOCK


def test_the_clock_scale_knob_still_works_mid_run_when_no_speed_is_given():
    sim = make_sim(vehicle="leaf_ze0")
    sim.set(clock_scale=60.0)
    assert elapsed(sim) == pytest.approx(60.0)


# ── bounds ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("asked,want", [
    (10_000_000.0, TIME_SCALE_MAX), (TIME_SCALE_MAX, TIME_SCALE_MAX),
    (0.0000001, TIME_SCALE_MIN), (1.0, 1.0),
])
def test_the_effective_multiplier_is_clamped_and_says_so(asked, want):
    sim = make_sim(vehicle="leaf_ze0", speed=asked)
    assert sim.time_scale() == want
    info = sim.time_scale_info()
    assert info["max"] == TIME_SCALE_MAX and info["min"] == TIME_SCALE_MIN


def test_an_absurd_clock_scale_knob_is_clamped_the_same_way():
    sim = make_sim(vehicle="leaf_ze0")
    sim.set(clock_scale=99_999.0)                   # the knob clamps at its own max
    assert sim.time_scale() <= TIME_SCALE_MAX


# ── it is never silent ───────────────────────────────────────────────────

def test_the_state_carries_the_effective_multiplier_and_its_source():
    st = make_sim(scenario=CLOCKED, speed=30.0).state()
    assert st["time_scale"] == 30.0
    assert "--speed" in st["time_scale_source"]
    st2 = make_sim(scenario=CLOCKED).state()
    assert st2["time_scale"] == CLOCK and "clock_scale" in st2["time_scale_source"]


def test_overriding_a_scenarios_clock_leaves_a_note_a_human_will_read():
    sim = make_sim(scenario=CLOCKED, speed=30.0)
    assert any("overrides clock_scale" in n for n in sim.notes)
    assert not any("overrides clock_scale" in n for n in make_sim(scenario=CLOCKED).notes)


def test_info_reports_the_clock(tmp_path):
    sim = make_sim(scenario=CLOCKED, speed=45.0)
    httpd = hakake_sim.serve_control(sim, port=0, log=lambda *a: None)
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with urllib.request.urlopen(base + "/sim/info", timeout=5) as r:
            doc = json.loads(r.read().decode())
        assert doc["time_scale"] == 45.0
        assert "--speed" in doc["time_scale_source"]
        assert doc["clock_scale"] == CLOCK
        assert doc["time_scale_max"] == TIME_SCALE_MAX
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_banner_prints_the_effective_multiplier():
    p = subprocess.run([sys.executable, "hakake_sim.py", "--scenario", CLOCKED,
                        "--speed", "30", "--no-control", "--duration", "0.3", "--report", "0"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert "30.0x simulated time" in p.stdout, p.stdout
    assert f"overrides clock_scale {CLOCK}" in p.stdout, p.stdout


def test_json_mode_carries_the_multiplier_for_an_agent():
    p = subprocess.run([sys.executable, "hakake_sim.py", "--scenario", CLOCKED,
                        "--speed", "12", "--no-control", "--json",
                        "--duration", "0.3", "--report", "0"],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    ready = json.loads(p.stdout.splitlines()[0])
    assert ready["time_scale"] == 12.0
    assert "--speed" in ready["time_scale_source"]


# ── the transport does not get to multiply either ────────────────────────

def test_the_sim_transport_does_not_reintroduce_the_multiplication():
    """SimELM applies `--speed` to wall time before the core sees it. The core
    divides that back out, so the product is the resolved scale and nothing
    else — which is why the rig can keep handing SimELM the same number."""
    from elm327 import SimELM
    sim = make_sim(scenario=CLOCKED)                # carries a clock_scale
    sim.set_speed(120.0, outer=120.0)               # what hakake_sim.build_sim does
    elm = SimELM(sim=sim, speed=120.0)
    elm.advance(0.0)
    before = sim.state()["t"]
    elm.advance(1.0)                                # one wall second
    assert sim.state()["t"] - before == pytest.approx(120.0, rel=1e-6)


def test_adapter_sim_speed_arrives_through_the_environment_without_multiplying(monkeypatch):
    """`web/reader.py --speed X` sets HAKAKE_SIM_SPEED and elm327 builds the
    transport from it. The core reads the same variable so the two roads meet
    at one number instead of multiplying."""
    from elm327 import SimELM
    monkeypatch.setenv("HAKAKE_SIM_SPEED", "50")
    sim = make_sim(scenario=CLOCKED)
    assert sim.time_scale() == 50.0, "the environment override beats the scenario clock"
    elm = SimELM(sim=sim, speed=50.0)
    elm.advance(0.0)
    before = sim.state()["t"]
    elm.advance(1.0)
    assert sim.state()["t"] - before == pytest.approx(50.0, rel=1e-6)


def test_an_unparseable_environment_speed_is_ignored_not_fatal(monkeypatch):
    monkeypatch.setenv("HAKAKE_SIM_SPEED", "quickly")
    assert make_sim(scenario=CLOCKED).time_scale() == CLOCK
