# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The charge curve.

These tests are about **shape**, because shape is what the simulator is for:
a chart drawn from a simulated charge has to be indistinguishable from one
drawn from a real charge, or someone will design the UI around an artifact
that no car produces. They do not claim the numbers are the car's — see the
CHARGERS comment in simulator/model.py for what is modelled and what is
merely asserted.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulator import make_sim                                  # noqa: E402
from simulator.model import CHARGERS                            # noqa: E402


def sim(**knobs):
    k = {"pack_temp_c": 20.0, "ambient_c": 20.0, "charging": True}
    k.update(knobs)
    return make_sim(vehicle="leaf_ze0", knobs=k, seed=1)


def curve(s, lo=5, hi=100, step=1.0):
    """(soc, kW) across the range, without advancing the clock."""
    out = []
    soc = float(lo)
    while soc <= hi + 1e-9:
        s.set(soc=soc)
        out.append((soc, s.model.charge_power_kw()))
        soc += step
    return out


# ── the flat phase and the taper ─────────────────────────────────────────

def test_power_is_flat_below_the_knee_and_falls_above_it():
    s = sim(capacity_ah=60.0, charger="l2")
    knee = s.model.taper_start_soc()
    below = [p for soc, p in curve(s) if soc < knee - 2]
    assert below and max(below) - min(below) < 1e-9, "the CC phase must be flat"
    assert below[0] == pytest.approx(3.3)
    above = [p for soc, p in curve(s) if soc > knee + 1]
    assert all(b >= a - 1e-9 for a, b in zip(above[1:], above)), "taper must be monotone"


def test_the_curve_is_smooth_not_stepped():
    """The whole reason the scripted two-step taper had to go: a developer
    would design a chart around a discontinuity that never happens.

    Continuity, tested the only way that distinguishes a curve from a
    staircase: refine the SOC grid tenfold and the largest step in power must
    shrink with it. A scripted step would not budge.
    """
    s = sim(capacity_ah=60.0, charger="l2")
    peak = 3.3

    def biggest_jump(step):
        pts = curve(s, step=step)
        return max(abs(b - a) for (_, a), (_, b) in zip(pts, pts[1:]))

    coarse = biggest_jump(1.0)
    fine = biggest_jump(0.1)
    assert coarse < 0.12 * peak
    assert fine < coarse / 5.0, "power must be continuous in SOC, not stepped"


def test_power_reaches_zero_at_full_and_current_stays_positive():
    s = sim(capacity_ah=60.0, charger="l2")
    s.set(soc=100.0)
    assert s.model.charge_power_kw() == 0.0
    s.set(soc=99.0)
    assert 0.0 < s.model.charge_power_kw() < 0.5
    for soc in (20, 60, 90, 99):
        s.set(soc=float(soc))
        assert s.model.current() > 0, "charging current is positive (house sign rule)"


def test_soc_climbs_and_the_rate_slows_as_the_pack_fills():
    s = sim(capacity_ah=60.0, charger="l2_66", soc=50.0)
    marks = []
    for _ in range(60):
        before = s.state()["soc"]
        s.step(600.0)
        marks.append(s.state()["soc"] - before)
    assert all(m >= -1e-9 for m in marks), "SOC never falls while charging"
    assert marks[0] > marks[-1] * 3, "the last hours must add far less than the first"
    assert s.state()["soc"] > 99.0


# ── charger types ────────────────────────────────────────────────────────

def test_each_charger_type_sets_its_own_power_and_knee():
    for name, (kw, knee, _exp, _tk, _lim) in CHARGERS.items():
        s = sim(capacity_ah=66.0, charger=name)
        assert s.get_knobs()["charge_kw"] == pytest.approx(kw)
        assert s.get_knobs()["charge_taper_start_soc"] == pytest.approx(knee)
        s.set(soc=30.0)
        assert s.model.charge_power_kw() == pytest.approx(kw)


def test_dc_fast_tapers_much_harder_above_80_percent():
    healthy = {"capacity_ah": 60.0, "pack_temp_c": 20.0}
    fast = sim(charger="dcfc", **healthy)
    slow = sim(charger="l2", **healthy)
    for s in (fast, slow):
        s.set(soc=85.0)
    frac_fast = fast.model.charge_power_kw() / fast.get_knobs()["charge_kw"]
    frac_slow = slow.model.charge_power_kw() / slow.get_knobs()["charge_kw"]
    assert frac_fast < frac_slow / 2, "a ZE0 on CHAdeMO is nearly done at 85 %"
    fast.set(soc=40.0)
    assert fast.model.charge_power_kw() > 40.0, "and full power well below the knee"


# ── the two things that move the curve sideways ──────────────────────────

def test_a_tired_pack_starts_tapering_earlier():
    """Calibrated against the owner's own captures: his 35 %-SOH pack rolls
    off from ~69 %, not from the 85 % a healthy one would."""
    healthy = sim(capacity_ah=66.0, charger="l2").model.taper_start_soc()
    tired = sim(capacity_ah=23.16, charger="l2").model.taper_start_soc()
    assert healthy == pytest.approx(85.0, abs=0.5)
    assert tired == pytest.approx(69.0, abs=1.5)


def test_the_real_sessions_shape_is_reproduced():
    """Two real Level-1/2 sessions in the owner's database, sampled: 3.2 kW
    flat, then 2.2 kW at 75 %, ~1.2 kW at 80 %, ~0.9 kW at 85 %, ~0.5 kW at
    90 %. Sample-to-sample scatter in those sessions is itself ±30 %, so the
    tolerance is wide on purpose."""
    s = sim(capacity_ah=23.16, charger="l2", charge_kw=3.2)
    for soc, want in ((70, 3.1), (75, 2.2), (80, 1.4), (85, 0.9), (90, 0.4)):
        s.set(soc=float(soc))
        got = s.model.charge_power_kw()
        assert got == pytest.approx(want, abs=0.45), f"{soc} %: {got:.2f} kW vs {want} kW"


def test_a_hot_pack_is_derated_and_a_freezing_one_more_so():
    hot = sim(capacity_ah=60.0, charger="dcfc", soc=30.0, pack_temp_c=60.0)
    cold = sim(capacity_ah=60.0, charger="dcfc", soc=30.0, pack_temp_c=-8.0)
    ok = sim(capacity_ah=60.0, charger="dcfc", soc=30.0, pack_temp_c=25.0)
    assert ok.model.charge_power_kw() == pytest.approx(44.0)
    assert hot.model.charge_power_kw() < 25.0
    assert cold.model.charge_power_kw() < 20.0


def test_charging_heats_the_pack_and_dc_fast_heats_it_faster():
    """charge_heat_frac is the one charge knob with no instantaneous effect,
    so it is skipped in the every-knob test and checked here instead."""
    def rise(charger, hours):
        s = sim(capacity_ah=60.0, charger=charger, soc=25.0,
                pack_temp_c=20.0, ambient_c=20.0)
        for _ in range(int(hours * 60)):
            s.step(60.0)
        return s.state()["pack_temp_c"] - 20.0

    slow = rise("l2", 2.0)
    fast = rise("dcfc", 0.4)
    assert 0.5 < slow < 5.0, "Level 2 warms the pack a couple of degrees over hours"
    assert fast > slow, "DC fast dumps far more heat in far less time"

    cold_run = sim(capacity_ah=60.0, charger="dcfc", soc=25.0, pack_temp_c=20.0,
                   ambient_c=20.0, charge_heat_frac=0.0)
    for _ in range(24):
        cold_run.step(60.0)
    assert cold_run.state()["pack_temp_c"] < 20.0 + fast


def test_not_charging_means_no_charge_power():
    s = sim(charging=False, charger="dcfc", soc=30.0)
    assert s.model.charge_power_kw() == 0.0
    assert s.model.current() < 0, "parked, the pack only discharges"
