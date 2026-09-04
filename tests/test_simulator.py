# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Simulator tests — the round trip is the headline.

model state -> simulator/encode.py -> leaf_decoders.decode_* -> compare

**What this proves, honestly.** It pins the decoders against an explicitly
written spec (docs/SIGNALS.md, transcribed twice: once into leaf_decoders.py
and once into simulator/encode.py) and makes refactors of either side safe.
It does NOT prove the decoders are right about the car. An error shared by the
encoder and the decoder passes silently: this suite would not have caught the
÷1024-vs-×0.001 group-05 current bug this project actually had, because both
halves would have used ×0.001 and agreed with each other. Only the real car
settles that, and only docs/SIGNALS.md records the verdict.

The one place this file touches reality is `test_matches_the_real_car`, which
checks that a simulator fed the owner's real 2026-08-24 numbers produces a
frame that decodes back into the same ballpark as the real capture.
"""

import json
import os

import pytest

import leaf_decoders as L
from conftest import fixture
from simulator import KNOBS, Simulator, make_sim, scenario_names
from simulator import encode as E

LBC_CMDS = ("2101", "2102", "2103", "2104", "2105", "2106")


def leaf(**knobs):
    """A Leaf simulator with the noise turned off, so comparisons are exact."""
    k = {"noise": 0.0}
    k.update(knobs)
    return make_sim(vehicle="leaf_ze0", knobs=k, seed=1)


def lbc(sim, cmds=LBC_CMDS):
    return {c: sim.respond(c, "79B", "7BB") for c in cmds}


def carcan(sim, ids, secs=0.3):
    return {i: sim.frames(i, secs) for i in ids}


# ── ISO-TP framing ───────────────────────────────────────────────────────

def test_isotp_framing_matches_the_real_adapter():
    """Frame shapes and parsed lengths must match the real 2026-08-24 capture:
    39 / 200 / 32 / 18 / 74 / 25 bytes, with the same declared lengths."""
    real = fixture("lbc_raw_20260824.json")["groups"]
    sim = leaf()
    for cmd in LBC_CMDS:
        lines = sim.respond(cmd, "79B", "7BB")
        r = real[cmd]
        assert lines[0].split()[1:3] == r[0].split()[1:3], f"{cmd} PCI/length differs"
        assert len(L.parse_isotp(lines)) == len(L.parse_isotp(r)), f"{cmd} parsed length differs"
        assert len(lines) == len(r), f"{cmd} frame count differs"


def test_single_frame_response_round_trips():
    sim = leaf()
    lines = sim.respond("2100", "744", "764")     # HVAC group 00: 4 data bytes
    assert len(lines) == 1
    assert L.parse_isotp(lines, "764") == bytearray([0x80, 0x01, 0x80, 0x00])


def test_unknown_request_is_never_invented_data():
    sim = leaf()
    assert sim.respond("2199", "79B", "7BB") == ["NO DATA"]
    assert L.is_no_data(sim.respond("21FF", "79B", "7BB"))
    # the HVAC amp answers NRC 0x12 instead, which decode_hvac drops
    nrc = sim.respond("2120", "744", "764")
    assert nrc == ["764 03 7F 21 12"]
    assert L.decode_hvac({"2120": nrc}) == {}


# ── the round trip, group by group ───────────────────────────────────────

@pytest.mark.parametrize("soc,cap,temp,cur,spread", [
    (76.87, 23.1568, 34.0, -1.5, 30.0),
    (100.0, 66.0, 5.0, -0.2, 4.0),
    (12.5, 40.0, -8.0, -120.0, 150.0),
    (55.0, 30.0, 45.0, 22.0, 60.0),
])
def test_group01_round_trip(soc, cap, temp, cur, spread):
    """Group 01: every decoded field back within its encoding resolution."""
    sim = leaf(soc=soc, capacity_ah=cap, pack_temp_c=temp, current_a=cur,
               cell_spread_mv=spread)
    st = sim.state()
    d = L.decode_group01(L.parse_isotp(sim.respond("2101", "79B", "7BB")))
    assert d["soc"] == pytest.approx(st["soc"], abs=0.01)          # u24 / 10000
    assert d["pack_v"] == pytest.approx(st["pack_v"], abs=0.01)    # u16 / 100
    assert d["capacity_ah"] == pytest.approx(st["capacity_ah"], abs=0.0001)
    assert d["soh"] == pytest.approx(st["soh"], abs=0.1)
    assert d["hx"] == pytest.approx(st["hx"], abs=0.01)
    assert d["lv_volts"] == pytest.approx(st["lv_volts"], abs=0.001)
    assert d["insulation_kohm"] == st["insulation_kohm"]
    assert d["hv_current2_a"] == pytest.approx(st["current_a"], abs=0.001)
    # sensor 1 is the coarse one on this car (±2 A steps) — same value, ±0.5 A
    assert d["hv_current1_a"] == pytest.approx(st["current_a"], abs=0.5)


def test_group02_round_trip():
    sim = leaf(soc=64.0, cell_spread_mv=90.0)
    st = sim.state()
    cells = L.decode_group02(L.parse_isotp(sim.respond("2102", "79B", "7BB")))
    assert cells == st["cells"]                       # exact: u16 mV, no scaling
    stats = L.cell_stats(cells)
    assert stats["cell_count"] == 96
    assert stats["cell_min"] == st["cell_min"]
    assert stats["cell_max"] == st["cell_max"]
    assert stats["cell_spread"] == st["cell_spread"]
    assert stats["pack_v_cells"] == pytest.approx(st["pack_v"], abs=0.05)


def test_group03_round_trip():
    sim = leaf(cell_spread_mv=44.0)
    st = sim.state()
    d = L.decode_group03(L.parse_isotp(sim.respond("2103", "79B", "7BB")))
    assert d["g03_cell_max_mv"] == st["cell_max"]
    assert d["g03_cell_min_mv"] == st["cell_min"]


@pytest.mark.parametrize("temp", [-20.0, 0.0, 22.0, 34.0, 51.0])
def test_group04_round_trip(temp):
    sim = leaf(pack_temp_c=temp)
    st = sim.state()
    temps = L.decode_group04(L.parse_isotp(sim.respond("2104", "79B", "7BB")))
    assert [t["c"] for t in temps] == st["temps_c"]           # exact: s8 °C
    assert [t["f"] for t in temps] == [L.c_to_f(c) for c in st["temps_c"]]
    # the u16 raw is a thermistor count, monotonically falling with temperature
    assert temps[0]["raw"] == E.temp_raw(st["temps_c"][0])


@pytest.mark.parametrize("cur", [-1.5, -28.0, 0.0, 16.0])
def test_group05_round_trip(cur):
    sim = leaf(current_a=cur, cell_spread_mv=35.0)
    st = sim.state()
    d = L.decode_group05(L.parse_isotp(sim.respond("2105", "79B", "7BB")))
    assert d["g05_cell_max_mv"] == st["cell_max"]
    assert d["g05_cell_min_mv"] == st["cell_min"]
    assert d["current_a"] == pytest.approx(st["current_a"], abs=0.001)   # s16 / 1024
    assert d["discharging"] is st["discharging"]
    assert d["g05_insulation_kohm"] == st["insulation_kohm"]
    assert d["segment_deltas"] == st["segment_deltas"]
    assert d["cell_groups"] == st["cell_groups"]


def test_group05_current_saturates_past_32_amps():
    """docs/SIGNALS.md gives group-05 current as s16 ÷ 1024 A, which cannot
    represent more than ±32 A — nowhere near a Leaf under acceleration. The
    encoder saturates rather than wrapping, and this test exists to record
    that the scale is unresolved above idle currents, not to bless it. The
    reader uses group-01 sensor 2 for the real number."""
    # -150 A extra on top of the READY base draw: a bit past -150 in total
    sim = leaf(current_a=-150.0)
    assert sim.state()["current_a"] < -150.0
    d = L.decode_group05(L.parse_isotp(sim.respond("2105", "79B", "7BB")))
    assert d["current_a"] == pytest.approx(-32.0, abs=0.01)
    assert d["discharging"] is True
    # the canonical sensor is unaffected
    assert L.decode_reading(lbc(sim))["current_a"] == pytest.approx(sim.state()["current_a"], abs=0.01)


def test_group06_round_trip():
    sim = leaf(charging=True, charge_kw=3.3, soc=92.0)
    st = sim.state()
    d = L.decode_group06(L.parse_isotp(sim.respond("2106", "79B", "7BB")))
    assert d["balancing"] == st["balancing"]
    assert d["balancing_active"] == sum(1 for f in st["balancing"] if f)
    assert d["balancing_active"] > 0, "a pack near the top of a charge should be balancing"


def test_decode_reading_over_the_whole_group_set():
    """The combined path the reader actually uses."""
    sim = leaf(soc=48.0, capacity_ah=22.0, pack_temp_c=19.0, current_a=-42.0)
    st = sim.state()
    rec = L.decode_reading(lbc(sim))
    assert rec["soc"] == pytest.approx(st["soc"], abs=0.01)
    assert rec["current_a"] == pytest.approx(st["current_a"], abs=0.001)
    assert rec["power_kw"] == pytest.approx(st["power_kw"], abs=0.01)
    assert rec["temps"] == st["temps_c"]
    assert rec["cells"] == st["cells"]
    assert rec["discharging"] is True


# ── HVAC amp ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fan,ac,setpoint,heat", [
    (0, False, 72.0, 0),
    (1, True, 60.0, 0),
    (4, True, 68.0, 0),
    (7, False, 90.0, 24),
])
def test_hvac_group10_round_trip(fan, ac, setpoint, heat):
    sim = leaf(hvac_on=fan > 0, hvac_ac_on=ac, hvac_fan_speed=fan,
               hvac_setpoint_f=setpoint, heater_level=heat,
               ambient_c=31.0, cabin_temp_c=24.0, evap_c=6.0, sunload=2)
    st = sim.state()
    d = L.decode_hvac({"2110": sim.respond("2110", "744", "764")})
    assert d["hvac_ambient_c"] == st["ambient_c"]         # exact: raw − 40
    assert d["cabin_temp_c"] == st["cabin_temp_c"]
    assert d["hvac_evap_c"] == st["evap_c"]
    assert d["hvac_sunload"] == st["sunload"]
    assert d["hvac_on"] is st["hvac_on"]
    assert d["hvac_ac_on"] is (st["hvac_on"] and st["hvac_ac_on"])
    assert d["hvac_blower_v"] == E.FAN_VOLTS[fan] if fan else d["hvac_blower_v"] == 0
    # byte 12 is ~11 counts per 5 °F, so the setpoint comes back within half a degree
    assert d["hvac_target_f"] == pytest.approx(st["hvac_setpoint_f"], abs=0.5)
    assert d["hvac_heater_level"] == st["heater_level"]
    assert d["hvac_compressor_rpm"] == st["compressor_rpm"]


def test_hvac_fan_speed_round_trips_where_the_car_can_tell_them_apart():
    """Speeds 6 and 7 both read 11 V on this car (docs/SIGNALS.md), so the
    decoder cannot separate them — encode 7, decode 6. That is the car's
    limitation, not a simulator bug, and the test says so."""
    for want in range(1, 6):
        sim = leaf(hvac_on=True, hvac_fan_speed=want)
        d = L.decode_hvac({"2110": sim.respond("2110", "744", "764")})
        assert d["hvac_fan_speed"] == want
    sim = leaf(hvac_on=True, hvac_fan_speed=7)
    d = L.decode_hvac({"2110": sim.respond("2110", "744", "764")})
    assert d["hvac_fan_speed"] == 6


# ── Car-CAN passive frames ───────────────────────────────────────────────

CARCAN_IDS = ("421", "358", "385", "5C5", "5A9", "355", "5B3", "284", "292", "60D", "180", "174")


@pytest.mark.parametrize("gear", ["P", "R", "N", "D", "Eco"])
def test_carcan_gear_round_trip(gear):
    sim = leaf(gear=gear)
    out = L.decode_carcan(carcan(sim, ["421"]))
    assert out["gear"] == gear


@pytest.mark.parametrize("turn", ["off", "left", "right", "hazards"])
def test_carcan_turn_signal_round_trip(turn):
    sim = leaf(turn_signal=turn)
    assert L.decode_carcan(carcan(sim, ["358"]))["turn_signal"] == turn


def test_carcan_body_round_trip():
    sim = leaf(doors="driver,hatch", locked=True, headlights=True, high_beam=True,
               parking_lights=True, fog_lights=True, start_state="acc")
    st = sim.state()
    out = L.decode_carcan(carcan(sim, ["60D"]))
    assert out["door_driver"] is True and out["door_hatch"] is True
    assert out["door_pass"] is False and out["door_rl"] is False and out["door_rr"] is False
    assert out["door_any"] is True
    assert out["locked"] is True
    assert out["headlights"] is True and out["high_beam"] is True
    assert out["parking_lights"] is True and out["fog_lights"] is True
    assert out["start_state_name"] == st["start_state"] == "acc"


def test_carcan_numbers_round_trip():
    sim = leaf(odometer_mi=123456, tpms_psi=32.5, speed_mph=45.0, brake_pct=40.0,
               accel_pedal_pct=30.0, handbrake=False, units_miles=True,
               soh=44.0, soc=70.0)
    st = sim.state()
    out = L.decode_carcan(carcan(sim, CARCAN_IDS))
    assert out["units_miles"] is True
    assert out["odometer_mi"] == st["odometer_mi"]                  # exact: u24
    assert out["tpms_psi"] == st["tpms_psi"]                        # exact: psi × 4
    assert out["handbrake"] is False
    assert out["speed_kmh"] == pytest.approx(st["speed_kmh"], abs=0.01)
    assert out["speed_mph"] == pytest.approx(st["speed_mph"], abs=0.1)
    assert out["brake_pct"] == pytest.approx(st["brake_pct"], abs=0.5)
    assert out["brake_on"] is True
    assert out["throttle_pct"] == pytest.approx(st["accel_pedal_pct"], abs=0.5)
    assert out["soh_dash_pct"] == round(st["soh"])
    assert out["range_km"] == pytest.approx(st["range_km"], abs=0.2)
    assert out["gear_174"] == "P/N"


def test_frames_look_like_a_capture_window():
    """A 0.2 s window on a 10 ms broadcast is a run of frames, not one."""
    sim = leaf()
    assert len(sim.frames("421", 0.2)) == 20
    assert len(sim.frames("385", 0.2)) == 1          # TPMS is a 1 Hz frame
    assert len(sim.frames("421", 10.0)) == E.MAX_FRAMES
    assert sim.frames("999", 0.2) == []              # not modelled: nothing, not junk


# ── Lancer ───────────────────────────────────────────────────────────────

def lancer_sim(**knobs):
    k = {"noise": 0.0}
    k.update(knobs)
    return make_sim(vehicle="lancer_2009", knobs=k, seed=1)


def lancer_decode(sim):
    import vehicles.lancer_2009 as V
    resp = {}
    for iid, it in V.ITEMS.items():
        tx, rx = V.TARGETS[it["kind"]]
        resp[iid] = sim.respond(it["cmd"], tx, rx)
    return V.decode(resp)


def test_lancer_mode01_round_trip():
    sim = lancer_sim(rpm=3250, speed_mph=62, coolant_temp_c=92, module_v=14.21,
                     load_pct=48.6, throttle_pct=22.7, maf_gs=17.35, map_kpa=63,
                     iat_c=31, ambient_c=19, fuel_pct=41.2, timing_deg=17.5,
                     baro_kpa=99, runtime_s=920, fuel_sys="open loop (warm-up)")
    st = sim.state()
    rec, alive = lancer_decode(sim)
    assert alive is True
    assert rec["rpm"] == pytest.approx(st["rpm"], abs=0.25)          # u16 ÷ 4
    assert rec["speed_kmh"] == st["speed_kmh"]                       # exact: km/h
    assert rec["coolant_temp_c"] == st["coolant_temp_c"]             # exact: raw − 40
    assert rec["module_v"] == pytest.approx(st["module_v"], abs=0.001)
    assert rec["engine_load_pct"] == pytest.approx(st["load_pct"], abs=0.4)   # raw ÷ 255
    assert rec["throttle_pct"] == pytest.approx(st["throttle_pct"], abs=0.4)
    assert rec["maf_gs"] == pytest.approx(st["maf_gs"], abs=0.01)
    assert rec["map_kpa"] == st["map_kpa"]
    assert rec["timing_deg"] == pytest.approx(st["timing_deg"], abs=0.25)
    assert rec["iat_c"] == st["iat_c"]
    assert rec["ambient_c"] == st["ambient_c"]
    assert rec["fuel_pct"] == pytest.approx(st["fuel_pct"], abs=0.4)
    assert rec["baro_kpa"] == st["baro_kpa"]
    assert rec["runtime_s"] == st["runtime_s"]
    assert rec["fuel_sys"] == st["fuel_sys"]


def test_lancer_dtc_round_trip_is_multi_frame():
    """12 stored codes is four ISO-TP frames — a real behaviour worth testing,
    and the reason lancer_2009.configure() sets ATFCSH/ATFCSD/ATFCSM1."""
    sim = lancer_sim(mil_on=True, dtc_stored="real", dtc_pending="real", dtc_trans="real")
    st = sim.state()
    stored = sim.respond("03", "7E0", "7E8")
    assert len(stored) == 4, "12 codes must not fit in one frame"
    assert stored[0].split()[1:4] == ["10", "1A", "43"]
    rec, _ = lancer_decode(sim)
    assert rec["dtc_stored"].split() == st["dtc_stored"]
    assert rec["dtc_pending"].split() == st["dtc_pending"]
    assert rec["dtc_trans"].split() == st["dtc_trans"]
    assert rec["mil_on"] is True
    assert rec["dtc_count"] == 12


def test_lancer_no_codes_reads_as_none_not_missing():
    sim = lancer_sim()
    rec, _ = lancer_decode(sim)
    assert rec["dtc_stored"] == "none"
    assert rec["mil_on"] is False
    assert rec["dtc_count"] == 0


def test_lancer_dtc_bytes_are_the_exact_inverse():
    from simulator.lancer import dtc_bytes
    assert dtc_bytes("P0171") == [0x01, 0x71]
    assert dtc_bytes("P2195") == [0x21, 0x95]
    assert dtc_bytes("U1234") == [0xD2, 0x34]
    with pytest.raises(ValueError):
        dtc_bytes("nonsense")


def test_lancer_supported_pid_bitmask_lists_what_it_answers():
    sim = lancer_sim()
    data = L.parse_isotp(sim.respond("0100", "7E0", "7E8"), "7E8")
    mask = int.from_bytes(bytes(data[:4]), "big")
    for pid in (0x05, 0x0C, 0x0D, 0x11, 0x1F):
        assert (mask >> (32 - pid)) & 1, f"PID {pid:02X} answers but is not advertised"
    assert not (mask >> (32 - 0x02)) & 1, "PID 02 is not implemented and must not be advertised"
    assert mask & 1, "the 0x20 block must be advertised as present"


# ── determinism ──────────────────────────────────────────────────────────

def test_same_seed_same_run():
    def run(seed):
        s = make_sim(vehicle="leaf_ze0", knobs={"soc": 61.0, "current_a": -30.0}, seed=seed)
        out = []
        for _ in range(20):
            s.step(0.5)
            out.append(s.respond("2101", "79B", "7BB") + s.respond("2102", "79B", "7BB"))
        return out

    assert run(3) == run(3)
    assert run(3) != run(4), "a different seed must produce a different pack"


def test_state_is_json_serialisable():
    st = make_sim(vehicle="leaf_ze0").state()
    assert json.loads(json.dumps(st))["simulated"] is True
    assert json.loads(json.dumps(lancer_sim().state()))["vehicle"] == "lancer_2009"


# ── knobs ────────────────────────────────────────────────────────────────

def test_knob_schema_is_complete_and_json_ready():
    for veh, kset in KNOBS.items():
        schema = make_sim(vehicle=veh).knob_schema()
        assert json.loads(json.dumps(schema))
        assert set(schema) == set(kset)
        for name, spec in schema.items():
            assert spec["type"] in ("float", "int", "bool", "text"), name
            assert spec["help"], f"{veh}.{name} has no help string"
            if spec["type"] in ("float", "int"):
                assert spec["min"] is not None and spec["max"] is not None, name


def test_contract_knob_coverage():
    """Every knob docs/SIMULATOR_CONTRACT.md asks for exists."""
    leaf_required = """soc capacity_ah soh hx pack_temp_c cell_spread_mv weak_cell_index
        weak_cell_offset_mv internal_resistance_ohm lv_volts insulation_kohm current_a
        speed_mph gear accel_pedal_pct brake_pct charging charge_kw hvac_on hvac_ac_on
        hvac_fan_speed hvac_setpoint_f cabin_temp_c ambient_c evap_c heater_level doors
        locked headlights high_beam parking_lights fog_lights turn_signal handbrake
        odometer_mi tpms_psi noise clock_scale
        fault.cell_degraded fault.insulation_low fault.lv_battery_weak
        fault.sensor_dropout fault.car_asleep fault.adapter_silent fault.bus_noise
        fault.ecu_nrc""".split()
    assert not [k for k in leaf_required if k not in KNOBS["leaf_ze0"]]
    lancer_required = ("rpm coolant_temp_c load_pct throttle_pct maf_gs map_kpa iat_c "
                       "fuel_pct module_v mil_on dtc_stored").split()
    assert not [k for k in lancer_required if k not in KNOBS["lancer_2009"]]


def test_unknown_knob_suggests_near_matches():
    sim = leaf()
    with pytest.raises(ValueError) as e:
        sim.set(soc_pct=50)
    msg = str(e.value)
    assert "soc_pct" in msg and "soc" in msg and "did you mean" in msg
    with pytest.raises(ValueError) as e:
        sim.set(**{"cell_degraded": True})
    assert "fault.cell_degraded" in str(e.value)
    with pytest.raises(ValueError) as e:
        sim.set(**{"zzzzzzzzz": 1})
    assert "zzzzzzzzz" in str(e.value) and "knob_schema" in str(e.value)


def test_out_of_range_is_clamped_and_recorded():
    sim = leaf()
    assert sim.set(soc=140)["soc"] == 100.0
    assert sim.set(soc=-5)["soc"] == 0.0
    assert any("clamped" in w for w in sim.warnings)


def test_bad_types_and_choices_are_rejected():
    sim = leaf()
    with pytest.raises(ValueError):
        sim.set(gear="Sport")
    with pytest.raises(ValueError):
        sim.set(soc="quite full")
    with pytest.raises(ValueError):
        sim.set(locked="maybe")
    assert sim.set(locked="off")["locked"] is False       # but on/off/1/0 are fine
    assert sim.set(gear="eco")["gear"] == "Eco"           # and choices are case-insensitive


def test_coupled_knobs_move_together():
    sim = leaf()
    assert sim.set(soh=50.0)["capacity_ah"] == pytest.approx(33.0, abs=0.01)
    assert sim.set(capacity_ah=16.5)["soh"] == pytest.approx(25.0, abs=0.01)
    applied = sim.set(doors="rl,rr")
    assert applied["door_rl"] is True and applied["door_driver"] is False
    assert sim.set(tpms_psi=28.0)["tpms_fr"] == 28.0


def test_every_knob_has_an_observable_effect():
    """No knob may be decorative: moving it must change either the state
    dictionary or the bytes on the wire."""
    ids = CARCAN_IDS
    # only observable across a step(); each has its own test below
    skip = {"clock_scale", "charge_heat_frac"}

    def snapshot(sim):
        return (json.dumps(sim.state(), sort_keys=True),
                json.dumps({c: sim.respond(c, "79B", "7BB") for c in LBC_CMDS}),
                json.dumps(sim.respond("2110", "744", "764")),
                json.dumps({i: sim.frames(i, 0.1) for i in ids}))

    for name, knob in KNOBS["leaf_ze0"].items():
        if name in skip:
            continue
        sim = leaf()
        before = snapshot(sim)
        if knob.type == "bool":
            alt = not knob.default
        elif knob.type == "text":
            alt = next(c for c in (knob.choices or ("all",)) if c != knob.default)
        else:
            span = (knob.max - knob.min) or 1
            alt = knob.min + span * 0.31
            if abs(alt - knob.default) < span * 0.05:
                alt = knob.min + span * 0.72
        sim.set(**{name: alt})
        if name in ("hvac_fan_speed", "hvac_ac_on", "heater_level"):
            sim.set(hvac_on=True)         # the amp reports nothing while it is off
            before = snapshot(leaf(hvac_on=True))
        if name == "ecu_nrc_code":        # only visible while fault.ecu_nrc is set
            sim.set(**{"fault.ecu_nrc": True})
            before = snapshot(leaf(**{"fault.ecu_nrc": True}))
        if name == "charger" or name.startswith("charge_"):
            # the charge curve only exists while charging; a warm pack so the
            # thermal derate knob has something to act on
            sim.set(charging=True, pack_temp_c=50.0)
            before = snapshot(leaf(charging=True, pack_temp_c=50.0))
        if name == "weak_cell_index":     # only visible once a pair is offset
            sim.set(weak_cell_offset_mv=-200)
            before = snapshot(leaf(weak_cell_offset_mv=-200))
        assert snapshot(sim) != before, f"knob {name!r} changed nothing"


def test_clock_scale_scales_simulated_time():
    fast = leaf(clock_scale=60.0)
    slow = leaf(clock_scale=1.0)
    fast.step(1.0)
    for _ in range(60):
        slow.step(1.0)
    assert fast.state()["t"] == pytest.approx(slow.state()["t"], abs=0.001)


def test_get_knobs_round_trips_through_set():
    sim = leaf(soc=33.0, gear="D")
    k = sim.get_knobs()
    other = leaf()
    other.set(**k)
    assert other.get_knobs() == k


# ── physics ──────────────────────────────────────────────────────────────

def test_soc_falls_under_load_and_rises_on_charge():
    sim = leaf(soc=50.0, capacity_ah=24.0, current_a=-24.0)   # 1 C
    for _ in range(360):
        sim.step(10.0)                                        # one hour
    # 24 A for an hour out of 24 Ah is the whole pack; SOC must have collapsed
    assert sim.state()["soc"] < 5.0
    # plug in — and take the extra 24 A off, or it keeps draining the charger
    sim.set(charging=True, charge_kw=6.6, current_a=0.0)
    before = sim.state()["soc"]
    for _ in range(60):
        sim.step(10.0)
    assert sim.state()["soc"] > before


def test_voltage_sags_under_load_and_recovers_at_rest():
    sim = leaf(soc=70.0, internal_resistance_ohm=0.1)
    rest = sim.state()["pack_v"]
    sim.set(current_a=-100.0)
    loaded = sim.state()["pack_v"]
    assert loaded < rest - 5.0, "100 A through 0.1 ohm should cost ~10 V"
    sim.set(current_a=-1.0)
    assert sim.state()["pack_v"] == pytest.approx(rest, abs=0.2)


def test_pack_warms_under_load_and_relaxes_to_ambient():
    sim = leaf(soc=80.0, pack_temp_c=20.0, ambient_c=20.0, current_a=-150.0,
               internal_resistance_ohm=0.1)
    for _ in range(600):
        sim.step(5.0)
    hot = sim.state()["pack_temp_c"]
    assert hot > 22.0, "I²R heating should show over an hour at 150 A"
    sim.set(current_a=0.0)
    for _ in range(3600):
        sim.step(5.0)
    assert sim.state()["pack_temp_c"] == pytest.approx(20.0, abs=0.5)


def test_ocv_curve_is_monotonic_and_in_range():
    from simulator.model import ocv
    prev = -1
    for pct in range(0, 101):
        v = ocv(pct)
        assert v > prev, "OCV must rise with SOC"
        prev = v
    assert ocv(100) * 96 < 400 and ocv(100) * 96 > 380
    assert ocv(2) < 3.5, "a nearly empty ZE0 cell pair sits below 3.5 V"


def test_odometer_integrates_from_speed():
    sim = leaf(odometer_mi=1000.0, speed_mph=60.0)
    for _ in range(3600):
        sim.step(1.0)
    assert sim.state()["odometer_mi"] == 1060


# ── faults ───────────────────────────────────────────────────────────────

def test_fault_cell_degraded_drops_one_pair_and_widens_the_spread():
    sim = leaf(cell_spread_mv=20.0, weak_cell_index=41)
    before = L.cell_stats(L.decode_group02(L.parse_isotp(sim.respond("2102", "79B", "7BB"))))
    sim.set(**{"fault.cell_degraded": True})
    after = L.cell_stats(L.decode_group02(L.parse_isotp(sim.respond("2102", "79B", "7BB"))))
    assert after["cell_min_idx"] == 41
    assert after["cell_min"] < before["cell_min"] - 300
    assert after["cell_spread"] > before["cell_spread"] + 300
    assert sim.faults() == {"fault.cell_degraded": True}


def test_fault_insulation_low_shows_in_groups_01_and_05():
    sim = leaf(**{"fault.insulation_low": True})
    rec = L.decode_reading(lbc(sim))
    assert rec["insulation_kohm"] < 50
    assert rec["g05_insulation_kohm"] == rec["insulation_kohm"]


def test_fault_lv_battery_weak_drops_the_12v_reading():
    sim = leaf()
    good = L.decode_reading(lbc(sim, ["2101"]))["lv_volts"]
    sim.set(**{"fault.lv_battery_weak": True})
    weak = L.decode_reading(lbc(sim, ["2101"]))["lv_volts"]
    assert good > 12.0 and weak < 11.5


def test_fault_sensor_dropout_corrupts_one_temp_and_one_current_sensor():
    sim = leaf(pack_temp_c=25.0, **{"fault.sensor_dropout": True})
    rec = L.decode_reading(lbc(sim, ["2101", "2104"]))
    assert -1 in rec["temps"], "sensor 3 should read the dropout value"
    assert rec["hv_current1_a"] > 1000, "sensor 1 should read nonsense"
    # the canonical current (sensor 2) is untouched, which is the point
    assert abs(rec["hv_current2_a"]) < 10


def test_fault_car_asleep_stops_the_battery_controller():
    sim = leaf(**{"fault.car_asleep": True})
    assert sim.respond("2101", "79B", "7BB") == ["NO DATA"]
    assert sim.respond("2110", "744", "764") == ["NO DATA"]
    assert sim.frames("60D", 0.2) == []
    assert L.decode_reading(lbc(sim)) == {}


def test_fault_adapter_silent_stops_everything():
    sim = leaf(**{"fault.adapter_silent": True})
    assert sim.respond("2101", "79B", "7BB") == []
    assert sim.frames("421", 0.2) == []
    assert L.is_no_data(sim.respond("2101", "79B", "7BB"))


def test_fault_ecu_nrc_returns_a_negative_response():
    sim = leaf(**{"fault.ecu_nrc": True})
    assert sim.respond("2101", "79B", "7BB") == ["7BB 03 7F 21 22"]
    sim.set(ecu_nrc_code=0x31)
    assert sim.respond("2101", "79B", "7BB") == ["7BB 03 7F 21 31"]
    assert L.decode_reading(lbc(sim, ["2101"])) == {}
    lan = lancer_sim(**{"fault.ecu_nrc": True})
    assert lan.respond("010C", "7E0", "7E8") == ["7E8 03 7F 01 22"]


def test_fault_bus_noise_puts_junk_in_the_capture_without_losing_the_signal():
    sim = leaf(gear="D", **{"fault.bus_noise": True})
    lines = sim.frames("421", 0.2)
    assert any("DATA ERROR" in l for l in lines)
    assert any(len(l.split()) < 4 for l in lines), "expected a truncated frame"
    # last_complete_frame() is why the decode still works through the noise
    assert L.decode_carcan({"421": lines})["gear"] == "D"


def test_faults_reports_only_what_is_active():
    sim = leaf()
    assert sim.faults() == {}
    sim.set(**{"fault.car_asleep": True, "fault.bus_noise": True})
    assert set(sim.faults()) == {"fault.car_asleep", "fault.bus_noise"}
    assert sim.state()["faults"] == ["fault.bus_noise", "fault.car_asleep"]


def test_every_fault_knob_has_a_symptom():
    """Each fault must change something a decoder can see."""
    base = leaf()
    clean = (json.dumps({c: base.respond(c, "79B", "7BB") for c in LBC_CMDS}),
             json.dumps(base.respond("2110", "744", "764")),
             json.dumps({i: base.frames(i, 0.1) for i in CARCAN_IDS}))
    faults = [n for n in KNOBS["leaf_ze0"] if n.startswith("fault.")]
    assert len(faults) == 8
    for f in faults:
        sim = leaf(**{f: True})
        now = (json.dumps({c: sim.respond(c, "79B", "7BB") for c in LBC_CMDS}),
               json.dumps(sim.respond("2110", "744", "764")),
               json.dumps({i: sim.frames(i, 0.1) for i in CARCAN_IDS}))
        assert now != clean, f"fault {f!r} produced no observable symptom"


# ── scenarios ────────────────────────────────────────────────────────────

def test_shipped_scenarios_all_load():
    names = scenario_names()
    for required in ("idle", "drive", "charge", "degraded_pack"):
        assert required in names
    for name in names:
        sim = make_sim(scenario=name)
        assert sim.scenario == name
        sim.step(1.0)
        assert sim.state()["simulated"] is True


def test_scenario_timeline_fires_at_the_right_simulated_time():
    sim = make_sim(scenario="drive")
    assert sim.get_knobs()["gear"] == "D"            # the t=0 entry fires on load
    assert sim.get_knobs()["speed_mph"] == 0.0
    for _ in range(4):
        sim.step(1.0)
    assert sim.get_knobs()["speed_mph"] == 0.0, "the t=5 entry must not fire early"
    sim.step(1.0)
    assert sim.get_knobs()["speed_mph"] == 20.0
    assert sim.get_knobs()["accel_pedal_pct"] == 25.0    # the pedal, not a current, drives the draw
    for _ in range(10):
        sim.step(1.0)
    assert sim.get_knobs()["speed_mph"] == 45.0


def test_scenario_timeline_fires_once_even_with_a_big_step():
    sim = make_sim(scenario="degraded_pack")
    assert sim.faults() == {}
    sim.step(100.0)                                  # jump past every entry
    assert set(sim.faults()) == {"fault.cell_degraded", "fault.insulation_low",
                                 "fault.lv_battery_weak"}
    sim.step(100.0)
    assert len(sim.faults()) == 3


def test_scenario_can_be_loaded_from_a_path(tmp_path):
    p = tmp_path / "custom.json"
    p.write_text(json.dumps({"name": "custom", "vehicle": "leaf_ze0",
                             "knobs": {"soc": 12.0},
                             "timeline": [{"t": 2, "set": {"gear": "R"}}]}))
    sim = make_sim()
    sim.load_scenario(str(p))
    assert sim.state()["soc"] == 12.0
    sim.step(3.0)
    assert sim.state()["gear"] == "R"


def test_unknown_scenario_names_the_shipped_ones():
    with pytest.raises(ValueError) as e:
        make_sim(scenario="drivve")
    assert "drive" in str(e.value)


def test_scenario_switches_the_vehicle_when_it_declares_one():
    sim = make_sim(scenario="lancer_dtc")
    assert sim.vehicle == "lancer_2009"
    assert sim.state()["dtc_count"] == 12
    assert any("switched the vehicle" in n for n in sim.notes)


def test_charge_scenario_charges():
    sim = make_sim(scenario="charge")
    start = sim.state()["soc"]
    assert sim.state()["current_a"] > 0
    # the scenario ships at 1x now (every shipped one does, bar degradation_arc),
    # so a simulated hour is an hour of dt rather than a minute of clock_scale
    sim.step(3600.0)
    assert sim.state()["soc"] > start + 5


# ── contact with reality ─────────────────────────────────────────────────

def test_matches_the_real_car():
    """Fed the owner's real 2026-08-24 numbers, the simulator's group 01
    decodes back into the same values the real capture does.

    This is a plausibility check on the model's calibration, not a
    verification of anything: both sides go through the same encoder.
    """
    real = L.decode_reading(fixture("lbc_raw_20260824.json")["groups"])
    sim = leaf(soc=real["soc"], capacity_ah=real["capacity_ah"], hx=real["hx"],
               lv_volts=real["lv_volts"], insulation_kohm=real["insulation_kohm"],
               pack_temp_c=34.0, cell_spread_mv=real["cell_spread"],
               start_state="ready")
    # The car was READY and drawing 1.5 A. The load table accounts for the
    # READY base (~0.4 A); `current_a` is the *extra* on top of the modelled
    # loads, so feed in the remainder rather than the whole reading.
    sim.set(current_a=real["hv_current2_a"] - sim.state()["current_a"])
    got = L.decode_reading(lbc(sim))

    assert got["soc"] == pytest.approx(real["soc"], abs=0.01)
    assert got["capacity_ah"] == pytest.approx(real["capacity_ah"], abs=0.001)
    assert got["soh"] == pytest.approx(real["soh"], abs=0.1)
    assert got["hx"] == pytest.approx(real["hx"], abs=0.01)
    assert got["lv_volts"] == pytest.approx(real["lv_volts"], abs=0.01)
    assert got["current_a"] == pytest.approx(real["current_a"], abs=0.01)
    # the OCV curve is what is really under test here: 76.87 % has to land on
    # the real 383.87 V without the pack voltage being fed in as a knob
    assert got["pack_v"] == pytest.approx(real["pack_v"], abs=0.5)
    assert got["cell_avg"] == pytest.approx(real["cell_avg"], abs=5)
    assert got["cell_spread"] == real["cell_spread"]
    assert got["temp_avg_c"] == pytest.approx(real["temp_avg_c"], abs=1.5)
    assert got["power_kw"] == pytest.approx(real["power_kw"], abs=0.02)


def test_scenario_files_are_well_formed():
    from simulator import SCENARIO_DIR
    for name in scenario_names():
        with open(os.path.join(SCENARIO_DIR, name + ".json")) as f:
            data = json.load(f)
        assert data["name"] == name
        assert data["vehicle"] in KNOBS
        for key in data.get("knobs", {}):
            assert key in KNOBS[data["vehicle"]], f"{name}: unknown knob {key}"
        for entry in data.get("timeline", []):
            assert isinstance(entry["t"], (int, float))
            for key in entry["set"]:
                assert key in KNOBS[data["vehicle"]], f"{name}: unknown knob {key}"


def test_the_simulator_never_pretends_to_be_a_car():
    """Every value it produces is labelled simulated, everywhere it surfaces."""
    sim = make_sim()
    assert sim.state()["simulated"] is True
    assert Simulator.simulated is True
    sim.set(soc=10.0)
    assert sim.state()["simulated"] is True
