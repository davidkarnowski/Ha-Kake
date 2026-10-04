# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The simulated CAN bus — the model's ECUs putting frames on a python-can bus.

    from simulator import make_sim
    from simulator.canbus import SimCanEcu

    sim = make_sim(vehicle="leaf_ze0", scenario="drive", seed=1)
    ecu = SimCanEcu(sim, bus="car", channel="hakake-car")     # ≈1,700 frames/s
    ecu.start()
    ...
    ecu.stop()

Why this exists: the native CAN transport (`cantransport.py`) receives every
frame on the bus all the time — about 1,700 a second on the Leaf's Car-CAN
and 790 on EV-CAN (research memo, from dalathegreat's `canmsgs.xlsx`). Until
the CANable arrives nothing in this project has seen that rate. `SimELM`
answers one `ATMA` with a handful of lines; this module answers the *bus*:
a thread driven by the running `Simulator` model broadcasts every id the
profile's passive items decode, encoded through `simulator/encode.py` (the
one encoder — nothing is duplicated here), at the period the survey gives
it, plus FILLER ids at their surveyed periods so the façade's intake, the
reader's cycle and the laptop's CPU are exercised at the real volume. A
second thread is the ISO-TP side: it answers the LBC's and the HVAC amp's
`21 NN` requests from `sim.respond()` — the same lines `SimELM` would print,
sent as frames — honouring the requester's flow control (BS, STmin) the way
`tests/test_cantransport.py`'s `FakeECU` does.

Read-only, at this layer too: the ECU implements service 0x21 only. Any
other service gets a UDS negative response `7F <svc> 11`
(serviceNotSupported), never an exception, never an invented answer.

**Everything about EV-CAN here is ASSERTED.** No adapter of this project has
ever been on pins 13/12. The seven EV-CAN layouts below are transcribed from
public documentation — dalathegreat's `leaf_can_bus_messages` DBC
(`EV-can_ZE0.dbc`, fetched 2026-09-09) and OVMS's `vehicle_nissanleaf.cpp` —
and each function cites what it took from where. The comparison the owner
will run when the board arrives (`tools/compare_sessions.py`) is exactly the
test of these bytes; until then they are the *expected* half, not evidence.

Rates come from the survey (`research/canable_adapter_recommendation_20260908.md`
§4.9, the "Time Between Msgs" column of `canmsgs.xlsx`); this car has
confirmed one rate itself (0x358 at ~10 Hz). Where the survey and
`encode.FRAME_PERIOD` disagree (0x421: 60 ms surveyed, 10 ms in the encoder,
which only sizes an ATMA line count) the survey wins here.

Nothing here names a vehicle outside the tables keyed by profile name — the
UDS pairs come from the profile's `TARGETS`, the passive ids from
`encode.frame_bytes()`; a profile without a table broadcasts nothing and
still answers UDS. The simulator is a fixture, not a verifier (CLAUDE.md §5).
"""

import threading
import time

import can

from . import encode
from .model import DRIVE_GEARS, KMH_PER_MPH, MOTOR_PEAK_KW, DRIVE_EFF, REGEN_BRAKE_KW

__all__ = ["SimCanEcu", "FrameSchedule", "CAR_PERIODS_MS", "EV_PERIODS_MS", "FILLER",
           "PERIODS", "frame_bytes", "ev_frame_bytes", "decode_1db", "decode_1da",
           "decode_1d4", "decode_55b", "decode_5bc", "decode_11a", "decode_1dc",
           "crc8_nissan", "frames_of_lines", "expected_fps", "CHANNELS"]

CHANNELS = {"car": "hakake-car", "ev": "hakake-ev"}
BUSES = ("car", "ev")

# ── the survey: period in ms per id, per bus, per vehicle ─────────────────
#
# memo §4.9 (canmsgs.xlsx "Time Between Msgs", parsed 2026-09-08). Ids the
# encoder models are MODELLED; the rest are FILLER — sent with a static,
# plausible payload (and a rolling counter nibble, which most of the
# Nissan frames carry) so the bus is as busy as the real one. `bus_load`
# scales the FILLER only; a modelled id is always sent at its period.

CAR_PERIODS_MS = {"leaf_ze0": {
    # 10 ms
    "002": 10, "130": 10, "174": 10, "176": 10, "180": 10, "1CA": 10, "1CB": 10,
    "1D5": 10, "1F9": 10, "2DE": 10,
    # 20 ms
    "215": 20, "216": 20, "245": 20, "260": 20, "280": 20, "284": 20, "285": 20,
    "292": 20, "300": 20,
    # 40–60 ms
    "354": 40, "355": 40, "421": 60,
    # ~100 ms
    "02A": 100, "351": 100, "358": 100, "35D": 100, "385": 100, "50A": 100, "50D": 100,
    "510": 100, "54A": 100, "54B": 100, "551": 100, "5C5": 100, "5E4": 100, "60D": 100,
    "625": 100, "6F6": 100,
    # ~500 ms
    "5A9": 500, "5B3": 500, "5C0": 500, "5E3": 500, "5EB": 500, "5FA": 500, "5FB": 500,
    "5FC": 500,
}}

EV_PERIODS_MS = {"leaf_ze0": {
    # 10 ms
    "11A": 10, "1D4": 10, "1DA": 10, "1DB": 10, "1DC": 10, "1F2": 10,
    # 20 ms
    "284": 20,
    # ~100 ms (50A/50B/50C are 103 ms in the sheet)
    "380": 100, "390": 100, "54A": 100, "54B": 100, "54C": 100, "54F": 100, "55A": 100,
    "55B": 100, "56E": 100, "5BF": 100, "50A": 103, "50B": 103, "50C": 103,
    # ~500 ms (5A9/5B9 are 512 ms in the sheet)
    "45E": 500, "481": 500, "5BC": 500, "5C0": 500, "59E": 500, "5A9": 512, "5B9": 512,
}}

PERIODS = {"car": CAR_PERIODS_MS, "ev": EV_PERIODS_MS}

# EV-CAN ids this module encodes from the model (ASSERTED layouts below).
EV_MODELLED = ("1DB", "1DA", "1D4", "55B", "5BC", "11A", "1DC")

# FILLER payloads with a known shape — from this car's own February 2026
# Car-CAN capture (docs/SIGNALS.md) where one exists, otherwise zeros. A frame absent
# from this table is sent as 8 zero bytes with the counter nibble.
FILLER_BYTES = {
    "245": [0x7F, 0xE8, 0x02, 0x18, 0x3A, 0x00, 0x7F, 0xE2],   # this car, Feb 2026: "no request"
    "1D5": [0x00, 0x00, 0x00, 0x03, 0xD9],                     # this car, Feb 2026: 5 bytes
    "176": [0x00] * 7,                                         # this car, Feb 2026: 7 bytes
    "50A": [0x00] * 6,                                         # 6 bytes on 2011-12 (memo §4)
}


def filler_ids(bus, vehicle):
    """The surveyed ids of `bus` that the model does not encode."""
    table = PERIODS[bus].get(vehicle, {})
    if bus == "car":
        modelled = {i for i in table if encode.frame_bytes(i, _PROBE_STATE) is not None}
    else:
        modelled = set(EV_MODELLED)
    return sorted(i for i in table if i not in modelled)


FILLER = {"car": None, "ev": None}       # filled lazily by filler_ids() (needs a state)


def expected_fps(bus, vehicle="leaf_ze0", bus_load=1.0):
    """Frames per second the rig aims for: modelled ids at their period plus
    the filler scaled by `bus_load`."""
    table = PERIODS[bus].get(vehicle, {})
    fill = set(filler_ids(bus, vehicle))
    total = 0.0
    for cid, ms in table.items():
        rate = 1000.0 / ms
        total += rate * (bus_load if cid in fill else 1.0)
    return round(total, 1)


# ── byte helpers ─────────────────────────────────────────────────────────

def crc8_nissan(data):
    """CRC-8, polynomial 0x85, init 0, no reflection, no final xor — the
    checksum the Leaf's VCM/LBC/inverter frames carry in their last byte
    (mynissanleaf "LEAF CANbus decoding" thread: "CRC poly 0x85" for 0x1D4;
    the ZE0 DBC names `CRC_1DB` / `CRC_1DA` / `CRC_1D4` / `CRC_55B` /
    `CRC_1DC` at 56|8). ASSERTED — nothing here has checked a real frame."""
    crc = 0
    for b in data:
        crc ^= b & 0xFF
        for _ in range(8):
            crc = ((crc << 1) ^ 0x85) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _s(v, bits):
    """Two's complement of `v` in `bits` bits."""
    v = int(round(v))
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return max(lo, min(hi, v)) & ((1 << bits) - 1)


def _u(v, bits):
    return max(0, min((1 << bits) - 1, int(round(v))))


def _signed(raw, bits):
    return raw - (1 << bits) if raw & (1 << (bits - 1)) else raw


def motor_rpm(mph):
    """Motor speed from road speed. ASSERTED: the ZE0's single-speed
    reduction is 7.94:1 and a 205/55R16 rolls ~0.316 m, so 100 km/h is
    ~6,660 rpm — about 107 rpm per mph. Sign follows direction (R negative)."""
    return mph * KMH_PER_MPH * 66.6


def motor_torque_nm(st):
    """Shaft torque from the model's motor / regen power and the rpm above.
    ASSERTED shape: P = T·ω, capped at the EM61's 280 Nm below base speed;
    regen is negative torque."""
    rpm = abs(motor_rpm(st["speed_mph"]))
    kw = st.get("motor_kw", 0.0) - st.get("regen_kw", 0.0)
    if rpm < 60.0:
        return 0.0 if kw <= 0 else 280.0 * min(1.0, kw / 10.0)
    t = kw * 1000.0 / (rpm * 2.0 * 3.141592653589793 / 60.0)
    return max(-280.0, min(280.0, t))


# ── EV-CAN encoders — every layout ASSERTED, source cited per function ─────

def enc_1db(st):
    """0x1DB HVBAT, 8 bytes, 10 ms — pack current and voltage from the LBC.

    DBC (EV-can_ZE0.dbc, dalathegreat): `LB_Current 7|11@0+ (0.5,0)
    [-400|200] "A"`, `LB_Total_Voltage 23|10@0+ (0.5,0) [0|450] "V"`,
    `LB_Relay_Cut_Request 11|2@1+`, `LB_Failsafe_Status 8|3@1+`,
    `LB_MainRelayOn_flag 29|1@1+`, `LB_Full_CHARGE_flag 28|1@1+`,
    `LB_INTER_LOCK 27|1@1+`, `LB_Discharge_Power_Status 25|2@1+`,
    `LB_PRUN_1DB 48|2@1+`, `CRC_1DB 56|8@1+`.
    OVMS vehicle_nissanleaf.cpp case 0x1db: current = `(d[0] << 3) |
    (d[1] & 0xe0) >> 5`, sign bit 0x0400, then *negated* and ÷2 (OVMS counts
    discharge positive; the DBC's [-400|200] range and this project's house
    rule both make discharge NEGATIVE, so raw = current_a × 2 as-is);
    voltage = `(d[2] << 2) | (d[3] & 0xc0) >> 6`, ÷2.
    """
    i_raw = _s(st["current_a"] * 2.0, 11)
    v_raw = _u(st["pack_v"] * 2.0, 10)
    d = [0] * 8
    d[0] = (i_raw >> 3) & 0xFF
    d[1] = ((i_raw & 0x07) << 5)
    d[2] = (v_raw >> 2) & 0xFF
    d[3] = ((v_raw & 0x03) << 6)
    relay_on = st["start_state"] == "ready" or st["charging"]
    if relay_on:
        d[3] |= 0x20                                  # LB_MainRelayOn_flag (bit 29)
    if st["soc"] >= 99.5:
        d[3] |= 0x10                                  # LB_Full_CHARGE_flag (bit 28)
    d[3] |= 0x02 if st["discharging"] else 0x00       # LB_Discharge_Power_Status (bits 25-26)
    d[6] = st.get("_prun", 0) & 0x03                  # LB_PRUN_1DB
    d[7] = crc8_nissan(d[:7])
    return d


def decode_1db(data):
    """The inverse of enc_1db (OVMS's expressions, house sign)."""
    d = list(data) + [0] * 8
    i_raw = _signed(((d[0] << 3) | ((d[1] & 0xE0) >> 5)) & 0x7FF, 11)
    v_raw = ((d[2] << 2) | ((d[3] & 0xC0) >> 6)) & 0x3FF
    return {"current_a": i_raw / 2.0, "pack_v": v_raw / 2.0,
            "main_relay_on": bool(d[3] & 0x20), "full_charge": bool(d[3] & 0x10),
            "discharging": bool(d[3] & 0x02)}


def enc_1da(st):
    """0x1DA INVmc, 8 bytes, 10 ms — the inverter's effective torque and rpm.

    DBC: `MG_EffectiveTorque 18|11@0+ (0.5,0) [-300|300] "Nm *0.5"`,
    `MG_OutputRevolution 39|15@0+ (1,0) [-16382|16382] "rpm"`, `MG_Clock
    48|2@1+`, `MG_ErrorCodes 50|6@1+`, `CRC_1DA 56|8@1+`. The ZE0 DBC
    declares both unsigned; OVMS case 0x1da treats them signed — torque
    `(d[2] & 0x07) << 8 | d[3]`, negative if `d[2] & 0x04`, ÷2 ("guess
    based on rpm"); rpm `d[4] << 8 | d[5]`, negative if `d[4] & 0x40`, ÷2 —
    and this encoder follows OVMS's sign handling (memo §4). 8dromeda's
    inverter notes put the DC bus voltage in bytes 0–1 at "2 increments per
    volt"; the DBC has no signal there, so it is filled from pack_v and
    labelled as 8dromeda's.
    """
    d = [0] * 8
    v_raw = _u(st["pack_v"] * 2.0, 16)                # 8dromeda: B0-1 volts, 2/V (ASSERTED)
    d[0], d[1] = (v_raw >> 8) & 0xFF, v_raw & 0xFF
    t_raw = _s(motor_torque_nm(st) * 2.0, 11)
    d[2] = (t_raw >> 8) & 0x07
    d[3] = t_raw & 0xFF
    rpm = motor_rpm(st["speed_mph"]) * (-1.0 if st["gear"] == "R" else 1.0)
    r_raw = _s(rpm * 2.0, 15)
    d[4] = (r_raw >> 8) & 0x7F
    d[5] = r_raw & 0xFF
    d[6] = st.get("_prun", 0) & 0x03                  # MG_Clock
    d[7] = crc8_nissan(d[:7])
    return d


def decode_1da(data):
    d = list(data) + [0] * 8
    t = _signed(((d[2] & 0x07) << 8) | d[3], 11)
    r = _signed(((d[4] & 0x7F) << 8) | d[5], 15)
    return {"torque_nm": t / 2.0, "rpm": r / 2.0, "inverter_v": ((d[0] << 8) | d[1]) / 2.0}


def enc_1d4(st):
    """0x1D4 VCM, 8 bytes, 10 ms — the torque the VCM is asking for.

    DBC: `MotorAmpTorqueRequest 23|12@0+ (0.25,0) [0|1024] "Nm"` (bytes 2
    and the high nibble of 3; the AZE0 DBC calls the same field
    `TargetMotorTorque 23|12@0-`, signed), `StatusOfHighVoltagePowerSupply
    34|1@1+`, `VCM_Clock 38|2@1+`, `Relay_Plus_Output_Status 46|1@1+`,
    `ChargeStatus 48|8@1+`, `CRC_1D4 56|8@1+`. 8dromeda: "signed 16-bit MSB
    first, 1 A per increment of 32, max 1120 = 560 A ≈ 250 Nm, lowest nibble
    always 00" — the same 12-bit field seen as a 16-bit word. OVMS case
    0x1d4 reads only `d[6] >> 7` (0 while charging = charge interrupted).
    Encoded signed, as the AZE0 DBC and 8dromeda have it (ASSERTED).
    """
    d = [0] * 8
    k = st
    driving = k["start_state"] == "ready" and k["gear"] in DRIVE_GEARS and not k["charging"]
    pedal = max(0.0, min(1.0, k["accel_pedal_pct"] / 100.0)) if driving else 0.0
    brake = max(0.0, min(1.0, k["brake_pct"] / 100.0)) if driving else 0.0
    # request = the pedal's share of the motor's torque, minus regen demand
    req_nm = pedal * 280.0 - brake * (REGEN_BRAKE_KW / (MOTOR_PEAK_KW / DRIVE_EFF)) * 280.0
    t_raw = _s(req_nm * 4.0, 12)
    d[2] = (t_raw >> 4) & 0xFF
    d[3] = (t_raw & 0x0F) << 4
    if k["start_state"] == "ready" or k["charging"]:
        d[4] |= 0x04                                  # StatusOfHighVoltagePowerSupply (bit 34)
        d[5] |= 0x40                                  # Relay_Plus_Output_Status (bit 46)
    d[4] |= (st.get("_prun", 0) & 0x03) << 6          # VCM_Clock (bits 38-39)
    d[6] = 0x80 if k["charging"] else 0x00            # ChargeStatus: bit 7 set = charge running
    d[7] = crc8_nissan(d[:7])
    return d


def decode_1d4(data):
    d = list(data) + [0] * 8
    raw = _signed(((d[2] << 4) | (d[3] >> 4)) & 0xFFF, 12)
    return {"torque_request_nm": raw / 4.0, "hv_on": bool(d[4] & 0x04),
            "charging": bool(d[6] & 0x80)}


def enc_55b(st):
    """0x55B HVBAT, 8 bytes, 100 ms — SOC at 0.1 %.

    DBC: `LB_SOC 7|10@0+ (1,0) [0|1000] "%+1"`, `LB_ALU_ANSWER 16|8@1+
    [85|170]`, `LB_IR_Sensor_Wave_Voltage 39|10@0+ "mV 5000/1024"`,
    `LB_IR_Sensor_Malfunction 40|1@1+`, `LB_PRUN_55B 48|2@1+`,
    `LB_RefusetoSleep 53|2@1+`, `LB_Capacity_Empty 55|1@1+`, `CRC_55B 56|8@1+`.
    OVMS case 0x55b: `soc = d[0] << 2 | d[1] >> 6`, ÷10, 0x3FF = invalid.
    """
    d = [0] * 8
    soc_raw = _u(st["soc"] * 10.0, 10)
    d[0] = (soc_raw >> 2) & 0xFF
    d[1] = (soc_raw & 0x03) << 6
    d[2] = 0x55 if (st.get("_prun", 0) & 1) == 0 else 0xAA   # LB_ALU_ANSWER alternates (ASSERTED)
    ir_raw = _u(min(4990.0, st["insulation_kohm"] * 4.0) * 1024.0 / 5000.0, 10)   # plausible wave mV
    d[4] = (ir_raw >> 2) & 0xFF
    d[5] = (ir_raw & 0x03) << 6
    if st["insulation_kohm"] < 100:
        d[5] |= 0x01                                  # LB_IR_Sensor_Malfunction (bit 40)
    d[6] = st.get("_prun", 0) & 0x03
    if st["soc"] <= 0.5:
        d[6] |= 0x80                                  # LB_Capacity_Empty (bit 55)
    d[7] = crc8_nissan(d[:7])
    return d


def decode_55b(data):
    d = list(data) + [0] * 8
    raw = ((d[0] << 2) | (d[1] >> 6)) & 0x3FF
    return {"soc": None if raw == 0x3FF else raw / 10.0, "ir_malfunction": bool(d[5] & 0x01)}


GIDS_WH = 80.0          # one gid ≈ 80 Wh (the community's figure; LeafSpy)
NOMINAL_PACK_V = 360.0


def enc_5bc(st):
    """0x5BC HVBAT, 8 bytes, 500 ms — gids, SOH, bars, temperature, minutes.

    DBC: `LB_Remain_Capacity 7|10@0+ "gids"`, `LB_New_Full_Capacity 13|10@0+
    (80,250) "wh"`, `LB_Remaining_Capacity_Segment m1 16|4@1+ "dash bars"`,
    `LB_Average_Battery_Temperature 24|8@1+ (1,-40) "degC"`,
    `LB_Capacity_Deterioration_Rate 33|7@1+ "%"`, `LB_Remaining_Capaci_Segment_Swit M
    32|1@1+`, `LB_Capacity_Bal_Complete_Flag 42|1@1+`, `LB_Output_Power_Limit_Reason
    45|3@1+`, `LB_Remain_charge_time_condition 41|5@0+`, `LB_Remain_charge_time
    52|13@0+ "minutes"` (8190 = none). OVMS case 0x5bc: gids `(d[0] << 2) |
    ((d[1] & 0xc0) >> 6)`, mux `(d[5] << 3 | d[6] >> 5) & 0x1f`, minutes
    `(d[6] << 8 | d[7]) & 0x1fff`. The "deterioration rate" byte is read by
    the community (LeafSpy, mynissanleaf) as the SOH percentage; encoded as
    SOH here and flagged ASSERTED.
    """
    d = [0] * 8
    wh_now = st["capacity_ah"] * st["pack_v"] * st["soc"] / 100.0
    gids = _u(wh_now / GIDS_WH, 10)
    d[0] = (gids >> 2) & 0xFF
    d[1] = (gids & 0x03) << 6
    full_raw = _u((st["capacity_ah"] * NOMINAL_PACK_V - 250.0) / 80.0, 10)
    d[1] |= (full_raw >> 4) & 0x3F                    # LB_New_Full_Capacity bits 13..4
    d[2] = (full_raw & 0x0F) << 4
    bars = _u(round(st["soc"] / 100.0 * 12.0), 4)
    d[2] |= bars                                      # segment mux m1: dash bars
    d[3] = _u(st["temp_avg_c"] + 40.0, 8)             # LB_Average_Battery_Temperature
    d[4] = 0x01                                       # mux switch = 1 (bars in byte 2)
    d[4] |= (_u(st["soh"], 7) << 1) & 0xFE            # SOH % (ASSERTED reading of the field)
    if st["charging"] and st["soc"] > 80:
        d[5] |= 0x04                                  # LB_Capacity_Bal_Complete_Flag
    limit_reason = 0
    if st.get("output_avail", 1.0) < 0.6:
        limit_reason = 1 if st["temp_avg_c"] > 45 else 2
    d[5] |= (limit_reason & 0x07) << 5
    minutes = 0x1FFF
    if st["charging"] and st.get("charge_power_kw", 0) > 0:
        minutes = _u((st["capacity_ah"] * st["pack_v"] * (100.0 - st["soc"]) / 100.0)
                     / (st["charge_power_kw"] * 1000.0) * 60.0, 13)
    d[6] |= (minutes >> 8) & 0x1F
    d[7] = minutes & 0xFF
    return d


def decode_5bc(data):
    d = list(data) + [0] * 8
    minutes = ((d[6] << 8) | d[7]) & 0x1FFF
    return {"gids": ((d[0] << 2) | ((d[1] & 0xC0) >> 6)) & 0x3FF,
            "soh_pct": (d[4] & 0xFE) >> 1,
            "temp_avg_c": d[3] - 40,
            "bars": d[2] & 0x0F if d[4] & 0x01 else None,
            "charge_minutes": None if minutes == 0x1FFF else minutes}


# JoystickGearPosition values. ASSERTED: the DBC declares the field
# (`4|4@1+`) with no value table in the file fetched; P=1 R=2 N=3 D=4 is the
# ordering community decoders use for the 2011-12 shift module.
GEAR_11A = {"P": 1, "R": 2, "N": 3, "D": 4, "Eco": 4}
GEAR_11A_NAMES = {1: "P", 2: "R", 3: "N", 4: "D"}
CAR_ON_11A = {"off": 0, "acc": 1, "on": 2, "ready": 3}      # ASSERTED enum, like 0x60D's


def enc_11a(st):
    """0x11A EShift, 8 bytes, 10 ms — the shift module (2011/2012 cars).

    DBC: `JoystickGearPosition 4|4@1+`, `ECOselected 12|1@1+`,
    `CarOnOffStatus 13|3@1+`, `SteeringWheelButton 16|8@1+`, `HeartbeatVCM
    24|8@1+ [85|170]`, `Mulitplexor M 48|8@1+`, `StartupDataUnknown0-3 m0-m3
    56|8@1+`. Not in OVMS's EV-CAN handler (it reads gear from 0x421 on
    Car-CAN); 8dromeda lists it at 10 ms. Enum values ASSERTED (above).
    """
    d = [0] * 8
    d[0] = (GEAR_11A.get(st["gear"], 0) & 0x0F) << 4
    if st["gear"] == "Eco":
        d[1] |= 0x10                                  # ECOselected (bit 12)
    d[1] |= (CAR_ON_11A.get(st["start_state"], 0) & 0x07) << 5   # CarOnOffStatus (bits 13-15)
    d[3] = 0x55 if (st.get("_prun", 0) & 1) == 0 else 0xAA        # HeartbeatVCM
    d[6] = st.get("_prun", 0) & 0x03                                # Mulitplexor
    return d


def decode_11a(data):
    d = list(data) + [0] * 8
    return {"gear": GEAR_11A_NAMES.get((d[0] >> 4) & 0x0F), "eco": bool(d[1] & 0x10),
            "car_on": (d[1] >> 5) & 0x07}


def enc_1dc(st):
    """0x1DC HVBAT, 8 bytes, 10 ms — the pack's power limits.

    DBC: `LB_Discharge_Power_Limit 7|10@0+ (0.25,0) "kW*0.25"`,
    `LB_Charge_Power_Limit 13|10@0+ (0.25,0)`, `LB_MAX_POWER_FOR_CHAGER
    19|10@0+ (0.1,-10) "kW"`, `LB_Charge_Power_Status 24|2@1+`,
    `LB_CODE_CONDITION 34|3@1+`, `LB_BPCMAX_UPRATE 37|3@1+`, `LB_PRUN_1DC
    49|2@0+`, `CRC_1DC 56|8@1+`. OVMS case 0x1dc: out `(d[0] << 2 | d[1] >> 6)
    / 4`, in `((d[1] & 0x3f) << 2 | d[2] >> 4) / 4`, charger max `((d[2] & 0x0f)
    << 6 | d[3] >> 2) / 10` (OVMS drops the DBC's −10 offset; encoded per the
    DBC here). The limits themselves come from the model's `output_avail`
    and regen fade — ASSERTED numbers on an ASSERTED layout.
    """
    d = [0] * 8
    avail = st.get("output_avail", 1.0)
    out_kw = MOTOR_PEAK_KW / DRIVE_EFF * avail                        # ≈94 kW when healthy
    soc_f = max(0.0, min(1.0, (95.0 - st["soc"]) / 5.0))
    in_kw = REGEN_BRAKE_KW * soc_f * (0.5 if st["temp_avg_c"] < 5 else 1.0)
    charger_kw = 44.0 * st.get("charge_derate", 1.0) if st["charging"] or st["plugged_in"] else 0.0
    o = _u(out_kw * 4.0, 10)
    i = _u(in_kw * 4.0, 10)
    c = _u((charger_kw + 10.0) * 10.0, 10)
    d[0] = (o >> 2) & 0xFF
    d[1] = ((o & 0x03) << 6) | ((i >> 4) & 0x3F)
    d[2] = ((i & 0x0F) << 4) | ((c >> 6) & 0x0F)
    d[3] = ((c & 0x3F) << 2) | (0x01 if st["charging"] else 0x00)   # LB_Charge_Power_Status
    d[6] = (st.get("_prun", 0) & 0x03) << 6                           # LB_PRUN_1DC (49|2@0+)
    d[7] = crc8_nissan(d[:7])
    return d


def decode_1dc(data):
    d = list(data) + [0] * 8
    return {"discharge_limit_kw": (((d[0] << 2) | (d[1] >> 6)) & 0x3FF) / 4.0,
            "charge_limit_kw": ((((d[1] & 0x3F) << 2) | (d[2] >> 4)) & 0x3FF) / 4.0,
            "charger_max_kw": ((((d[2] & 0x0F) << 6) | (d[3] >> 2)) & 0x3FF) / 10.0 - 10.0}


EV_ENCODERS = {"leaf_ze0": {"1DB": enc_1db, "1DA": enc_1da, "1D4": enc_1d4, "55B": enc_55b,
                            "5BC": enc_5bc, "11A": enc_11a, "1DC": enc_1dc}}
EV_DECODERS = {"1DB": decode_1db, "1DA": decode_1da, "1D4": decode_1d4, "55B": decode_55b,
               "5BC": decode_5bc, "11A": decode_11a, "1DC": decode_1dc}


def ev_frame_bytes(can_id, st, vehicle="leaf_ze0"):
    fn = EV_ENCODERS.get(vehicle, {}).get(can_id.upper())
    return fn(st) if fn else None


def frame_bytes(bus, can_id, st, vehicle="leaf_ze0", counter=0):
    """The data bytes of one frame on `bus`: the encoder's if the id is
    modelled, a FILLER payload otherwise. None if the id is unknown here."""
    cid = can_id.upper()
    st = dict(st, _prun=counter)
    if bus == "car":
        b = encode.frame_bytes(cid, st)
        if b is not None:
            return b[:encode.FRAME_DLC.get(cid, 8)]
    else:
        b = ev_frame_bytes(cid, st, vehicle)
        if b is not None:
            return b
    if cid not in PERIODS[bus].get(vehicle, {}):
        return None
    b = list(FILLER_BYTES.get(cid, [0] * 8))
    if len(b) == 8 and PERIODS[bus][vehicle][cid] <= 20:
        b[7] = (b[7] & 0xF0) | (counter & 0x0F)      # the rolling counter nibble
    return b


# a bare state, for deciding which Car-CAN ids the encoder models
_PROBE_STATE = {
    "gear": "P", "turn_signal": "off", "tpms_psi": [35.0] * 4, "odometer_mi": 0, "units_miles": True,
    "handbrake": True, "range_km": 0, "soh": 50, "speed_kmh": 0, "brake_pct": 0,
    "accel_pedal_pct": 0, "doors_open": [], "parking_lights": False, "headlights": False,
    "start_state": "ready", "high_beam": False, "fog_lights": False, "locked": True,
}


# ── the schedule: which ids are due, pure and deterministic ──────────────

class FrameSchedule:
    """Per-id next-due bookkeeping on any clock the caller supplies.

    `due(now)` returns the ids whose period has elapsed, in id order, and
    advances them by their period (if an id has fallen more than a second
    behind — the thread stalled — it resyncs to `now` rather than bursting a
    second's worth of frames). Filler ids run at `period / bus_load`; at
    bus_load 0 they never fire. The same object drives the live thread and
    the offline stream writer, so a recorded stream and a live bus have the
    same cadence.
    """

    def __init__(self, bus, vehicle="leaf_ze0", bus_load=1.0, t0=0.0):
        self.bus = bus
        self.vehicle = vehicle
        self.table = dict(PERIODS[bus].get(vehicle, {}))
        self.filler = set(filler_ids(bus, vehicle))
        self.bus_load = float(bus_load)
        self.next = {}
        self.reset(t0)

    def reset(self, t0):
        # stagger the first firings across one period so the bus does not
        # start with every id in the same millisecond
        self.next = {}
        for n, cid in enumerate(sorted(self.table)):
            per = self.period(cid)
            self.next[cid] = t0 + (per * (n % 7) / 7.0 if per else 0.0)

    def set_load(self, bus_load):
        self.bus_load = max(0.0, min(1.0, float(bus_load)))

    def period(self, cid):
        """Seconds between frames of `cid`; None for a filler id at load 0."""
        ms = self.table[cid]
        if cid in self.filler:
            if self.bus_load <= 0.0:
                return None
            return ms / 1000.0 / self.bus_load
        return ms / 1000.0

    def due(self, now):
        out = []
        for cid in sorted(self.table):
            per = self.period(cid)
            if per is None:
                self.next[cid] = now + 1.0
                continue
            if self.next[cid] <= now:
                out.append(cid)
                self.next[cid] += per
                if self.next[cid] < now - 1.0:
                    self.next[cid] = now + per
        return out

    def next_due(self):
        return min(self.next.values()) if self.next else None


# ── frames from ELM lines ────────────────────────────────────────────────

def frames_of_lines(lines):
    """`["7BB 10 29 61 01 …", …]` → `[(0x7BB, bytes), …]`."""
    out = []
    for line in lines or []:
        p = line.split()
        if len(p) < 2:
            continue
        try:
            out.append((int(p[0], 16), bytes(int(b, 16) for b in p[1:])))
        except ValueError:
            continue
    return out


def _msg(cid, data, err=False):
    return can.Message(arbitration_id=cid, data=bytes(data), is_extended_id=False,
                       is_error_frame=err)


# ── the ECU ──────────────────────────────────────────────────────────────

NRC_SERVICE_NOT_SUPPORTED = 0x11
READ_SERVICE = 0x21


class SimCanEcu:
    """The model's ECUs on one python-can channel: a broadcaster thread and
    an ISO-TP responder thread. Start with `start()`, stop with `stop()`.

    `sim` is a Simulator (or anything with `state()`, `respond()`, `step()`,
    `get_knobs()`, `vehicle`); `lock` guards it against the control API
    thread. `clock=True` makes the broadcaster step the model in real time
    (there is no SimELM.advance() behind the CAN façade); set it False when
    another thread owns the clock, or for a frozen-state test.
    `bus_factory()` returns the python-can Bus to use (default: the virtual
    interface on `channel`); tests pass their own.
    """

    def __init__(self, sim, bus="car", channel=None, lock=None, bus_load=None, uds=None,
                 clock=True, bus_factory=None, targets=None, log=None):
        if bus not in BUSES:
            raise ValueError(f"bus must be one of {BUSES}, not {bus!r}")
        self.sim = sim
        self.bus = bus
        self.channel = channel or CHANNELS[bus]
        self.lock = lock if lock is not None else threading.RLock()
        self.vehicle = getattr(sim, "vehicle", "leaf_ze0")
        self._load_override = bus_load
        self.uds = (bus == "car") if uds is None else bool(uds)
        self.clock = bool(clock)
        self.bus_factory = bus_factory or (lambda: can.Bus(interface="virtual", channel=self.channel,
                                                            receive_own_messages=False))
        self.log = log or (lambda *a: None)
        self.schedule = FrameSchedule(bus, self.vehicle, bus_load=self.bus_load())
        # UDS pairs from the profile: {tx_int: (tx_hex, rx_hex)}
        self.targets = dict(targets) if targets is not None else self._profile_targets()
        self.halt = threading.Event()
        self._tx_bus = None
        self._rx_bus = None
        self._threads = []
        self.sent = 0
        self.sent_by_id = {}
        self.requests = []             # (tx_hex, cmd) every request heard
        self.refused = []              # (tx_hex, service) answered 7F xx 11
        self.fc = []                   # (BS, STmin) of every flow control honoured
        self.cpu_s = 0.0               # thread CPU seconds spent broadcasting (for the bench)
        self.counter = 0
        self.stepped_s = 0.0

    # ── configuration ────────────────────────────────────────────────────

    def _profile_targets(self):
        try:
            from vehicles import get_vehicle
            prof = get_vehicle(self.vehicle)
        except Exception:
            return {}
        out = {}
        for pair in (getattr(prof, "TARGETS", {}) or {}).values():
            if pair:
                tx, rx = pair
                out[int(tx, 16)] = (tx.upper(), rx.upper())
        return out

    def bus_load(self):
        """The `bus_load` knob if the model has one (0–1), else 1.0; an
        explicit constructor value wins."""
        if self._load_override is not None:
            return max(0.0, min(1.0, float(self._load_override)))
        try:
            with self.lock:
                v = self.sim.get_knobs().get("bus_load", 1.0)
            return max(0.0, min(1.0, float(v)))
        except Exception:
            return 1.0

    def set_bus_load(self, v):
        self._load_override = v
        self.schedule.set_load(self.bus_load())

    def expected_fps(self):
        return expected_fps(self.bus, self.vehicle, self.bus_load())

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self):
        self._tx_bus = self.bus_factory()
        t = threading.Thread(target=self._broadcast, name=f"sim-ecu-{self.bus}", daemon=True)
        self._threads = [t]
        if self.uds and self.targets:
            self._rx_bus = self.bus_factory()
            r = threading.Thread(target=self._respond, name=f"sim-uds-{self.bus}", daemon=True)
            self._threads.append(r)
        for th in self._threads:
            th.start()
        return self

    def stop(self):
        self.halt.set()
        for th in self._threads:
            th.join(2.0)
        for b in (self._tx_bus, self._rx_bus):
            try:
                if b is not None:
                    b.shutdown()
            except Exception:
                pass
        self._tx_bus = self._rx_bus = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    # ── the broadcaster ──────────────────────────────────────────────────

    def _quiet(self, k):
        """A sleeping car broadcasts nothing; so does the rig when the
        `adapter_silent` fault is set (there is no adapter here — the bus
        itself goes dark, which is the nearest honest reading of it)."""
        return bool(k.get("fault.car_asleep") or k.get("fault.adapter_silent"))

    def _broadcast(self):
        now = time.monotonic()
        self.schedule.reset(now)
        last_step = now
        last_state = 0.0
        st = None
        knobs = {}
        noisy = False
        cpu0 = time.thread_time()
        while not self.halt.is_set():
            now = time.monotonic()
            # the clock: step the model by real time (the sim applies its own scale)
            if self.clock and now - last_step >= 0.02:
                with self.lock:
                    self.sim.step(now - last_step)
                self.stepped_s += now - last_step
                last_step = now
            # a fresh state at most every 10 ms; the knobs with it
            if st is None or now - last_state >= 0.01:
                with self.lock:
                    st = self.sim.state()
                    knobs = self.sim.get_knobs()
                last_state = now
                load = self.bus_load()
                if load != self.schedule.bus_load:
                    self.schedule.set_load(load)
                noisy = bool(knobs.get("fault.bus_noise"))
            due = self.schedule.due(now)
            if due and not self._quiet(knobs):
                self.counter = (self.counter + 1) & 0x0F
                for cid in due:
                    data = frame_bytes(self.bus, cid, st, self.vehicle, self.counter)
                    if data is None:
                        continue
                    try:
                        self._tx_bus.send(_msg(int(cid, 16), data))
                    except Exception as e:       # a closed bus on shutdown
                        self.log(f"  [sim-can] send failed: {e}")
                        return
                    self.sent += 1
                    self.sent_by_id[cid] = self.sent_by_id.get(cid, 0) + 1
                    if noisy and self.sent % 97 == 0:
                        self._tx_bus.send(_msg(int(cid, 16), b"", err=True))
            self.cpu_s = time.thread_time() - cpu0
            nxt = self.schedule.next_due()
            wait = 0.005 if nxt is None else max(0.0005, min(0.01, nxt - time.monotonic()))
            time.sleep(wait)

    # ── the ISO-TP responder ─────────────────────────────────────────────

    def _respond(self):
        bus = self._rx_bus
        while not self.halt.is_set():
            m = bus.recv(0.02)
            if m is None or m.is_error_frame or m.is_extended_id:
                continue
            pair = self.targets.get(m.arbitration_id)
            if pair is None:
                continue
            d = bytes(m.data)
            if not d or (d[0] & 0xF0) != 0x00 or d[0] < 1 or d[0] > 7:
                continue                                    # not a single-frame request
            req = d[1:1 + d[0]]
            tx_hex, rx_hex = pair
            self.requests.append((tx_hex, req.hex().upper()))
            self._answer(bus, m.arbitration_id, int(rx_hex, 16), tx_hex, rx_hex, req)

    def _answer(self, bus, tx_id, rx_id, tx_hex, rx_hex, req):
        svc = req[0]
        if svc != READ_SERVICE:
            # read-only: this ECU knows one service. A negative response, the
            # way a real ECU refuses, never an exception and never data.
            self.refused.append((tx_hex, svc))
            bus.send(_msg(rx_id, [0x03, 0x7F, svc, NRC_SERVICE_NOT_SUPPORTED, 0, 0, 0, 0]))
            return
        with self.lock:
            k = self.sim.get_knobs()
            if self._quiet(k):
                return                                      # asleep: silence, as on the car
            lines = self.sim.respond(req.hex().upper(), tx_hex, rx_hex)
        lines = list(lines or [])
        if not lines or lines == ["NO DATA"]:
            return                                          # the model has no answer: silence
        frames = frames_of_lines(lines)
        if not frames:
            return
        cid, data = frames[0]
        bus.send(_msg(cid, self._pad(data)))
        if (data[0] & 0xF0) != 0x10:
            return                                          # single frame: done
        bs, stmin = self._wait_fc(bus, tx_id)
        if bs is None:
            return                                          # no flow control: abandon, as an ECU would
        self.fc.append((bs, stmin))
        gap = _stmin_seconds(stmin)
        in_block = 0
        for cid, data in frames[1:]:
            if self.halt.is_set():
                return
            if gap:
                time.sleep(gap)
            bus.send(_msg(cid, self._pad(data)))
            in_block += 1
            if bs and in_block >= bs:
                bs2, stmin2 = self._wait_fc(bus, tx_id)
                if bs2 is None:
                    return
                self.fc.append((bs2, stmin2))
                bs, gap, in_block = bs2, _stmin_seconds(stmin2), 0

    @staticmethod
    def _pad(data):
        """The LBC's frames are 8 bytes on the wire (encode.isotp pads with
        FF); a short single frame is left as the encoder made it."""
        return bytes(data)

    def _wait_fc(self, bus, tx_id, timeout=1.0):
        """Wait for the requester's flow control on `tx_id`. Returns (BS,
        STmin) for ContinueToSend, keeps waiting on Wait (0x31), None on
        Overflow (0x32) or timeout."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout and not self.halt.is_set():
            f = bus.recv(0.02)
            if f is None or f.arbitration_id != tx_id or not f.data:
                continue
            pci = f.data[0]
            if (pci & 0xF0) != 0x30:
                continue
            fs = pci & 0x0F
            if fs == 0:
                return (f.data[1] if len(f.data) > 1 else 0, f.data[2] if len(f.data) > 2 else 0)
            if fs == 1:
                t0 = time.monotonic()                       # Wait: the requester asked for more time
                continue
            return (None, None)                             # Overflow / abort
        return (None, None)


def _stmin_seconds(stmin):
    """ISO 15765-2 STmin: 0x00-0x7F milliseconds, 0xF1-0xF9 100-900 µs,
    anything else reserved (treated as the 0x7F maximum)."""
    if stmin <= 0x7F:
        return stmin / 1000.0
    if 0xF1 <= stmin <= 0xF9:
        return (stmin - 0xF0) / 10000.0
    return 0.127
