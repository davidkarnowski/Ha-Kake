# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cluster: every ZE0 indicator the model can drive, and honesty about
the ones it cannot.

One parametrised case per lamp — the knob that lights it, and the default
state in which it is dark. The two aggregates (red and yellow master
warnings) are checked against their definitions, and the unmodelled set is
checked to be always False and disjoint from anything lit.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulator import make_sim                                   # noqa: E402
from simulator.model import LAMPS_UNMODELLED, LAMP_NAMES           # noqa: E402

# a default car: READY, in P, handbrake on, doors shut, unlocked
BASE = {"noise": 0.0, "start_state": "ready", "handbrake": False, "locked": False}


def lamps(**knobs):
    k = dict(BASE)
    k.update(knobs)
    return make_sim(vehicle="leaf_ze0", knobs=k, seed=1).model.lamps()


CASES = [
    ("ready", {"start_state": "ready"}, {"start_state": "off"}),
    ("turn_left", {"turn_signal": "left"}, {}),
    ("turn_right", {"turn_signal": "right"}, {}),
    ("hazards", {"turn_signal": "hazards"}, {"turn_signal": "left"}),
    ("low_beam", {"headlights": True}, {}),
    ("high_beam", {"high_beam": True}, {}),
    ("position", {"parking_lights": True}, {}),
    ("fog", {"fog_lights": True}, {}),
    ("parking_brake", {"handbrake": True}, {}),
    ("door_ajar", {"doors": "rl"}, {}),
    ("plug_in", {"charging": True}, {}),
    ("charge_12v", {"lv_volts": 11.5}, {}),
    ("ev_system", {"fault.insulation_low": True}, {}),
    ("power_limit", {"pack_temp_c": 62.0}, {}),
    ("low_battery", {"soc": 9.0}, {}),
    ("tpms", {"tpms_rr": 27.0}, {}),
    ("headlight_warning", {"headlights": True, "start_state": "off"}, {"headlights": True}),
    ("security", {"locked": True, "start_state": "off"}, {"locked": True}),
    ("eco", {"gear": "Eco"}, {"gear": "D"}),
]


@pytest.mark.parametrize("name,on,off", CASES, ids=[c[0] for c in CASES])
def test_each_lamp_lights_on_its_knob_and_is_dark_otherwise(name, on, off):
    assert lamps(**on)[name] is True
    assert lamps(**off)[name] is False


def test_the_lamp_set_is_complete_and_boolean():
    L = lamps()
    assert set(L) == set(LAMP_NAMES)
    assert all(isinstance(v, bool) for v in L.values())
    assert {c[0] for c in CASES} | {"master_red", "master_yellow"} == set(LAMP_NAMES)


# ── the ones with more than one trigger ──────────────────────────────────

def test_position_lamp_also_lights_with_the_low_beam():
    assert lamps(headlights=True)["position"] is True


def test_hazards_light_both_arrows():
    L = lamps(turn_signal="hazards")
    assert L["turn_left"] and L["turn_right"] and L["hazards"]


def test_charge_12v_also_comes_from_the_fault():
    assert lamps(**{"fault.lv_battery_weak": True})["charge_12v"] is True


@pytest.mark.parametrize("fault", ["fault.cell_degraded", "fault.insulation_low",
                                   "fault.sensor_dropout", "fault.ecu_nrc"])
def test_ev_system_lamp_covers_every_pack_fault(fault):
    assert lamps(**{fault: True})["ev_system"] is True


@pytest.mark.parametrize("fault", ["fault.car_asleep", "fault.adapter_silent", "fault.bus_noise"])
def test_rig_faults_are_not_ev_system_faults(fault):
    assert lamps(**{fault: True})["ev_system"] is False


def test_power_limit_turtle_from_heat_cold_and_empty():
    assert lamps(pack_temp_c=62.0)["power_limit"] is True       # output_avail < 0.6
    assert lamps(pack_temp_c=-5.0)["power_limit"] is True
    assert lamps(soc=6.0)["power_limit"] is True
    assert lamps(pack_temp_c=30.0, soc=50.0)["power_limit"] is False


def test_output_avail_is_the_panels_derate():
    m = make_sim(vehicle="leaf_ze0", knobs=dict(BASE, pack_temp_c=30.0, soc=50.0), seed=1).model
    assert m.output_avail() == 1.0
    m.k["soc"] = 7.5
    assert m.output_avail() == pytest.approx(0.5, abs=0.01)
    m.k["soc"] = 50.0
    m.k["pack_temp_c"] = 60.0
    assert 0.35 <= m.output_avail() < 0.6


def test_tpms_ignores_a_missing_sensor():
    assert lamps(tpms_fl=0.0)["tpms"] is False, "0 psi is a missing sensor, not flat tyres"
    assert lamps(tpms_fl=29.9)["tpms"] is True
    assert lamps(tpms_fl=30.0)["tpms"] is False


def test_low_battery_boundary():
    assert lamps(soc=10.0)["low_battery"] is True
    assert lamps(soc=10.1)["low_battery"] is False


# ── the aggregates ───────────────────────────────────────────────────────

def test_master_red_from_ev_system_or_moving_with_a_door_or_the_handbrake():
    assert lamps()["master_red"] is False
    assert lamps(**{"fault.cell_degraded": True})["master_red"] is True
    assert lamps(doors="driver")["master_red"] is False, "a door open while parked is not red"
    assert lamps(doors="driver", gear="D", speed_mph=10.0)["master_red"] is True
    assert lamps(handbrake=True, gear="D", speed_mph=10.0)["master_red"] is True
    assert lamps(handbrake=True, gear="D", speed_mph=2.0)["master_red"] is False
    assert lamps(handbrake=True, gear="N", speed_mph=10.0)["master_red"] is False


def test_master_yellow_from_any_of_its_five():
    assert lamps()["master_yellow"] is False
    for knobs in ({"lv_volts": 11.0}, {"tpms_rl": 25.0}, {"soc": 8.0},
                  {"pack_temp_c": 65.0}, {"headlights": True, "start_state": "off"}):
        assert lamps(**knobs)["master_yellow"] is True, knobs
    assert lamps(doors="hatch")["master_yellow"] is False


# ── the honest part ──────────────────────────────────────────────────────

def test_unmodelled_lamps_are_always_false_and_never_among_the_lit():
    from simulator.model import LeafModel
    assert set(LAMPS_UNMODELLED) == {"abs", "vsp", "brake_yellow", "brake_red", "ps",
                                     "shift_control", "vdc", "vdc_off", "seatbelt", "airbag",
                                     "passenger_airbag"}
    everything_on = dict(BASE, **{"fault.cell_degraded": True, "fault.lv_battery_weak": True,
                                  "doors": "all", "headlights": True, "high_beam": True,
                                  "fog_lights": True, "turn_signal": "hazards", "handbrake": True,
                                  "gear": "Eco", "speed_mph": 20.0, "soc": 5.0, "tpms_psi": 20.0,
                                  "charging": True})
    sim = make_sim(vehicle="leaf_ze0", knobs=everything_on, seed=1)
    lit = {n for n, v in sim.model.lamps().items() if v}
    assert lit, "the everything-on car should light something"
    un = LeafModel.lamps_unmodelled()
    assert set(un) == set(LAMPS_UNMODELLED) and not any(un.values())
    assert set(un) & lit == set()
    assert set(un) & set(LAMP_NAMES) == set()
    st = sim.state()
    assert st["lamps_unmodelled"] == un and st["lamps"] == sim.model.lamps()


# ── the dot-matrix line ──────────────────────────────────────────────────

def test_messages_match_their_lamps():
    m = lambda **k: make_sim(vehicle="leaf_ze0", knobs=dict(BASE, **k), seed=1).model.messages()
    assert m() == []
    assert "Battery level is low" in m(soc=9.0)
    assert "Motor power is limited" in m(pack_temp_c=65.0)
    assert "Check tire pressure" in m(tpms_fr=24.0)
    assert "Door open" in m(doors="pass")
    assert "Parking brake on" in m(handbrake=True, gear="D")
    assert "Parking brake on" not in m(handbrake=True, gear="P")
    assert "Charge connector connected" in m(charging=True)
    both = m(soc=9.0, doors="hatch")
    assert both.index("Battery level is low") < both.index("Door open")


# ── labels ───────────────────────────────────────────────────────────────

def test_every_knob_of_every_profile_has_a_label():
    from simulator import KNOBS
    for veh, kset in KNOBS.items():
        schema = make_sim(vehicle=veh).knob_schema()
        for name, spec in schema.items():
            assert spec.get("label"), f"{veh}.{name} has no label"
            assert spec["label"] != name, f"{veh}.{name} label is the raw name"
            assert len(spec["label"]) <= 40, f"{veh}.{name} label too long: {spec['label']!r}"
            assert spec["label"] == kset[name].label


def test_a_label_is_derived_from_help_when_none_is_given():
    from simulator.knobs import Knob, derive_label
    k = Knob("frobnicate_rate", "float", 1.0, "How fast to frobnicate; 0 = never (see docs)", "Hz", 0, 9)
    assert k.label == "How fast to frobnicate"
    assert derive_label("Pack temperature the four sensors sit around") == "Pack temperature the four sensors sit"
    assert derive_label("", "fault.cell_degraded") == "Cell degraded"
    assert Knob("x", "bool", False, "whatever", label="Given").label == "Given"
