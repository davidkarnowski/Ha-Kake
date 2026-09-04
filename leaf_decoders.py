#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nissan Leaf (ZE0, 2011-2012) LBC/BMS decoders — single source of truth.

All UDS requests go to 0x79B and are answered from 0x7BB. The ELM327 returns
multi-frame ISO-TP responses as lines like:

    7BB 10 29 61 01 FF FF F9 E9      (first frame: PCI 0x10, length 0x29)
    7BB 21 02 87 FF FF FC 44 FF      (consecutive frames: PCI 0x2N)

`parse_isotp()` strips the CAN ID, PCI bytes, and the 2-byte service echo
(61 xx) so every decode_groupNN() sees the bare payload with byte 0 being the
first data byte after "61 NN".

Conventions
-----------
* Temperatures are always returned as both Celsius and Fahrenheit.
* Current follows the BMS sign convention: NEGATIVE = discharging (energy
  leaving the pack), POSITIVE = charging / regen.  Power uses the same sign.
* Offsets below are 0-based into the stripped payload.

Verified offsets (2026-02 / 2026-08 sessions)
---------------------------------------------
Group 01 (39 B)  HV current 1 [0:4] s32/1024 A  | HV current 2 [6:10] s32/1024 A
                 pack V [18:20] u16/100         | 12 V [20:22] u16/1024
                 insulation [22:24] kOhm         | HX [26:28] u16/100
                 SOC [29:32] u24/10000 %         | capacity [33:36] u24/10000 Ah
Group 02 (192 B) 96 x u16 mV cell-pair voltages
Group 03 (32 B)  [10:12] cell max mV, [12:14] cell min mV (tentative)
Group 04 (18 B)  4 x (u16 raw, s8 degC); byte 12 = mean degC
Group 05 (74 B)  cell max [6:8] / min [8:10] mV | temp raws [10:18]
                 discharge flag [20:22] (FFFF) | current [22:24] s16/1024 A
                 insulation [24:26] kOhm | segment deltas [26:46] | group V [46:66]
Group 06 (25 B)  24 data bytes = 192 bits = 2 bits per cell pair (balancing, tentative)
"""

# The temperature helpers are vehicle-independent and now live in util.py;
# re-exported here because callers have imported them from this module since
# the project began.
from util import c_to_f, fmt_temp    # noqa: F401  (re-export)

NUM_CELLS = 96
NOMINAL_CAPACITY_AH = 66.0
DEFAULT_RX_ID = "7BB"


# ── helpers ──────────────────────────────────────────────────────────────


def _u16(d, i):
    return (d[i] << 8) | d[i + 1]


def _s16(d, i):
    v = _u16(d, i)
    return v - 0x10000 if v > 0x7FFF else v


def _u24(d, i):
    return (d[i] << 16) | (d[i + 1] << 8) | d[i + 2]


def _s32(d, i):
    v = (d[i] << 24) | (d[i + 1] << 16) | (d[i + 2] << 8) | d[i + 3]
    return v - 0x100000000 if v > 0x7FFFFFFF else v


def _s8(b):
    return b - 256 if b > 127 else b


def is_no_data(lines):
    """True if the ELM327 reply carries no CAN frames (NO DATA / error / empty)."""
    if not lines:
        return True
    joined = " ".join(lines).upper()
    return any(k in joined for k in ("NO DATA", "CAN ERROR", "BUS ERROR", "UNABLE TO CONNECT", "STOPPED", "?"))


# ── ISO-TP ───────────────────────────────────────────────────────────────

def parse_isotp(lines, rx_id=DEFAULT_RX_ID):
    """Reassemble a multi-frame ISO-TP response into the bare payload.

    Strips: CAN ID, PCI byte(s), and the 2-byte positive-response echo (61 NN)
    from the first frame. Frames from other CAN IDs are ignored.
    """
    data = bytearray()
    for line in lines:
        parts = line.strip().split()
        if len(parts) < 2 or parts[0].upper() != rx_id.upper():
            continue
        try:
            hx = [int(b, 16) for b in parts[1:]]
        except ValueError:
            continue
        pci = hx[0]
        if (pci & 0xF0) == 0x10:        # first frame: 10 LL 61 NN d0 d1 d2
            data.extend(hx[4:])
        elif (pci & 0xF0) == 0x20:      # consecutive frame: 2N d..
            data.extend(hx[1:])
        elif (pci & 0xF0) == 0x00:      # single frame: 0L 61 NN d..
            data.extend(hx[3:1 + pci])
    return data


# ── Group 01 — battery state ─────────────────────────────────────────────

def decode_group01(data):
    if len(data) < 39:
        return {}
    cap_ah = _u24(data, 33) / 10000.0
    return {
        "hv_current1_a": round(_s32(data, 0) / 1024.0, 3),
        "hv_current2_a": round(_s32(data, 6) / 1024.0, 3),
        "pack_v": round(_u16(data, 18) / 100.0, 2),
        "lv_volts": round(_u16(data, 20) / 1024.0, 3),
        "insulation_kohm": _u16(data, 22),
        "hx": round(_u16(data, 26) / 100.0, 2),
        "soc": round(_u24(data, 29) / 10000.0, 2),
        "capacity_ah": round(cap_ah, 4),
        "soh": round(cap_ah / NOMINAL_CAPACITY_AH * 100.0, 1),
        # unknown-but-static words kept for future decoding
        "g01_word4": _u16(data, 4),
        "g01_word24": _u16(data, 24),
    }


# ── Group 02 — cell pair voltages ────────────────────────────────────────

def decode_group02(data):
    """Return list of up to 96 cell-pair voltages in mV (stops at padding)."""
    volts = []
    for i in range(NUM_CELLS):
        off = i * 2
        if off + 1 >= len(data):
            break
        mv = _u16(data, off)
        if mv >= 5000:      # 0xFFFF padding
            break
        volts.append(mv)
    return volts


decode_cells = decode_group02  # backwards-compatible alias


def cell_stats(volts):
    if not volts:
        return {}
    mn, mx = min(volts), max(volts)
    return {
        "cell_count": len(volts),
        "cell_min": mn,
        "cell_max": mx,
        "cell_avg": round(sum(volts) / len(volts)),
        "cell_spread": mx - mn,
        "cell_min_idx": volts.index(mn),
        "cell_max_idx": volts.index(mx),
        "pack_v_cells": round(sum(volts) / 1000.0, 1),
    }


# ── Group 03 — tentative ─────────────────────────────────────────────────

def decode_group03(data):
    if len(data) < 14:
        return {}
    return {
        "g03_cell_max_mv": _u16(data, 10),
        "g03_cell_min_mv": _u16(data, 12),
        "g03_raw": data.hex(),
    }


# ── Group 04 — temperatures ──────────────────────────────────────────────

def decode_group04(data):
    """Return list of {raw, c, f} for the 4 pack sensors."""
    temps = []
    for i in range(4):
        off = i * 3
        if off + 2 >= len(data):
            break
        c = _s8(data[off + 2])
        temps.append({"raw": _u16(data, off), "c": c, "f": c_to_f(c)})
    return temps


# ── Group 05 — extended state / current ──────────────────────────────────

def decode_group05(data):
    if len(data) < 24:
        return {}
    discharging = _u16(data, 20) == 0xFFFF
    current_a = round(_s16(data, 22) / 1024.0, 3)
    out = {
        "g05_cell_max_mv": _u16(data, 6),
        "g05_cell_min_mv": _u16(data, 8),
        "current_a": current_a,
        "discharging": discharging,
    }
    if len(data) >= 26:
        out["g05_insulation_kohm"] = _u16(data, 24)
    if len(data) >= 66:
        out["segment_deltas"] = [v for v in (_u16(data, 26 + 2 * i) for i in range(10)) if v != 0xFFFF]
        out["cell_groups"] = [v for v in (_u16(data, 46 + 2 * i) for i in range(10)) if v < 5000]
    return out


# ── Group 06 — balancing flags (tentative) ───────────────────────────────

def decode_group06(data):
    if len(data) < 24:
        return {}
    bits = int.from_bytes(bytes(data[:24]), "big")
    flags = [(bits >> (190 - 2 * i)) & 0b11 for i in range(NUM_CELLS)]
    return {"balancing": flags, "balancing_active": sum(1 for f in flags if f), "g06_raw": data[:24].hex()}


# ── Combined reading ─────────────────────────────────────────────────────

GROUP_DECODERS = {
    "2101": decode_group01,
    "2102": None,   # handled specially (list + stats)
    "2103": decode_group03,
    "2104": None,
    "2105": decode_group05,
    "2106": decode_group06,
}


def decode_reading(responses, rx_id=DEFAULT_RX_ID):
    """Turn {cmd: [lines]} into one flat record.

    Adds derived fields: power_kw (signed, pack_v × current), temps_c, temps_f,
    temp_avg_c/f, cell list + stats. Missing/NO DATA groups are skipped.
    """
    rec = {}
    for cmd, lines in responses.items():
        if is_no_data(lines):
            continue
        data = parse_isotp(lines, rx_id)
        if cmd == "2101":
            rec.update(decode_group01(data))
        elif cmd == "2102":
            cells = decode_group02(data)
            if cells:
                rec["cells"] = cells
                rec.update(cell_stats(cells))
        elif cmd == "2103":
            rec.update(decode_group03(data))
        elif cmd == "2104":
            temps = decode_group04(data)
            if temps:
                rec["temps"] = [t["c"] for t in temps]          # legacy key (°C)
                rec["temps_c"] = rec["temps"]
                rec["temps_f"] = [t["f"] for t in temps]
                rec["temps_raw"] = [t["raw"] for t in temps]
                avg = sum(rec["temps"]) / len(rec["temps"])
                rec["temp_avg_c"] = round(avg, 1)
                rec["temp_avg_f"] = c_to_f(avg)
        elif cmd == "2105":
            rec.update(decode_group05(data))
        elif cmd == "2106":
            rec.update(decode_group06(data))

    # Canonical current: group-01 sensor 2 whenever group 01 was read (it is
    # polled every cycle), else group 05. Never alternate between the two —
    # they carry different zero offsets and the switch looks like a sawtooth.
    # The group-05 discharge flag is kept separately as the authority on
    # direction; the sign of a few-hundred-milliamp reading is not.
    if "current_a" in rec:
        rec["g05_current_a"] = rec["current_a"]
    if "hv_current2_a" in rec:
        rec["current_a"] = rec["hv_current2_a"]
    cur = rec.get("current_a")
    if "pack_v" not in rec and "pack_v_cells" in rec:
        rec["pack_v"] = rec["pack_v_cells"]
    if cur is not None and rec.get("pack_v"):
        rec["power_kw"] = round(rec["pack_v"] * cur / 1000.0, 3)
    return rec


# ═════════════════════════════════════════════════════════════════════════
# Car-CAN passive frames (captured with ATCAF0 — see elm327.passive_capture)
# Verified 2026-08-24 unless marked tentative.
# ═════════════════════════════════════════════════════════════════════════

# 0x421 byte 0 → gear. All five values confirmed live 2026-08-24 19:12-19:13
# (P→R→N→D→Eco→P): 08 / 10 / 18 / 20 / 38. Unlike 0x174 it separates P from N and D from Eco.
GEAR_421 = {0x08: "P", 0x10: "R", 0x18: "N", 0x20: "D", 0x38: "Eco"}


def _frame_bytes(line):
    """'385 00 00 95 8F 92 92 F0' → [0x00, 0x00, ...] (None if malformed)."""
    parts = line.strip().split()
    try:
        return [int(b, 16) for b in parts[1:]]
    except ValueError:
        return None


def last_complete_frame(lines, min_len):
    """Most recent frame with at least min_len data bytes (BLE can truncate lines)."""
    for line in reversed(lines):
        b = _frame_bytes(line)
        if b and len(b) >= min_len:
            return b
    return None


def decode_carcan(captures):
    """captures: {can_id: [lines]} → flat dict of vehicle signals."""
    out = {}

    b = last_complete_frame(captures.get("421", []), 1)
    if b and b[0] == 0x00:
        # a bare zero shows up occasionally (BLE line truncated mid-byte or a transitional frame);
        # it is never a gear — prefer the newest non-zero frame, else report nothing
        nz = [f for f in (_frame_bytes(l) for l in captures.get("421", [])) if f and f[0]]
        b = nz[-1] if nz else None
    if b:
        out["gear_code"] = b[0]
        out["gear"] = GEAR_421.get(b[0], f"?{b[0]:02X}")

    b = last_complete_frame(captures.get("355", []), 7)
    if b:
        out["units_miles"] = bool(b[6] & 0x20)

    b = last_complete_frame(captures.get("5C5", []), 4)
    if b:
        out["handbrake"] = bool(b[0] & 0x04)
        out["odometer_raw"] = (b[1] << 16) | (b[2] << 8) | b[3]
        miles = out.get("units_miles", True)
        out["odometer_mi"] = out["odometer_raw"] if miles else round(out["odometer_raw"] * 0.621371)
        out["odometer_km"] = round(out["odometer_raw"] * 1.609344) if miles else out["odometer_raw"]

    b = last_complete_frame(captures.get("385", []), 6)
    if b:
        psi = [b[i] / 4.0 for i in (2, 3, 4, 5)]
        out["tpms_psi"] = psi                       # FL, FR, RR, RL
        out["tpms_kpa"] = [round(p * 6.89476, 1) for p in psi]

    b = last_complete_frame(captures.get("5B3", []), 2)
    if b:
        out["soh_dash_pct"] = (b[1] >> 1) & 0x7F

    b = last_complete_frame(captures.get("284", []), 6)
    if b:
        raw = (b[4] << 8) | b[5]
        out["speed_raw"] = raw
        out["speed_kmh"] = round(raw / 100.0, 1)      # tentative scale
        out["speed_mph"] = round(raw / 100.0 * 0.621371, 1)

    b = last_complete_frame(captures.get("5A9", []), 3)
    if b:
        raw12 = ((b[1] << 8) | b[2]) >> 4
        out["range_raw"] = raw12
        out["range_km"] = raw12 / 5.0                  # tentative (OVMS)
        out["range_mi"] = round(raw12 / 5.0 * 0.621371, 1)

    b = last_complete_frame(captures.get("60D", []), 3)
    if b:
        # 0x60D verified 2026-08-25 (door + lock walks):
        #   byte 0 = per-door open flags   byte 2 = lock flags
        out["doors_raw"] = b[0]
        out["door_driver"] = bool(b[0] & 0x08)         # 08 driver, 10 pass, 20 rear-L, 40 rear-R, 80 hatch
        out["door_pass"] = bool(b[0] & 0x10)
        out["door_rl"] = bool(b[0] & 0x20)
        out["door_rr"] = bool(b[0] & 0x40)
        out["door_hatch"] = bool(b[0] & 0x80)
        out["door_any"] = bool(b[0] & 0xF8)
        # legacy grouped aliases (front = either front door, etc.)
        out["door_front"] = out["door_driver"] or out["door_pass"]
        out["door_rear"] = out["door_rl"] or out["door_rr"]
        out["door_trunk"] = out["door_hatch"]
        # byte 2 = 0x18 when locked, 0x00 unlocked (both lock presses, both ways)
        out["locked"] = bool(b[2] & 0x18)              # b2 0x18 locked, 0x00 unlocked (verified)
        out["driver_locked"] = out["locked"]           # legacy alias
        # exterior lights (verified 2026-08-25 lights walk):
        out["parking_lights"] = bool(b[0] & 0x04)      # position/park lamps
        out["headlights"] = bool(b[0] & 0x02)          # low beam
        out["high_beam"] = bool(b[1] & 0x08)           # b1 bit3
        out["fog_lights"] = bool(b[1] & 0x01)          # b1 bit0
        out["lights_on"] = bool(b[0] & 0x06) or out["fog_lights"]
        out["start_state"] = (b[1] >> 1) & 0x03        # byte 1 bits1-2: 0 off,1 acc,2 on,3 ready (0x06→ready)
        out["start_state_name"] = ["off", "acc", "on", "ready"][out["start_state"]]

    b = last_complete_frame(captures.get("180", []), 6)
    if b:
        out["throttle_pct"] = b[5] / 2.0

    b = last_complete_frame(captures.get("292", []), 7)
    if b:
        out["brake_raw"] = b[6]
        out["brake_pct"] = round(b[6] / 1.39, 1)
        out["brake_on"] = b[6] > 0                       # brake-light proxy (tentative)

    b = last_complete_frame(captures.get("358", []), 3)
    if b:
        out["turn_raw"] = b[2]
        out["turn_signal"] = {0x80: "off", 0x82: "left", 0x84: "right", 0x86: "hazards"}.get(b[2], f"?{b[2]:02X}")

    b = last_complete_frame(captures.get("174", []), 4)
    if b:
        out["gear_174"] = {0xAA: "P/N", 0x99: "R", 0xBB: "D/Eco"}.get(b[3], f"?{b[3]:02X}")

    return out


# ═════════════════════════════════════════════════════════════════════════
# HVAC amplifier (0x744 → 0x764), service 21 groups 01 / 10 / 11
# Group 10 (46 B) first bytes look like sensor temps in (raw − 40) °C:
#   b0 ambient, b1 in-car (cabin), b2 intake/evaporator, b3 sunload — TENTATIVE
#   (evening AC-on sample: 33 / 21 / 1 °C, sunload 2). Raw kept for calibration.
# ═════════════════════════════════════════════════════════════════════════

FAN_VOLTS = [(4, 1), (5, 2), (6, 3), (8, 4), (9, 5), (11, 6), (12, 7)]


def fan_level_from_volts(v):
    """Nearest fan setting (1-7) for a blower voltage; 6 vs 7 is tentative (11 vs ~12 V)."""
    if v <= 0:
        return 0
    return min(FAN_VOLTS, key=lambda t: abs(t[0] - v))[1]


def decode_hvac(responses, rx_id="764"):
    out = {}
    for cmd, lines in responses.items():
        if is_no_data(lines):
            continue
        d = parse_isotp(lines, rx_id)
        if not d or (len(d) == 1):          # negative response payloads are 1 byte
            continue
        key = cmd[-2:].upper()
        out[f"hvac_g{key}_raw"] = d.hex()
        if key == "10" and len(d) >= 4:
            amb = d[0] - 40
            cab = d[1] - 40
            evap = d[2] - 40
            out.update({
                "hvac_ambient_c": amb, "hvac_ambient_f": c_to_f(amb),
                "cabin_temp_c": cab, "cabin_temp_f": c_to_f(cab),
                "hvac_evap_c": evap, "hvac_evap_f": c_to_f(evap),
                "hvac_sunload": d[3],
                "hvac_decode": "tentative",
            })
            if len(d) >= 12:
                # byte 11 = 0x80 | blower volts (fan walk 2026-08-24: 84 85 86 88 89 8B 8B for speeds 1-7)
                b11 = d[11]
                volts = b11 & 0x7F
                out["hvac_fan_on"] = bool(b11 & 0x80) and volts > 0
                out["hvac_blower_v"] = volts
                out["hvac_fan_speed"] = fan_level_from_volts(volts) if out["hvac_fan_on"] else 0
                out["hvac_on"] = out["hvac_fan_on"]          # HVAC OFF button drops byte 11 to 0x00 (on/off walk)
                out["hvac_ac_on"] = bool(d[10] & 0x80)       # A/C walk: byte 10 0x00 ↔ 0x80, consistent both ways
            if len(d) >= 13:
                # byte 12 = air-mix target that tracks the setpoint: 111 … 173 for 60 … 90 °F
                # (setpoint walk 2026-08-24, ≈ 11 counts per 5 °F, 1–3 counts of lag on the way down)
                tf = 60.0 + (d[12] - 111) * 30.0 / 62.0
                out["hvac_target_f"] = round(min(95.0, max(55.0, tf)), 1)
                out["hvac_target_c"] = round((out["hvac_target_f"] - 32) * 5 / 9, 1)
            if len(d) >= 25:
                rpm = (d[21] << 8) | d[22]                   # 1976 / 1730 / 2425 / 1600 rpm seen with A/C on, 0 off
                out["hvac_compressor_rpm"] = rpm if rpm < 20000 else 0
                out["hvac_b23"] = d[23]                      # scale with compressor rpm — power or current, unresolved
                out["hvac_b24"] = d[24]
            if len(d) >= 37:
                out["hvac_heater_level"] = d[36]             # 0 at 60–65 °F, 3 → 40 as the PTC heater works (tentative)
                out["hvac_b29"] = d[29]                      # non-zero only while heating — current / kW candidates
                out["hvac_b31"] = d[31]
            if len(d) >= 27:
                out["hvac_w21"] = (d[21] << 8) | d[22]
                out["hvac_w23"] = (d[23] << 8) | d[24]
                out["hvac_w25"] = (d[25] << 8) | d[26]
    return out
