# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`record()` — the model in the decoders' vocabulary.

The dashboard's tiles read what `vehicles/leaf_ze0.decode()` produces
(`door_driver`, `hvac_ambient_f`, `hvac_target_c` …); the model's `state()`
speaks its own language (`doors_open`, `ambient_c`, `hvac_setpoint_f`).
`LeafModel.record()` is the one mapping between them, and the standard it is
held to is the strongest one available: for every key the real decoders also
produce, `record()` must equal `encode(state) -> leaf_decoders.decode_*()`.
Anything the wire quantises (setpoint byte, quarter-psi tyres, fifths of a km
of range) the record quantises the same way, so a sim page and the dashboard
never disagree about a number.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import leaf_decoders as L                                       # noqa: E402
import vehicles.leaf_ze0 as V                                   # noqa: E402
from simulator import make_sim                                  # noqa: E402
from simulator import history                                   # noqa: E402
from simulator import model as M                                # noqa: E402

CARCAN_IDS = ("421", "358", "385", "5C5", "5A9", "355", "5B3", "284", "292", "60D", "180", "174")
LBC_CMDS = ("2101", "2102", "2103", "2104", "2105", "2106")


def leaf(**knobs):
    k = {"noise": 0.0}
    k.update(knobs)
    return make_sim(vehicle="leaf_ze0", knobs=k, seed=1)


def decoded(sim):
    """encode -> the profile's decode(), exactly the reader's path."""
    resp = {}
    for iid, it in V.ITEMS.items():
        if it["kind"] == "passive":
            resp[iid] = sim.frames(it["id"], 0.3)
        else:
            tx, rx = V.TARGETS[it["kind"]]
            resp[iid] = sim.respond(it["cmd"], tx, rx)
    rec, alive = V.decode(resp)
    assert alive
    return rec


# keys the wire carries exactly (or that the record quantises the wire's way)
EXACT = ("doors_raw", "door_driver", "door_pass", "door_rl", "door_rr", "door_hatch",
         "door_front", "door_rear", "door_trunk", "door_any", "locked",
         "headlights", "high_beam", "parking_lights", "fog_lights", "start_state_name",
         "gear", "turn_signal", "handbrake", "units_miles",
         "odometer_mi", "odometer_km", "range_km", "range_mi", "soh_dash_pct",
         "tpms_psi", "tpms_kpa", "brake_on",
         "hvac_blower_v", "hvac_fan_on", "hvac_fan_speed", "hvac_on", "hvac_ac_on",
         "hvac_target_f", "hvac_target_c", "hvac_compressor_rpm", "hvac_heater_level",
         "hvac_sunload", "hvac_decode",
         "cabin_temp_c", "cabin_temp_f", "hvac_ambient_c", "hvac_ambient_f",
         "hvac_evap_c", "hvac_evap_f",
         "temps", "temps_c", "temps_f", "temp_avg_c", "temp_avg_f",
         "cells", "cell_count", "cell_min", "cell_max", "cell_avg", "cell_spread",
         "cell_min_idx", "cell_max_idx", "balancing_active",
         "insulation_kohm", "discharging", "hv_current1_a", "g05_current_a")
# keys that go through a fixed-point scale: back within its resolution
APPROX = {"soc": 0.01, "pack_v": 0.01, "capacity_ah": 0.0001, "soh": 0.1, "hx": 0.01,
          "lv_volts": 0.001, "current_a": 0.001, "hv_current2_a": 0.001,
          "power_kw": 0.01, "speed_kmh": 0.01, "speed_mph": 0.1, "brake_pct": 0.5}

STATES = [
    {},
    {"start_state": "off", "locked": True, "charging": True, "charger": "l2", "soc": 40.0},
    {"doors": "driver,hatch", "headlights": True, "high_beam": True, "parking_lights": True,
     "fog_lights": True, "turn_signal": "hazards", "start_state": "acc", "locked": False},
    {"gear": "D", "speed_mph": 47.3, "accel_pedal_pct": 22.0, "brake_pct": 0.0,
     "handbrake": False, "hvac_on": True, "hvac_ac_on": True, "hvac_fan_speed": 5,
     "hvac_setpoint_f": 66.0, "cabin_temp_c": 31.0, "ambient_c": 34.0, "evap_c": 4.0,
     "sunload": 120, "odometer_mi": 123456.0, "tpms_psi": 32.3, "soh": 44.0, "soc": 70.0},
    {"gear": "Eco", "speed_mph": 12.0, "brake_pct": 40.0, "hvac_on": True, "hvac_fan_speed": 2,
     "heater_level": 24, "hvac_setpoint_f": 84.0, "cabin_temp_c": 9.0, "ambient_c": -3.0,
     "units_miles": False, "odometer_mi": 30000.0, "pack_temp_c": 3.0,
     "tpms_fl": 26.0, "tpms_rr": 44.5},
    {"gear": "R", "speed_mph": 2.0, "accel_pedal_pct": 8.0, "brake_pct": 3.0,
     "fault.cell_degraded": True, "weak_cell_index": 41, "cell_spread_mv": 55.0},
    {"fault.sensor_dropout": True, "pack_temp_c": 27.0, "charging": True, "soc": 92.0},
]


@pytest.mark.parametrize("knobs", STATES, ids=[str(i) for i in range(len(STATES))])
def test_record_equals_encode_then_decode(knobs):
    sim = leaf(**knobs)
    rec = sim.model.record(cells=True)
    dec = decoded(sim)
    missing = [k for k in EXACT + tuple(APPROX) if k not in dec]
    assert not missing, f"the decoders no longer produce {missing}; the record test needs updating"
    for key in EXACT:
        assert rec[key] == dec[key], f"{key}: record {rec[key]!r} != decoded {dec[key]!r}"
    for key, tol in APPROX.items():
        assert rec[key] == pytest.approx(dec[key], abs=tol), f"{key}: record {rec[key]!r} vs decoded {dec[key]!r}"


def test_the_record_carries_every_key_the_brief_names():
    want = """gear start_state_name speed_mph speed_kmh odometer_mi odometer_km range_mi
        range_km soh_dash_pct handbrake doors_raw door_driver door_pass door_rl door_rr
        door_hatch door_front door_rear door_trunk door_any headlights high_beam
        parking_lights fog_lights turn_signal brake_on brake_pct locked tpms_psi tpms_kpa
        cabin_temp_c cabin_temp_f hvac_ambient_c hvac_ambient_f hvac_evap_c hvac_evap_f
        hvac_sunload hvac_decode temp_avg_c temp_avg_f temps_c temps_f hvac_target_c
        hvac_target_f hvac_ac_on hvac_compressor_rpm hvac_heater_level hvac_on hvac_blower_v
        hvac_fan_on hvac_fan_speed soc pack_v current_a power_kw capacity_ah soh hx lv_volts
        insulation_kohm cell_min cell_max cell_avg cell_spread cell_min_idx cell_max_idx
        cell_count cells discharging g05_current_a hv_current1_a hv_current2_a
        lamps lamps_unmodelled messages loads_w simulated""".split()
    rec = leaf().model.record()
    assert not [k for k in want if k not in rec]
    assert rec["simulated"] is True


def test_every_temperature_in_the_record_has_its_fahrenheit_twin():
    rec = leaf(cabin_temp_c=27.0, ambient_c=33.0, evap_c=6.0, pack_temp_c=31.0).model.record()
    from simulator.units import c_to_f
    pairs = [(k, k[:-2] + "_f") for k in rec if k.endswith("_c") and isinstance(rec[k], (int, float))]
    assert pairs
    for c, f in pairs:
        assert f in rec, f"{c} has no {f}"
        assert rec[f] == c_to_f(rec[c]) or rec[f] == pytest.approx(c_to_f(rec[c]), abs=0.1), c
    assert rec["temps_f"] == [c_to_f(t) for t in rec["temps_c"]]


def test_blower_volts_come_from_the_fan_walk_not_a_multiplier():
    """The old generator wrote 1.5 V per fan step; the car says 4/5/6/8/9/11/11."""
    for fan in range(8):
        rec = leaf(hvac_on=fan > 0, hvac_fan_speed=fan).model.record()
        assert rec["hvac_blower_v"] == M.FAN_VOLTS[fan]
        assert rec["hvac_fan_on"] is (fan > 0)
    assert leaf(hvac_on=False, hvac_fan_speed=5).model.record()["hvac_blower_v"] == 0


def test_tpms_order_is_the_wire_order():
    rec = leaf(tpms_fl=31.0, tpms_fr=32.0, tpms_rr=33.0, tpms_rl=34.0).model.record()
    assert rec["tpms_psi"] == [31.0, 32.0, 33.0, 34.0]        # FL FR RR RL, as 0x385
    assert rec["tpms_kpa"] == [round(p * 6.89476, 1) for p in rec["tpms_psi"]]


def test_record_resolves_every_declared_history_column():
    from store import _resolve
    from vehicles import history_cols
    rec = leaf(charging=True, hvac_on=True, hvac_ac_on=True, hvac_fan_speed=3).model.record()
    missing = [c for c, s in history_cols(V).items() if _resolve(rec, s["key"]) is None]
    assert not missing


def test_history_record_from_state_is_the_same_mapping_plus_the_stamp():
    sim = leaf(doors="pass", hvac_on=True, hvac_fan_speed=4)
    st = sim.state()
    a = history.record_from_state(st, cells=True)
    b = M.record_from_state(st, cells=True)
    assert a["generated"] is True and a["sim_source"].startswith("hakake_sim")
    for k in b:
        assert a[k] == b[k], k


def test_simulator_record_adds_the_marker_fields():
    sim = make_sim(vehicle="leaf_ze0", scenario="idle", seed=4)
    rec = sim.record()
    assert rec["simulated"] is True and rec["vehicle"] == "leaf_ze0"
    assert rec["scenario"] == "idle" and rec["seed"] == 1     # the scenario's own seed
    assert rec["time_scale"] == sim.time_scale()
    assert rec["faults"] == []
    assert "cells" in rec and "cells" not in sim.record(cells=False)
    assert sim.can_record()


def test_a_core_without_record_says_so_instead_of_inventing_one():
    sim = make_sim(vehicle="lancer_2009")
    assert not sim.can_record()
    with pytest.raises(NotImplementedError):
        sim.record()


def test_clear_scenario_drops_the_timeline_and_keeps_the_knobs():
    sim = make_sim(scenario="drive")
    knobs = sim.get_knobs()
    sim.clear_scenario()
    assert sim.scenario is None
    assert sim.get_knobs() == knobs
    sim.step(30.0)
    assert sim.get_knobs()["speed_mph"] == 0.0, "the t=5 entry must not fire after clearing"
    assert sim.record()["scenario"] is None


def test_record_is_json_serialisable():
    import json
    json.dumps(leaf(**{"fault.sensor_dropout": True}).record())
