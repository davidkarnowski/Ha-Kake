# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`"ramp": true` on a scenario timeline entry.

A timeline is a step function by design, which is right for a gear change and
wrong for a pedal: `pulls.json` moves `speed_mph` every half second and the
current climbs in stairs however fast the dashboard samples it. A ramping
entry interpolates its **numeric** knobs linearly from the previous entry's
time to its own; its text and boolean knobs still snap, because half of `D` is
not a gear.

What is pinned here: the interpolation is a function of absolute simulated
time (so it is the same at every `dt`, every sub-step size and every clock
scale), it lands exactly on the target at the entry's own `t`, non-numeric
knobs snap, a scenario with no `ramp` behaves EXACTLY as it did before the
flag existed — that last one is a compatibility guarantee for every shipped
and every hand-written scenario — and the `pulls` curve is visibly smoother
than the stepped one it replaces.

Nothing here is evidence about a car; it is arithmetic about arithmetic.
"""

import json

import pytest

from conftest import ROOT  # noqa: F401  (sets sys.path)
from simulator import make_sim


def scenario(tmp_path, timeline, knobs=None, name="ramptest"):
    p = tmp_path / (name + ".json")
    p.write_text(json.dumps({"name": name, "vehicle": "leaf_ze0", "seed": 1,
                             "knobs": dict(knobs or {"soc": 80, "start_state": "ready",
                                                     "gear": "P", "speed_mph": 0}),
                             "timeline": timeline}))
    return str(p)


def walk(sim, until, dt):
    """[(t, knobs)] sampled after every step of `dt` up to `until`."""
    out, t = [], 0.0
    while t < until - 1e-9:
        sim.step(dt)
        t += dt
        out.append((round(sim.model.t, 6), dict(sim.get_knobs())))
    return out


RAMP = [{"t": 0, "set": {"gear": "D", "speed_mph": 0}},
        {"t": 10, "set": {"speed_mph": 40}, "ramp": True}]


@pytest.mark.parametrize("dt", [0.1, 0.25, 0.5, 1.0, 2.5, 5.0])
def test_a_ramp_is_linear_in_simulated_time_whatever_the_step(tmp_path, dt):
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, RAMP))
    for t, k in walk(sim, 10.0, dt):
        assert k["speed_mph"] == pytest.approx(4.0 * t, abs=1e-6), f"at t={t}, dt={dt}"


@pytest.mark.parametrize("dt", [0.1, 0.25, 0.5, 1.0, 2.5, 5.0])
def test_a_ramp_lands_exactly_on_its_target_at_its_own_time(tmp_path, dt):
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, RAMP))
    t = 0.0
    while t < 10.0 - 1e-9:
        sim.step(dt)
        t += dt
    assert sim.model.t == pytest.approx(10.0, abs=1e-9)
    assert sim.get_knobs()["speed_mph"] == 40.0        # exactly, not 39.996
    sim.step(dt)
    assert sim.get_knobs()["speed_mph"] == 40.0        # and it stays there


def test_a_ramp_passes_through_the_midpoint(tmp_path):
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, RAMP))
    sim.step(5.0)
    assert sim.get_knobs()["speed_mph"] == pytest.approx(20.0)


def test_a_step_that_jumps_the_whole_ramp_still_lands_on_the_target(tmp_path):
    """A 30 s dt at 3600x is one call; the ramp must not be left half-done."""
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, RAMP))
    sim.step(60.0)
    assert sim.get_knobs()["speed_mph"] == 40.0


def test_a_ramp_starts_from_where_the_previous_entry_left_the_knob(tmp_path):
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, [
        {"t": 0, "set": {"gear": "D", "speed_mph": 0}},
        {"t": 4, "set": {"speed_mph": 20}},
        {"t": 8, "set": {"speed_mph": 40}, "ramp": True}]))
    sim.step(4.0)
    assert sim.get_knobs()["speed_mph"] == 20.0        # the step, still a step
    sim.step(2.0)                                      # halfway through the ramp
    assert sim.get_knobs()["speed_mph"] == pytest.approx(30.0)
    sim.step(2.0)
    assert sim.get_knobs()["speed_mph"] == 40.0


def test_the_clock_scale_does_not_move_a_ramp(tmp_path):
    """--speed compresses the wall clock, not the timeline: the same simulated
    instant has the same value at 1x and at 60x."""
    path = scenario(tmp_path, RAMP)
    slow = make_sim()
    slow.load_scenario(path)
    fast = make_sim(speed=60.0)
    fast.load_scenario(path)
    for _ in range(20):
        slow.step(0.5)                                 # 0.5 s of simulated time
        fast.step(0.5 / 60.0)                          # the same, 60x compressed
        assert fast.model.t == pytest.approx(slow.model.t, abs=1e-6)
        assert fast.get_knobs()["speed_mph"] == pytest.approx(
            slow.get_knobs()["speed_mph"], abs=1e-6)


def test_non_numeric_knobs_snap_at_the_entrys_own_time(tmp_path):
    """Half of `D` is not a gear and 0.5 of `handbrake` is not a brake."""
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, [
        {"t": 0, "set": {"gear": "P", "handbrake": True, "speed_mph": 0}},
        {"t": 10, "set": {"gear": "D", "handbrake": False, "speed_mph": 40},
         "ramp": True}]))
    sim.step(5.0)
    k = sim.get_knobs()
    assert k["speed_mph"] == pytest.approx(20.0), "the number is halfway"
    assert k["gear"] == "P" and k["handbrake"] is True, "the rest has not moved"
    sim.step(5.0)
    k = sim.get_knobs()
    assert k["gear"] == "D" and k["handbrake"] is False
    assert k["speed_mph"] == 40.0


def test_a_ramp_on_the_first_entry_is_a_step(tmp_path):
    """There is nothing to ramp from, so it behaves exactly as it always did —
    including the rule that an entry at t <= 0 fires on load."""
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, [
        {"t": 0, "set": {"gear": "D", "speed_mph": 12}, "ramp": True}]))
    assert sim.get_knobs()["speed_mph"] == 12.0


def test_an_int_knob_ramps_through_whole_numbers(tmp_path):
    """An int knob is still a number, so it interpolates — through the knob's
    own coercion, which rounds."""
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, [
        {"t": 0, "set": {"sunload": 0}},
        {"t": 10, "set": {"sunload": 200}, "ramp": True}]))
    sim.step(5.0)
    got = sim.get_knobs()["sunload"]
    assert got == 100 and isinstance(got, int)
    sim.step(2.5)
    assert sim.get_knobs()["sunload"] == 150


def test_a_target_outside_the_range_warns_once_not_every_substep(tmp_path):
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, [
        {"t": 0, "set": {"soc": 80}},
        {"t": 10, "set": {"soc": 140}, "ramp": True}]))
    for _ in range(40):
        sim.step(0.25)
    assert len([w for w in sim.warnings if "soc" in w]) == 1
    # approx, not exact: SOC is coulomb-counted, so the integrator moves it
    # again after the ramp's last write — the same as any stepped entry
    assert sim.get_knobs()["soc"] == pytest.approx(100.0, abs=0.01)


def test_clearing_a_scenario_stops_a_ramp_where_it_stands(tmp_path):
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, RAMP))
    sim.step(5.0)
    sim.clear_scenario()
    sim.step(5.0)
    assert sim.scenario is None
    assert sim.get_knobs()["speed_mph"] == pytest.approx(20.0), \
        "a cleared scenario leaves every knob exactly where it was"


def test_reloading_a_scenario_restarts_its_ramps(tmp_path):
    path = scenario(tmp_path, RAMP)
    sim = make_sim()
    sim.load_scenario(path)
    sim.step(5.0)
    sim.load_scenario(path)                            # model.t goes back to 0
    assert sim.model.t == 0.0
    sim.step(2.5)
    assert sim.get_knobs()["speed_mph"] == pytest.approx(10.0), \
        "the ramp starts again from where the t=0 entry put the knob"


# ── the compatibility guarantee ──────────────────────────────────────────

def test_a_scenario_with_no_ramp_is_exactly_a_step_function(tmp_path):
    """Every scenario written before the flag existed must behave identically:
    a knob holds its value until an entry's own `t`, then snaps."""
    sim = make_sim()
    sim.load_scenario(scenario(tmp_path, [
        {"t": 0, "set": {"gear": "D", "speed_mph": 0}},
        {"t": 2, "set": {"speed_mph": 20}},
        {"t": 4, "set": {"speed_mph": 45}}]))
    want = {0.5: 0.0, 1.0: 0.0, 1.5: 0.0, 2.0: 20.0, 2.5: 20.0, 3.0: 20.0,
            3.5: 20.0, 4.0: 45.0, 4.5: 45.0, 5.0: 45.0}
    for t, k in walk(sim, 5.0, 0.5):
        assert k["speed_mph"] == want[t], f"at t={t}"


def test_the_shipped_stepped_scenarios_are_untouched():
    """`drive` and `commute` stay stepped on purpose (a stepped scenario that
    tests something specific should stay stepped); `pulls` is the one that
    ramps."""
    import os
    from simulator import SCENARIO_DIR
    ramped = set()
    for name in os.listdir(SCENARIO_DIR):
        data = json.load(open(os.path.join(SCENARIO_DIR, name)))
        if any(e.get("ramp") for e in data.get("timeline") or []):
            ramped.add(data["name"])
    assert ramped == {"pulls"}, ramped


def test_ramp_must_be_a_boolean(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"name": "bad", "vehicle": "leaf_ze0",
                             "timeline": [{"t": 1, "set": {"speed_mph": 3},
                                           "ramp": "yes"}]}))
    with pytest.raises(ValueError) as e:
        make_sim().load_scenario(str(p))
    assert "ramp" in str(e.value)


# ── what it was built for ────────────────────────────────────────────────

def _pulls_current(dt=0.1, seconds=133.0):
    sim = make_sim(vehicle="leaf_ze0", scenario="pulls", seed=7)
    out, t = [], 0.0
    while t < seconds:
        sim.step(dt)
        t += dt
        out.append(sim.state()["current_a"])
    return out


def test_the_pulls_curve_climbs_smoothly_now(tmp_path):
    """The measure of the complaint: the biggest current jump between two
    samples a tenth of a second apart. Stepped, a 0.5 s speed entry moves the
    current by tens of amps in one sample; ramped, it is spread over the whole
    half second."""
    ramped = _pulls_current()
    jumps = [abs(b - a) for a, b in zip(ramped, ramped[1:])]
    assert max(jumps) < 20.0, f"biggest jump {max(jumps):.1f} A"
