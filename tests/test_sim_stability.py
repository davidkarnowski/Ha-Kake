# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The model must not depend on how the caller chops up time.

Found by running the shipped `charge` scenario at a compressed clock: a 3.3 kW
Level 2 charge took the pack to 80 °C. That was not a clamp and not a bad
constant — it was forward Euler exploding, because a single `step(dt)` was
being handed hours at a time and the pack's relaxation term
`(T - ambient) * dt / 1800` flips sign and grows once `dt > 3600`.

The fix is sub-stepping: `LeafModel.step()` never integrates more than
MAX_SUBSTEP_S at once. These tests pin both halves of what that has to buy —
**equivalence** (one big call lands where many small ones land) and
**boundedness** (no reachable knob combination produces a temperature no pack
could be at).

Tolerances below are *documented, not incidental*. With a 5 s cap, one call
covering N seconds takes N/5 Euler steps where the reference takes N; the two
truncation errors differ, and the difference is largest on the fastest state
(the evaporator, tau = 60 s). Everything else agrees far more tightly than the
number asserted.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulator import make_sim                                   # noqa: E402
from simulator.model import MAX_SUBSTEP_S, substeps              # noqa: E402

# what "agree" means, per quantity, over any duration tested here
TOLERANCE = {
    "soc": 0.1,             # %
    "pack_temp_c": 0.05,    # °C
    "cabin_temp_c": 0.5,    # °C  (integer in state(); tau as low as 90 s)
    "evap_c": 0.5,          # °C  (integer in state(); tau 60 s, the fastest)
    "odometer_mi": 0.01,    # mi
    "t": 1e-6,              # s
}

# a pack being charged, driven, heated and cooled all at once: every integrated
# quantity is moving, which is the only way a step-size test means anything
BUSY = {"noise": 0.0, "charging": True, "charger": "l2", "soc": 40.0,
        "pack_temp_c": 15.0, "ambient_c": 32.0, "cabin_temp_c": 15.0,
        "evap_c": 15.0, "hvac_on": True, "hvac_ac_on": True, "hvac_fan_speed": 7,
        "speed_mph": 45.0}

DRIVING = {"noise": 0.0, "current_a": -180.0, "internal_resistance_ohm": 0.12,
           "soc": 90.0, "pack_temp_c": 20.0, "ambient_c": 5.0, "speed_mph": 70.0,
           "hvac_on": True, "hvac_fan_speed": 4, "hvac_setpoint_f": 80.0}


def ends(knobs, seconds, chunk):
    s = make_sim(vehicle="leaf_ze0", knobs=dict(knobs), seed=1)
    done = 0.0
    while done < seconds - 1e-9:
        h = min(chunk, seconds - done)
        s.step(h)
        done += h
    return s.state()


def assert_agree(a, b, why):
    for key, tol in TOLERANCE.items():
        assert a[key] == pytest.approx(b[key], abs=tol), \
            f"{key} disagrees {why}: {a[key]} vs {b[key]} (tolerance {tol})"


# ── equivalence ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("knobs,name", [(BUSY, "charging"), (DRIVING, "driving")])
@pytest.mark.parametrize("seconds", [600.0, 3600.0, 8 * 3600.0])
def test_one_big_step_lands_where_many_small_ones_land(knobs, name, seconds):
    """The headline. `step(3600)` must be `step(1)` three thousand six hundred
    times, or a compressed clock is quietly a different car."""
    assert_agree(ends(knobs, seconds, seconds), ends(knobs, seconds, 1.0),
                 f"between one {seconds} s call and {seconds:.0f} one-second calls ({name})")


@pytest.mark.parametrize("chunk", [0.25, 1.0, 7.0, 60.0, 900.0, 3600.0])
def test_no_chunk_size_changes_the_destination(chunk):
    """Not just 1 s against the whole interval: *any* two chunkings agree.
    A caller's loop period is not physics."""
    assert_agree(ends(BUSY, 3600.0, chunk), ends(BUSY, 3600.0, 1.0), f"at chunk {chunk}")


def test_the_substep_cap_is_what_bounds_the_integration_interval():
    assert substeps(0) == []
    assert substeps(-5) == []
    for dt in (0.3, 5.0, 5.1, 3600.0, 86400.0):
        chunks = substeps(dt)
        assert chunks, dt
        assert sum(chunks) == pytest.approx(dt, rel=1e-12)
        assert max(chunks) <= MAX_SUBSTEP_S + 1e-9, f"{dt} s was integrated in one bite"


# ── boundedness ──────────────────────────────────────────────────────────

SANE_MIN_C, SANE_MAX_C = -40.0, 70.0


def test_no_reachable_knob_combination_drives_the_pack_out_of_a_sane_band():
    """The original symptom, generalised. Walk the extremes of every knob that
    feeds the thermal model — including combinations no car can be in, like
    400 A through a 2 ohm pack — at the fastest clock the rig allows, and the
    pack temperature must stay somewhere a pack could actually be.

    Sub-stepping is what makes this true for anything realistic; the domain
    guard in model._clamp() catches the physically impossible remainder.
    """
    worst_hi, worst_lo = -999.0, 999.0
    for current in (-400.0, -80.0, 0.0, 400.0):
        for r in (0.0, 0.08, 2.0):
            for ambient in (-40.0, 22.0, 60.0):
                for charging in (True, False):
                    s = make_sim(vehicle="leaf_ze0", seed=1, knobs={
                        "noise": 0.0, "current_a": current, "internal_resistance_ohm": r,
                        "ambient_c": ambient, "charging": charging, "charger": "dcfc",
                        "charge_heat_frac": 0.5, "charge_temp_limit_c": 80.0,
                        "soc": 50.0, "pack_temp_c": 22.0, "clock_scale": 3600.0})
                    for _ in range(120):          # 120 real s at 3600x = 5 days
                        s.step(1.0)
                        t = s.state()["pack_temp_c"]
                        worst_hi, worst_lo = max(worst_hi, t), min(worst_lo, t)
                        assert SANE_MIN_C <= t <= SANE_MAX_C, (
                            f"pack reached {t} °C at current={current} r={r} "
                            f"ambient={ambient} charging={charging}")
    assert worst_hi > 22.0, "the sweep never actually heated the pack — it proves nothing"
    assert worst_lo < 22.0, "the sweep never actually cooled the pack — it proves nothing"


def test_the_level_2_charge_that_started_this_stays_at_a_charging_temperature():
    """The exact reported case: the shipped `charge` scenario with the clock
    wound right up. A 3.3 kW charge into an 18 °C pack warms it by a couple of
    degrees an hour. It does not reach 80 °C, at any time scale."""
    for scale in (1.0, 120.0, 3600.0):
        s = make_sim(vehicle="leaf_ze0", scenario="charge", speed=scale)
        for _ in range(200):
            s.step(1.0)
        st = s.state()
        assert st["pack_temp_c"] < 40.0, f"{scale}x drove the pack to {st['pack_temp_c']} °C"
        assert st["pack_temp_c"] > 10.0
        assert 0.0 <= st["soc"] <= 100.0


def test_every_integrated_quantity_stays_inside_its_own_declared_range():
    """`state()` must never report a value outside the range its own schema
    advertises — including the ones an absurd knob set would otherwise push
    past. A state variable that leaves its declared domain is a lie."""
    schema = make_sim(vehicle="leaf_ze0").knob_schema()
    s = make_sim(vehicle="leaf_ze0", seed=1, knobs={
        "noise": 0.0, "current_a": -400.0, "internal_resistance_ohm": 2.0,
        "ambient_c": 60.0, "speed_mph": 120.0, "hvac_on": True, "hvac_ac_on": True,
        "hvac_fan_speed": 7, "soc": 100.0, "odometer_mi": 999_000.0,
        "clock_scale": 3600.0})
    for _ in range(300):
        s.step(1.0)
        k = s.get_knobs()
        for name in ("soc", "pack_temp_c", "cabin_temp_c", "evap_c", "odometer_mi"):
            spec = schema[name]
            assert spec["min"] <= k[name] <= spec["max"], \
                f"{name} = {k[name]}, outside its declared {spec['min']}..{spec['max']}"


def test_the_lancer_integrators_are_sub_stepped_too():
    """The second profile has its own step(); it must not be the one place a
    big dt still goes straight into Euler."""
    def run(chunk):
        s = make_sim(vehicle="lancer_2009", seed=1,
                     knobs={"noise": 0.0, "engine_running": True, "coolant_temp_c": 20.0,
                            "load_pct": 80.0, "fuel_pct": 90.0})
        done = 0.0
        while done < 3600.0 - 1e-9:
            h = min(chunk, 3600.0 - done)
            s.step(h)
            done += h
        return s.state()
    big, small = run(3600.0), run(1.0)
    assert big["coolant_temp_c"] == pytest.approx(small["coolant_temp_c"], abs=0.05)
    assert big["fuel_pct"] == pytest.approx(small["fuel_pct"], abs=0.05)
    assert big["runtime_s"] == pytest.approx(small["runtime_s"], abs=2)
