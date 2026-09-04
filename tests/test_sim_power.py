# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The push-button start.

The ZE0 is switched on by a button and a brake pedal, and the simulator
emulates that rather than only exposing `start_state` as a knob — because the
things that make the real switch interesting are refusals: it will not go
READY with the charge connector latched, it will not go READY out of gear, it
parks the car by itself on the way off, and it will not switch off while the
car is moving unless you hold it.

Every rule here is the 2012 Owner's Manual's (pp. 5-7..5-13), cited in
`LeafModel.press_power`. Where a wording or a threshold is ours rather than
the manual's, the model says ASSERTED and so does the test.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulator import make_sim                                   # noqa: E402


def leaf(**knobs):
    k = {"noise": 0.0, "start_state": "off", "gear": "P"}
    k.update(knobs)
    return make_sim(vehicle="leaf_ze0", knobs=k, seed=1)


# ── the transition table ─────────────────────────────────────────────────
#
# from, brake, extra knobs -> start_state, gear, accepted

TABLE = [
    # no brake: OFF -> ACC -> ON -> OFF (p. 5-8)
    ("off", False, {}, "acc", "P", True),
    ("acc", False, {}, "on", "P", True),
    ("on", False, {}, "off", "P", True),
    # brake: READY from any switch position, with the selector in P or N (5-8, 5-11)
    ("off", True, {}, "ready", "P", True),
    ("acc", True, {}, "ready", "P", True),
    ("on", True, {}, "ready", "P", True),
    ("off", True, {"gear": "N"}, "ready", "N", True),
    # ...but not in a driving gear (5-11)
    ("off", True, {"gear": "D"}, "off", "D", False),
    ("on", True, {"gear": "R"}, "on", "R", False),
    # a press while READY switches off AND applies P by itself (5-11 step 4, 5-13 NOTE)
    ("ready", False, {}, "off", "P", True),
    ("ready", True, {}, "off", "P", True),
    ("ready", False, {"gear": "D"}, "off", "P", True),
    # not while it is moving (ASSERTED, from 5-14)
    ("ready", False, {"gear": "D", "speed_mph": 25.0}, "ready", "D", False),
    # the connector is latched: no READY (p. 2-19), but ACC and ON still work
    ("off", True, {"plugged_in": True}, "off", "P", False),
    ("off", False, {"plugged_in": True}, "acc", "P", True),
    ("acc", False, {"charging": True}, "on", "P", True),
    ("off", True, {"charging": True}, "off", "P", False),
]


@pytest.mark.parametrize("frm,brake,extra,to,gear,ok", TABLE,
                         ids=[f"{r[0]}{'+brake' if r[1] else ''}"
                              f"{'+' + ','.join(f'{k}={v}' for k, v in r[2].items()) if r[2] else ''}"
                              for r in TABLE])
def test_the_power_switch_transition_table(frm, brake, extra, to, gear, ok):
    sim = leaf(start_state=frm, **extra)
    out = sim.press_power(brake=brake)
    assert out["start_state"] == to, out
    assert out["gear"] == gear, out
    assert out["accepted"] is ok
    assert out["message"], "the car always says something"
    assert sim.get_knobs()["start_state"] == to
    assert sim.get_knobs()["gear"] == gear


def test_three_pushes_walk_the_switch_all_the_way_round():
    sim = leaf()
    assert [sim.press_power(brake=False)["start_state"] for _ in range(4)] == \
        ["acc", "on", "off", "acc"]


def test_the_brake_pedal_knob_is_the_default_answer():
    """A cockpit that drives the pedal needs no second control."""
    assert leaf(brake_pct=40.0).press_power()["start_state"] == "ready"
    assert leaf(brake_pct=0.0).press_power()["start_state"] == "acc"
    # ...and an explicit flag overrides it either way
    assert leaf(brake_pct=40.0).press_power(brake=False)["start_state"] == "acc"


def test_the_refusals_say_what_to_do():
    assert leaf(gear="D").press_power(brake=True)["message"] == "Shift to P or N"
    assert "connector" in leaf(plugged_in=True).press_power(brake=True)["message"].lower()
    moving = leaf(start_state="ready", gear="D", speed_mph=30.0).press_power()
    assert moving["message"] == "Stop vehicle" and moving["accepted"] is False


def test_a_held_press_is_the_emergency_shut_off():
    """ASSERTED, from the 5-9 emergency procedure: holding the button is the
    only way the car switches off while it is rolling."""
    sim = leaf(start_state="ready", gear="D", speed_mph=45.0)
    assert sim.press_power(hold=True)["start_state"] == "off"


def test_the_emergency_shut_off_needs_the_car_to_be_ready_first():
    """The 5-9 procedure stops the EV system *while driving*, so it presupposes
    READY. `speed_mph` is an independently settable knob, so an API caller (or
    an agent driving knobs) can hand the model a car that is OFF and "moving";
    a held press there must be the ordinary no-brake cycle, not an emergency
    stop the car is in no state to perform."""
    for frm, want in (("off", "acc"), ("acc", "on"), ("on", "off")):
        sim = leaf(start_state=frm, gear="D", speed_mph=45.0)
        out = sim.press_power(brake=False, hold=True)
        assert out["start_state"] == want, out
        assert out["message"] != "Emergency shut off", out


def test_leaving_ready_stops_the_hvac_and_the_motor_draw():
    sim = leaf(start_state="ready", gear="D", speed_mph=40.0, accel_pedal_pct=30.0,
               hvac_on=True, hvac_ac_on=True, hvac_fan_speed=7, cabin_temp_c=35.0)
    before = sim.state()
    assert before["loads_w"]["ac"] > 0 and before["motor_kw"] > 0
    sim.set(speed_mph=0.0)
    sim.press_power(brake=False)
    after = sim.state()
    assert after["loads_w"]["ac"] == 0.0 and after["motor_kw"] == 0.0
    assert after["load_total_w"] < 10.0, "an off car draws a few watts"


def test_the_ignition_knob_is_still_directly_settable():
    """The button is a second way in, not a replacement: a scenario or an
    agent that just wants the car READY still says so."""
    sim = leaf()
    sim.set(start_state="ready")
    assert sim.state()["start_state"] == "ready"


def test_the_connector_knobs_cannot_disagree():
    """A car cannot be charging without being plugged in, and unplugging it
    stops the charge."""
    sim = leaf()
    assert sim.set(charging=True)["plugged_in"] is True
    assert sim.set(plugged_in=False)["charging"] is False
    st = leaf(plugged_in=True).state()
    assert st["lamps"]["plug_in"] is True
    assert "Charge connector connected" in st["messages"]


def test_a_core_without_a_power_switch_says_so_rather_than_pretending():
    lancer = make_sim(vehicle="lancer_2009", seed=1)
    assert lancer.can_press_power() is False
    with pytest.raises(NotImplementedError):
        lancer.press_power()
