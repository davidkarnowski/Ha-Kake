# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The load model: pack current is a sum of switched-on consumers.

Before this table existed, `current()` was a three-way selector and the A/C,
the 5 kW heater and the headlights changed the pack current by zero watts.
These tests pin the other way round: every row of LOADS_W has an observable
effect, the READY idle draw lands where the kelvin-shunt measurement puts it,
HVAC and motor draw nothing unless the car is READY, and the bookkeeping
identity Σloads = −P + I²R holds.

Numbers labelled ASSERTED in model.py are shape, not measurement; the tests
check monotonicity and gating for those, and only pin absolute values where
the load table says MEASURED.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulator import make_sim                                   # noqa: E402
from simulator import model as M                                 # noqa: E402
from simulator.model import LOADS_W, LOAD_NAMES, cruise_kw        # noqa: E402


def leaf(**knobs):
    k = {"noise": 0.0, "start_state": "ready"}
    k.update(knobs)
    return make_sim(vehicle="leaf_ze0", knobs=k, seed=1)


def draw_w(sim):
    """Watts leaving the pack (positive while discharging)."""
    return -sim.state()["power_kw"] * 1000.0


# ── the base draw by ignition state ──────────────────────────────────────

def test_ready_idle_lands_on_the_measured_band():
    """140-160 W with a kelvin shunt (Lab Test thread); the model says 150."""
    assert 120.0 <= draw_w(leaf()) <= 200.0


def test_off_is_a_few_watts():
    assert draw_w(leaf(start_state="off")) < 20.0


def test_acc_and_on_sit_between_off_and_ready():
    off, acc, on, ready = (draw_w(leaf(start_state=s)) for s in ("off", "acc", "on", "ready"))
    assert off < acc < on < ready


def test_every_load_table_row_carries_a_provenance_comment():
    """Reading the source is the test: each row's line must say MEASURED,
    ASSERTED or OWNER REPORT, on the row or on the comment block above it."""
    src = open(M.__file__, encoding="utf-8").read()
    table = src[src.index("LOADS_W = {"):src.index("LOAD_NAMES = (")]
    lines = table.splitlines()
    for n, line in enumerate(lines):
        if not line.strip().startswith('"'):
            continue
        window = "\n".join(lines[max(0, n - 6):n + 1])
        assert any(tag in window for tag in ("MEASURED", "ASSERTED", "OWNER REPORT")), \
            f"no provenance near LOADS_W row: {line.strip()}"


# ── each row is observable ───────────────────────────────────────────────

ROWS = [
    # loads_w name, knobs that switch it on, expected watts (None = just > 0)
    ("low_beam", {"headlights": True}, LOADS_W["low_beam"]),
    ("high_beam", {"high_beam": True}, LOADS_W["high_beam"]),
    ("position", {"parking_lights": True}, LOADS_W["position"]),
    ("fog", {"fog_lights": True}, LOADS_W["fog"]),
    ("turn", {"turn_signal": "left"}, LOADS_W["turn"]),
    ("brake_lamps", {"brake_pct": 30.0}, LOADS_W["brake_lamps"]),
    ("reverse_lamps", {"gear": "R"}, LOADS_W["reverse_lamps"]),
    ("blower", {"hvac_on": True, "hvac_fan_speed": 6}, LOADS_W["blower_max"]),
    ("ac", {"hvac_on": True, "hvac_ac_on": True, "hvac_fan_speed": 3}, None),
    ("ptc", {"hvac_on": True, "heater_level": 40}, LOADS_W["ptc_max"]),
    ("motor", {"gear": "D", "speed_mph": 40.0, "accel_pedal_pct": 20.0}, None),
]


@pytest.mark.parametrize("name,knobs,watts", ROWS, ids=[r[0] for r in ROWS])
def test_each_row_is_observable_in_loads_w_and_in_the_current(name, knobs, watts):
    base = leaf()
    b = base.state()
    i0, v = b["current_a"], base.model.ocv_pack()
    assert b["loads_w"][name] == 0.0
    st = leaf(**knobs).state()
    assert st["loads_w"][name] > 0.0
    if watts is not None:
        assert st["loads_w"][name] == pytest.approx(watts, rel=0.01)
    # the current got more negative by about P / V (the A/C case also has to
    # run the blower, so compare against the whole load change)
    added = st["load_total_w"] - b["load_total_w"]
    assert added >= st["loads_w"][name] - 0.1
    assert i0 - st["current_a"] == pytest.approx(added / v, rel=0.02, abs=0.02)


def test_loads_w_lists_every_row_in_dashboard_order():
    assert tuple(leaf().state()["loads_w"]) == LOAD_NAMES


def test_hazards_draw_both_sides():
    assert leaf(turn_signal="hazards").state()["loads_w"]["turn"] == 2 * LOADS_W["turn"]


def test_high_beam_subsumes_the_low_beam_row():
    """The 360 W measurement was taken with the low beams lit under the high
    beams, so both on must not double-count: 150 + 200, not 150 + 70 + 200."""
    both = leaf(headlights=True, high_beam=True).state()["loads_w"]
    assert both["high_beam"] == LOADS_W["high_beam"] and both["low_beam"] == 0.0
    assert 330.0 <= draw_w(leaf(headlights=True, high_beam=True)) <= 380.0


# ── gating on READY ──────────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["off", "acc", "on"])
def test_hvac_draws_nothing_unless_ready(state):
    st = leaf(start_state=state, hvac_on=True, hvac_ac_on=True, hvac_fan_speed=7,
              heater_level=40, cabin_temp_c=40.0).state()
    L = st["loads_w"]
    assert L["blower"] == L["ac"] == L["ptc"] == 0.0
    assert st["hvac_kw"] == 0.0


@pytest.mark.parametrize("state", ["off", "acc", "on"])
def test_motor_draws_nothing_unless_ready(state):
    st = leaf(start_state=state, gear="D", speed_mph=50.0, accel_pedal_pct=60.0).state()
    assert st["loads_w"]["motor"] == 0.0 and st["motor_kw"] == 0.0
    assert st["regen_kw"] == 0.0


def test_lamps_draw_in_every_ignition_state():
    for s in ("off", "acc", "on", "ready"):
        assert leaf(start_state=s, headlights=True).state()["loads_w"]["low_beam"] > 0


# ── HVAC shapes ──────────────────────────────────────────────────────────

def test_ac_scales_with_cabin_minus_setpoint():
    ws = [leaf(hvac_on=True, hvac_ac_on=True, hvac_fan_speed=3, hvac_setpoint_f=72.0,
               cabin_temp_c=c, ambient_c=30.0).state()["loads_w"]["ac"]
          for c in (22.0, 26.0, 30.0)]
    assert ws[0] < ws[1] < ws[2]
    assert LOADS_W["ac_min"] <= min(ws) and max(ws) <= LOADS_W["ac_max"]


def test_ac_reaches_the_owners_two_figures():
    """~1.5 kW stopped with a mild cabin, ~3 kW hot cabin in hot weather."""
    mild = leaf(hvac_on=True, hvac_ac_on=True, hvac_fan_speed=3, cabin_temp_c=22.0,
                ambient_c=22.0).state()["loads_w"]["ac"]
    hot = leaf(hvac_on=True, hvac_ac_on=True, hvac_fan_speed=3, cabin_temp_c=40.0,
               ambient_c=40.0, hvac_setpoint_f=70.0).state()["loads_w"]["ac"]
    assert mild == pytest.approx(1500.0, rel=0.05)
    assert hot == pytest.approx(3000.0, rel=0.05)


def test_ac_needs_the_compressor_and_the_blower():
    assert leaf(hvac_on=True, hvac_ac_on=False, hvac_fan_speed=3).state()["loads_w"]["ac"] == 0.0
    assert leaf(hvac_on=False, hvac_ac_on=True, hvac_fan_speed=3).state()["loads_w"]["ac"] == 0.0


def test_ptc_is_linear_in_heater_level():
    ws = [leaf(hvac_on=True, heater_level=h).state()["loads_w"]["ptc"] for h in (0, 10, 20, 40)]
    assert ws[0] == 0.0
    assert ws[1] == pytest.approx(LOADS_W["ptc_max"] / 4, rel=0.01)
    assert ws[2] == pytest.approx(LOADS_W["ptc_max"] / 2, rel=0.01)
    assert ws[3] == pytest.approx(LOADS_W["ptc_max"], rel=0.01)
    assert 4500.0 <= ws[3] <= 5500.0, "the owner reports 4.5-5.5 kW flat out"


def test_blower_is_monotonic_in_fan_speed():
    ws = [leaf(hvac_on=True, hvac_fan_speed=f).state()["loads_w"]["blower"] for f in range(8)]
    assert ws[0] == 0.0
    assert all(b >= a for a, b in zip(ws, ws[1:]))
    assert ws[6] == ws[7], "6 and 7 both read 11 V on this car"
    assert ws[7] == pytest.approx(LOADS_W["blower_max"])


# ── the motor and regen ──────────────────────────────────────────────────

def test_cruise_kw_is_the_documented_shape():
    assert 5.0 <= cruise_kw(40.0) <= 7.0, "≈5.7 kW at 40 mph is the asserted shape"
    assert cruise_kw(0.0) == 0.0
    prev = -1.0
    for mph in range(0, 121, 5):
        v = cruise_kw(mph)
        assert v > prev
        prev = v


def test_eco_coast_regen_matches_the_2026_09_03_drive():
    """The one motor/regen number the real drive corroborated.

    Rows in Eco with the brake released and power flowing back gave a median
    +3.60 kW at 10-20 mph and peaked at +7.68 kW (docs/SIMULATOR.md). Lift-off
    regen is a commanded torque, so road grade — unmeasured on that drive —
    cannot fake it. Pin the model into that observed band; road load, the pedal
    term, D coast and the brake term stay ASSERTED and are not pinned here.
    """
    st = leaf(gear="Eco", speed_mph=15.0, accel_pedal_pct=0.0, brake_pct=0.0,
              soc=70.0).state()
    assert 2.5 <= st["regen_kw"] <= 7.7, "Eco coast regen outside the observed band"


def test_motor_is_monotonic_in_speed_and_pedal_and_zero_in_park():
    by_speed = [leaf(gear="D", accel_pedal_pct=20.0, speed_mph=s).state()["motor_kw"]
                for s in (10.0, 30.0, 50.0, 70.0)]
    assert all(b > a for a, b in zip(by_speed, by_speed[1:]))
    by_pedal = [leaf(gear="D", speed_mph=30.0, accel_pedal_pct=p).state()["motor_kw"]
                for p in (5.0, 20.0, 50.0, 100.0)]
    assert all(b > a for a, b in zip(by_pedal, by_pedal[1:]))
    for gear in ("P", "N"):
        assert leaf(gear=gear, speed_mph=30.0, accel_pedal_pct=50.0).state()["motor_kw"] == 0.0
    assert leaf(gear="R", speed_mph=5.0, accel_pedal_pct=20.0).state()["motor_kw"] > 0.0


def test_lifting_off_is_coasting_not_a_draw():
    st = leaf(gear="D", speed_mph=40.0, accel_pedal_pct=0.0).state()
    assert st["motor_kw"] == 0.0
    assert st["regen_kw"] > 0.0, "coast regen in D"


def test_regen_only_when_moving_in_d_or_eco_and_below_95_percent():
    assert leaf(gear="D", speed_mph=40.0).state()["regen_kw"] > 0
    assert leaf(gear="Eco", speed_mph=40.0).state()["regen_kw"] > leaf(gear="D", speed_mph=40.0).state()["regen_kw"]
    assert leaf(gear="D", speed_mph=0.0, brake_pct=50.0).state()["regen_kw"] == 0.0
    for gear in ("P", "N", "R"):
        assert leaf(gear=gear, speed_mph=40.0, brake_pct=50.0).state()["regen_kw"] == 0.0
    assert leaf(gear="D", speed_mph=40.0, brake_pct=50.0, soc=96.0).state()["regen_kw"] == 0.0
    assert leaf(gear="D", speed_mph=40.0, brake_pct=50.0, soc=80.0).state()["regen_kw"] > 0.0


def test_brake_regen_makes_the_current_positive():
    st = leaf(gear="D", speed_mph=40.0, brake_pct=60.0).state()
    assert st["current_a"] > 0 and st["power_kw"] > 0
    assert st["loads_w"]["regen"] == pytest.approx(st["regen_kw"] * 1000.0, abs=1.0)


# ── the two overrides ────────────────────────────────────────────────────

def test_load_kw_override_is_exact_and_bypasses_the_model():
    sim = leaf(load_kw=10.0, headlights=True, hvac_on=True, hvac_ac_on=True, hvac_fan_speed=7,
               gear="D", speed_mph=50.0, accel_pedal_pct=50.0, current_a=-77.0)
    v = sim.model.ocv_pack()
    assert sim.state()["current_a"] == pytest.approx(-10000.0 / v, abs=0.001)


def test_current_a_is_an_exact_extra_on_top_of_the_model():
    base = leaf(headlights=True).state()["current_a"]
    assert leaf(headlights=True, current_a=-25.0).state()["current_a"] == pytest.approx(base - 25.0, abs=0.001)
    assert leaf(headlights=True, current_a=+8.0).state()["current_a"] == pytest.approx(base + 8.0, abs=0.001)


def test_current_a_defaults_to_zero():
    from simulator import KNOBS
    assert KNOBS["leaf_ze0"]["current_a"].default == 0.0


# ── bookkeeping ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("knobs", [
    {},
    {"headlights": True, "fog_lights": True, "turn_signal": "hazards"},
    {"hvac_on": True, "hvac_ac_on": True, "hvac_fan_speed": 5, "cabin_temp_c": 30.0},
    {"hvac_on": True, "heater_level": 30},
    {"gear": "D", "speed_mph": 45.0, "accel_pedal_pct": 30.0, "brake_pct": 0.0},
    {"gear": "R", "speed_mph": 3.0, "accel_pedal_pct": 10.0, "brake_pct": 5.0},
])
def test_the_power_identity_sum_of_loads_equals_minus_power_plus_i2r(knobs):
    """Σloads = −P_terminal + I²R when nothing is charging or regenerating:
    the pack delivers the loads plus its own ohmic loss."""
    sim = leaf(internal_resistance_ohm=0.08, **knobs)
    st = sim.state()
    assert st["regen_kw"] == 0.0 and not st["charging"]
    i = st["current_a"]
    loads = sum(v for n, v in st["loads_w"].items() if n != "regen")
    assert loads == pytest.approx(-st["power_kw"] * 1000.0 + i * i * 0.08, rel=0.01, abs=1.0)
    assert st["load_total_w"] == pytest.approx(loads, abs=0.5)


def test_charging_with_the_car_off_is_still_a_positive_current():
    st = leaf(start_state="off", charging=True, charger="l2", soc=40.0).state()
    assert st["current_a"] > 0 and st["power_kw"] > 0
    assert st["wall_kw"] == pytest.approx(st["charge_power_kw"] / M.CHARGE_EFF["l2"], abs=0.001)
    assert st["charge_eff"] == M.CHARGE_EFF["l2"]


def test_charge_efficiency_table_has_the_measured_pair():
    assert M.CHARGE_EFF["l1"] == pytest.approx(0.78, abs=0.01)
    assert M.CHARGE_EFF["l2"] == pytest.approx(0.91, abs=0.01)
    assert set(M.CHARGE_EFF) == set(M.CHARGER_NAMES)


def test_a_day_parked_and_off_costs_under_one_percent():
    sim = leaf(start_state="off", soc=80.0, capacity_ah=24.0)
    sim.step(24 * 3600.0)
    assert 80.0 - sim.state()["soc"] < 1.0


def test_an_hour_ready_with_the_heater_on_costs_real_charge():
    sim = leaf(soc=80.0, capacity_ah=24.0, hvac_on=True, heater_level=40)
    sim.step(3600.0)
    # 5 kW for an hour out of ~9 kWh
    assert 80.0 - sim.state()["soc"] > 45.0


def test_state_carries_every_fahrenheit_twin():
    st = leaf(cabin_temp_c=30.0, ambient_c=35.0, evap_c=5.0, pack_temp_c=28.0).state()
    from simulator.units import c_to_f
    for c, f in (("pack_temp_c", "pack_temp_f"), ("temp_avg_c", "temp_avg_f"),
                 ("cabin_temp_c", "cabin_temp_f"), ("ambient_c", "ambient_f"),
                 ("evap_c", "evap_f"), ("charge_temp_limit_c", "charge_temp_limit_f")):
        assert st[f] == pytest.approx(c_to_f(st[c]), abs=0.1), f
    assert st["temps_f"] == [c_to_f(t) for t in st["temps_c"]]
    assert st["hvac_setpoint_c"] == pytest.approx((st["hvac_setpoint_f"] - 32) * 5 / 9, abs=0.1)


def test_encode_takes_fan_volts_and_door_bits_from_the_model():
    """One table each; the encoder must not carry its own copy."""
    from simulator import encode as E
    assert E.FAN_VOLTS is M.FAN_VOLTS and E.DOOR_BITS is M.DOOR_BITS


# ── the one power identity ───────────────────────────────────────────────
#
# `LeafModel.power()` is the only place the budget is summed, and everything
# else — the current, the dashboard's power figure, the cockpit's chips —
# reads it. These pin the identity itself, then the case that made it
# necessary: a Level 2 charge with the A/C at full blast used to report the
# whole 3.3 kW going into the pack and 0 W of A/C.

IDENTITY_CASES = [
    ("ready idle", {}),
    ("driving", {"gear": "D", "speed_mph": 45.0, "accel_pedal_pct": 30.0}),
    ("regen", {"gear": "Eco", "speed_mph": 30.0, "brake_pct": 40.0}),
    ("charging", {"start_state": "off", "charging": True, "charger": "l2", "soc": 45.0}),
    ("charging + hvac", {"start_state": "off", "charging": True, "charger": "l2", "soc": 45.0,
                         "hvac_on": True, "hvac_ac_on": True, "hvac_fan_speed": 7,
                         "cabin_temp_c": 35.0, "ambient_c": 35.0, "hvac_setpoint_f": 60.0}),
    ("off with the lights on", {"start_state": "off", "headlights": True,
                                "parking_lights": True}),
    ("an extra current on top", {"current_a": -20.0, "headlights": True}),
]


@pytest.mark.parametrize("name,knobs", IDENTITY_CASES, ids=[c[0] for c in IDENTITY_CASES])
def test_the_power_identity_holds_in_every_regime(name, knobs):
    """pack_kw = charger + regen − loads + extra, and the current is that
    over the pack's open-circuit voltage. One sum, one place."""
    sim = leaf(**knobs)
    st = sim.state()
    p = st["power"]
    assert p["pack_kw"] == pytest.approx(
        p["charger_kw"] + p["regen_kw"] - p["loads_kw"] + p["extra_kw"], abs=0.002)
    assert p["loads_total_w"] == pytest.approx(st["load_total_w"], abs=0.5)
    assert p["loads_kw"] * 1000.0 == pytest.approx(
        sum(w for n, w in st["loads_w"].items() if n != "regen"), abs=1.0)
    v = sim.model.ocv_pack()
    assert st["current_a"] == pytest.approx(p["pack_kw"] * 1000.0 / v, abs=0.002)
    # power_kw is the same number at the TERMINALS: the pack sags under load,
    # so the two differ by the I²R the pack's own resistance eats
    i = st["current_a"]
    r = sim.get_knobs()["internal_resistance_ohm"]
    assert st["power_kw"] == pytest.approx(p["pack_kw"] + i * i * r / 1000.0, abs=0.05)
    assert st["pack_kw"] == p["pack_kw"]


def test_the_state_and_the_record_both_carry_the_budget():
    sim = leaf(start_state="off", charging=True, charger="l2", soc=50.0)
    for doc in (sim.state(), sim.record()):
        p = doc["power"]
        for key in ("wall_kw", "charger_kw", "loads_kw", "loads_total_w", "regen_kw",
                    "extra_kw", "pack_kw", "hvac_kw", "motor_kw", "charge_eff",
                    "load_override"):
            assert key in p, key
        assert doc["pack_kw"] == p["pack_kw"]
        assert doc["loads_total_w"] == p["loads_total_w"]


def test_wall_is_the_charge_power_over_the_efficiency():
    st = leaf(start_state="off", charging=True, charger="l2", soc=40.0).state()
    p = st["power"]
    assert p["charger_kw"] == pytest.approx(st["charge_power_kw"], abs=0.001)
    assert p["wall_kw"] == pytest.approx(p["charger_kw"] / M.CHARGE_EFF["l2"], abs=0.002)
    assert p["wall_kw"] > p["charger_kw"] > p["pack_kw"], "losses, then the accessories"
    assert st["wall_kw"] == pytest.approx(p["wall_kw"], abs=0.001)


def test_the_load_override_still_replaces_the_whole_budget():
    st = leaf(load_kw=10.0, headlights=True, hvac_on=True, hvac_ac_on=True).state()
    p = st["power"]
    assert p["load_override"] is True
    assert p["loads_kw"] == pytest.approx(10.0) and p["pack_kw"] == pytest.approx(-10.0)


# ── the bug this audit was called for ────────────────────────────────────

def l2_with_the_ac_flat_out(**extra):
    k = {"start_state": "off", "charging": True, "charger": "l2", "soc": 45.0,
         "hvac_on": True, "hvac_ac_on": True, "hvac_fan_speed": 7,
         "cabin_temp_c": 35.0, "ambient_c": 35.0, "hvac_setpoint_f": 60.0}
    k.update(extra)
    return leaf(**k)


def test_ac_on_a_level_2_charge_comes_off_the_charge_power():
    """The owner's report, as an assertion: on L2 with the A/C at full blast
    the dashboard showed a full 3.3 kW going into the pack."""
    st = l2_with_the_ac_flat_out().state()
    p = st["power"]
    assert st["hvac_kw"] > 0.3, "the climate system runs on the plug"
    assert p["hvac_kw"] > 0.3
    assert p["pack_kw"] < p["charger_kw"] - 0.3, "the accessories come off the charge"
    assert p["pack_kw"] == pytest.approx(p["charger_kw"] - p["loads_kw"], abs=0.002)
    assert st["power_kw"] == pytest.approx(p["pack_kw"], abs=0.05), "power_kw ≈ pack_kw"


def test_a_charging_car_is_awake_not_asleep():
    """The base row while charging is the base_on class — DC-DC, LBC, pump and
    the charger's electronics — not the 3 W of a sleeping car."""
    st = leaf(start_state="off", charging=True, charger="l2", soc=45.0).state()
    assert st["loads_w"]["base"] == LOADS_W["base_charging"]
    assert LOADS_W["base_on"] <= LOADS_W["base_charging"] <= LOADS_W["base_ready"]
    assert LOADS_W["base_charging"] > 10 * LOADS_W["base_off"]


@pytest.mark.parametrize("state", ["off", "acc", "on", "ready"])
def test_hvac_draws_while_charging_whatever_the_ignition_knob_says(state):
    st = l2_with_the_ac_flat_out(start_state=state).state()
    assert st["loads_w"]["ac"] > 0 and st["loads_w"]["blower"] > 0


def test_the_taper_still_shapes_the_charge_and_the_loads_come_off_after():
    """Order matters: the taper is a function of SOC applied to charger_kw;
    the accessories are subtracted from what is left."""
    low = l2_with_the_ac_flat_out(soc=45.0).state()["power"]
    high = l2_with_the_ac_flat_out(soc=95.0).state()["power"]
    assert high["charger_kw"] < low["charger_kw"], "the taper is still there"
    assert low["loads_kw"] == pytest.approx(high["loads_kw"], rel=0.02), "loads do not taper"
    assert high["pack_kw"] < 0, "past the knee the A/C outruns the charger"


def test_the_motor_never_runs_with_the_connector_latched():
    st = leaf(start_state="ready", charging=True, gear="D", speed_mph=40.0,
              accel_pedal_pct=50.0).state()
    assert st["motor_kw"] == 0.0 and st["regen_kw"] == 0.0


# ── the couplings audit ──────────────────────────────────────────────────

def test_every_coupling_carries_a_verdict_and_a_reason():
    """The audit is a table, not a memory. Each row says implemented, kept or
    deliberately not modelled — and why."""
    assert len(M.COUPLINGS) >= 15
    for name, (verdict, why) in M.COUPLINGS.items():
        assert verdict in M.COUPLING_VERDICTS, f"{name}: {verdict!r}"
        assert len(why) > 60, f"{name}: the reason is too thin to be one"
    assert any(v == "not modelled" for v, _ in M.COUPLINGS.values()), \
        "an audit with nothing left out is not an audit"


def test_compressor_speed_follows_demand_not_the_blower():
    """It used to read 1500 + 130·fan: the compressor sped up because somebody
    turned the fan up in a cabin that was already cold."""
    def rpm(fan=3, **k):
        return leaf(hvac_on=True, hvac_ac_on=True, hvac_fan_speed=fan,
                    **k).state()["compressor_rpm"]
    mild = rpm(cabin_temp_c=22.0, ambient_c=22.0, hvac_setpoint_f=72.0)
    hot = rpm(cabin_temp_c=40.0, ambient_c=40.0, hvac_setpoint_f=60.0)
    assert hot > mild + 500, "demand is what drives it"
    by_fan = [rpm(fan=f, cabin_temp_c=30.0, ambient_c=30.0) for f in (1, 4, 7)]
    assert len(set(by_fan)) == 1, "the blower does not drive the compressor"
    assert leaf(hvac_on=True, hvac_ac_on=False).state()["compressor_rpm"] == 0
    assert 1000 <= hot <= 3200, "the band the real car was seen in"


def test_the_evaporator_is_pulled_down_by_the_compressor_and_warmed_by_airflow():
    def evap(fan):
        s = leaf(hvac_on=True, hvac_ac_on=True, hvac_fan_speed=fan, cabin_temp_c=30.0,
                 ambient_c=30.0, evap_c=30.0)
        s.step(600.0)
        return s.state()["evap_c"]
    assert evap(1) < 10.0, "the coil gets cold"
    assert evap(7) > evap(1), "more air over the coil, warmer coil"


def test_the_cabin_only_reaches_a_setpoint_when_something_is_running():
    """A blower on its own is ventilation, and a car with no power runs
    nothing at all — this used to head for the setpoint in both cases."""
    def cabin(**k):
        s = leaf(cabin_temp_c=35.0, ambient_c=35.0, hvac_setpoint_f=60.0, **k)
        s.step(1800.0)
        return s.state()["cabin_temp_c"]
    assert cabin(hvac_on=True, hvac_ac_on=True, hvac_fan_speed=5) < 25.0
    assert cabin(hvac_on=True, hvac_fan_speed=5) > 30.0, "fan only: ventilation"
    assert cabin(start_state="off", hvac_on=True, hvac_ac_on=True, hvac_fan_speed=5) > 30.0
    # ...but on the plug the climate system works, which is pre-conditioning
    assert cabin(start_state="off", charging=True, charger="l2", hvac_on=True,
                 hvac_ac_on=True, hvac_fan_speed=5) < 25.0


def test_headlights_left_on_flatten_the_12_v_battery():
    """The one 12 V behaviour every owner has met. There is no forced 14 V
    DC-DC bus here — see the lv_volts row in COUPLINGS for why."""
    s = leaf(start_state="off", headlights=True, lv_volts=12.68)
    for _ in range(6):
        s.step(3600.0)
    st = s.state()
    assert st["lv_volts"] < M.LV_WARN_V
    assert st["lamps"]["charge_12v"] is True
    quiet = leaf(start_state="off", lv_volts=12.68)
    quiet.step(6 * 3600.0)
    assert quiet.state()["lv_volts"] == pytest.approx(12.68, abs=0.05), "a dark car keeps it"


def test_the_converter_holds_the_12_v_up_while_the_car_runs():
    s = leaf(start_state="ready", lv_volts=11.4)
    s.step(3600.0)
    assert s.state()["lv_volts"] > 12.0
    charging = leaf(start_state="off", charging=True, charger="l2", lv_volts=11.4)
    charging.step(3600.0)
    assert charging.state()["lv_volts"] > 12.0


def test_the_12_v_reading_is_still_whatever_the_knob_says_right_now():
    """The owner's own 2026-08-24 capture reads 12.677 V with the car READY;
    nothing here may contradict it."""
    assert leaf(start_state="ready", lv_volts=12.677).state()["lv_volts"] == \
        pytest.approx(12.677, abs=0.01)


def test_range_is_soc_times_capacity_and_the_accessories_shorten_it():
    plain = leaf(soc=80.0, capacity_ah=24.0)
    st = plain.state()
    kwh = 24.0 * plain.model.ocv_pack() * 0.8 / 1000.0
    assert st["range_km"] == pytest.approx(kwh / M.RANGE_KWH_PER_KM, rel=0.01)
    half = leaf(soc=40.0, capacity_ah=24.0).state()["range_km"]
    assert half == pytest.approx(st["range_km"] / 2, rel=0.05), "half the charge, half the range"
    ac = leaf(soc=80.0, capacity_ah=24.0, hvac_on=True, hvac_ac_on=True, hvac_fan_speed=5,
              cabin_temp_c=38.0, ambient_c=38.0, hvac_setpoint_f=60.0).state()["range_km"]
    assert ac < st["range_km"] * 0.85, "3 kW of A/C is a quarter of the range"


def test_a_tired_pack_has_less_range_than_a_healthy_one_at_the_same_soc():
    tired = leaf(soc=70.0, capacity_ah=23.16).state()["range_km"]
    healthy = leaf(soc=70.0, capacity_ah=60.0).state()["range_km"]
    assert healthy > tired * 2


# ── the shipped scenarios run at real time ───────────────────────────────

def test_every_shipped_scenario_runs_at_real_time_or_says_why_not():
    """A scenario that carries its own clock decides how fast you see it
    whether you wanted that or not. --speed is how time gets compressed."""
    import json
    import os
    from simulator import SCENARIO_DIR, scenario_names
    loud = 0
    for name in scenario_names():
        with open(os.path.join(SCENARIO_DIR, name + ".json")) as f:
            data = json.load(f)
        clocks = [float((data.get("knobs") or {}).get("clock_scale", 1.0))]
        clocks += [float((e.get("set") or {}).get("clock_scale", 1.0))
                   for e in data.get("timeline") or []]
        if max(clocks) == 1.0:
            continue
        desc = data.get("description") or ""
        assert "DOES NOT RUN AT REAL TIME" in desc, \
            f"{name} ships clock_scale {max(clocks)} without saying so loudly"
        loud += 1
    assert loud == 1, "degradation_arc is the one exception; it is not a habit"
