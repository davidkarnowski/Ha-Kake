# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vehicle profile — 2012 Nissan Leaf (ZE0).

Everything Leaf-specific the reader needs: what to poll (ITEMS/TARGETS),
what the dashboard shows (TILES/SIGNALS), how to decode (leaf_decoders),
and the Leaf's current-sensor policy. docs/SIGNALS.md is the byte-level
authority behind every entry here.
"""

from elm327 import configure_leaf_bms
from leaf_decoders import decode_reading, decode_hvac, decode_carcan

NAME = "leaf_ze0"
TITLE = "2012 Nissan Leaf (ZE0)"
LOGO = "leaf"        # header wordmark art; profiles without one get a neutral dial

# kind -> UDS (tx, rx) headers, or None for passive monitor capture
TARGETS = {"lbc": ("79B", "7BB"), "hvac": ("744", "764"), "passive": None}
KIND_ORDER = ("lbc", "hvac", "passive")

# period: 0 = every cycle (fast lane); otherwise seconds between refreshes.
# est: seconds one poll costs over BLE (default 0.35 UDS / secs+0.25 passive).
ITEMS = {
    "lbc01":  {"kind": "lbc", "cmd": "2101", "period": 0,   "timeout": 10.0, "est": 0.45, "label": "battery state"},
    "lbc05":  {"kind": "lbc", "cmd": "2105", "period": 5,   "timeout": 10.0, "est": 0.6,  "label": "extended state"},
    "lbc04":  {"kind": "lbc", "cmd": "2104", "period": 15,  "timeout": 10.0, "label": "temperatures"},
    "lbc02":  {"kind": "lbc", "cmd": "2102", "period": 20,  "timeout": 15.0, "est": 1.3,  "label": "cell voltages"},
    "lbc06":  {"kind": "lbc", "cmd": "2106", "period": 30,  "timeout": 10.0, "label": "balancing"},
    "hvac10": {"kind": "hvac", "cmd": "2110", "period": 3,  "timeout": 4.0,  "label": "HVAC sensors"},
    "hvac11": {"kind": "hvac", "cmd": "2111", "period": 30, "timeout": 4.0,  "label": "HVAC group 11"},
    "hvac00": {"kind": "hvac", "cmd": "2100", "period": 30, "timeout": 4.0,  "label": "HVAC group 00"},
    "p421":   {"kind": "passive", "id": "421", "secs": 0.2, "period": 0,   "label": "gear"},
    "p358":   {"kind": "passive", "id": "358", "secs": 0.2, "period": 0,   "label": "turn signals"},
    "p284":   {"kind": "passive", "id": "284", "secs": 0.2, "period": 2,   "label": "speed"},
    "p60D":   {"kind": "passive", "id": "60D", "secs": 0.2, "period": 3,   "label": "doors / lights / locks"},
    "p5C5":   {"kind": "passive", "id": "5C5", "secs": 0.2, "period": 15,  "label": "odometer"},
    "p385":   {"kind": "passive", "id": "385", "secs": 0.3, "period": 20,  "label": "TPMS"},
    "p5B3":   {"kind": "passive", "id": "5B3", "secs": 0.8, "period": 60,  "label": "dash SOH"},
    "p5A9":   {"kind": "passive", "id": "5A9", "secs": 0.8, "period": 30,  "label": "range"},
    "p355":   {"kind": "passive", "id": "355", "secs": 0.2, "period": 300, "label": "units"},
    "p292":   {"kind": "passive", "id": "292", "secs": 0.2, "period": 1,   "label": "brake pedal"},
}

FAST_ONLY = {"lbc01"}

TILES = [
    {"id": "soc",         "name": "State of charge",        "items": ["lbc01"]},
    {"id": "health",      "name": "Battery health & state", "items": ["lbc01"]},
    {"id": "temps",       "name": "Pack temperature",       "items": ["lbc04"]},
    {"id": "vehicle",     "name": "Vehicle & shifter",      "items": ["p421", "p358", "p284", "p60D", "p5C5", "p5B3", "p5A9", "p355"]},
    {"id": "tires",       "name": "Tires (TPMS)",           "items": ["p385"]},
    {"id": "body",        "name": "Body (doors / lights)",  "items": ["p60D", "p358", "p421", "p292"]},
    {"id": "climate",     "name": "Climate (HVAC amp)",     "items": ["hvac10", "hvac11", "hvac00"]},
    {"id": "power",       "name": "Power monitor",          "items": ["lbc01", "lbc05"]},
    {"id": "history",     "name": "SOC history",            "items": ["lbc01"]},
    {"id": "degradation", "name": "Capacity degradation",   "items": ["lbc01"]},
    {"id": "cells",       "name": "Cell pairs",             "items": ["lbc02", "lbc06"]},
]
DEFAULT_SPAN = {"soc": 4, "health": 5, "temps": 3, "vehicle": 4, "tires": 4, "climate": 4,
                "body": 4, "power": 12, "history": 12, "degradation": 12, "cells": 12}
DEFAULT_TILES = [{"id": t["id"], "enabled": True, "span": DEFAULT_SPAN[t["id"]]} for t in TILES]

# keys produced by each item, dropped from the cache when the item is disabled
ITEM_KEYS = {
    "lbc02": ("cells", "cell_min", "cell_max", "cell_avg", "cell_spread", "cell_min_idx",
              "cell_max_idx", "cell_count", "pack_v_cells"),
    "lbc06": ("balancing", "balancing_active", "g06_raw"),
    "lbc04": ("temps", "temps_c", "temps_f", "temps_raw", "temp_avg_c", "temp_avg_f"),
    "p385": ("tpms_psi", "tpms_kpa"),
    "hvac10": ("cabin_temp_c", "cabin_temp_f", "hvac_ambient_c", "hvac_ambient_f",
               "hvac_evap_c", "hvac_evap_f", "hvac_sunload", "hvac_g10_raw",
               "hvac_fan_on", "hvac_blower_v", "hvac_fan_speed", "hvac_on", "hvac_ac_on",
               "hvac_compressor_rpm", "hvac_b23", "hvac_b24", "hvac_target_f", "hvac_target_c",
               "hvac_heater_level", "hvac_b29", "hvac_b31"),
}

# discrete states logged to the events table on change (clean signals — no flapping)
WATCH = ("hvac_ac_on", "hvac_on", "gear", "locked", "door_any", "handbrake", "high_beam", "fog_lights")


def _temp_avg(rec):
    """Pack mean: what the BMS reports, or the mean of the four sensors when a
    legacy record carries only the list."""
    v = rec.get("temp_avg_c")
    if v is not None:
        return v
    t = rec.get("temps") or []
    return round(sum(t) / len(t), 1) if t else None


# Record keys that get real, indexed columns in the store instead of riding in
# the `extra` JSON bag — the Leaf's battery/HVAC set, which is what the SOC,
# power, degradation and A/C-usage charts query. See the HISTORY_COLS contract
# in vehicles/__init__.py; web/store.py builds the schema from this.
HISTORY_COLS = {
    # ── LBC group 01 (battery state) ──
    "soc":              {"kind": "real", "hist": "soc",  "round": 2, "daily": {"min": "soc_min", "max": "soc_max"}},
    "pack_v":           {"kind": "real", "hist": "pack_v", "round": 1},
    "current_a":        {"kind": "real", "hist": "current_a", "round": 3},
    "power_kw":         {"kind": "real", "hist": "power_kw", "round": 3},
    "discharging":      {"kind": "bool", "hist": "discharging"},
    "capacity_ah":      {"kind": "real", "hist": "capacity_ah", "round": 3, "daily": {"avg": "capacity_ah"},
                         "daily_filter": True},
    "soh":              {"kind": "real", "hist": "soh", "round": 2, "daily": {"avg": "soh"}},
    "hx":               {"kind": "real", "hist": "hx",  "round": 2, "daily": {"avg": "hx"}},
    "lv_volts":         {"kind": "real", "hist": "lv_volts", "round": 2,
                         "daily": {"avg": "lv_volts", "min": "lv_min"}},
    "insulation_kohm":  {"kind": "real", "type": "INTEGER", "hist": "insulation_kohm", "round": 0,
                         "daily": {"avg": "insulation_kohm", "min": "insulation_min"}},
    # ── LBC group 04 (pack temperatures) ──
    "temp1_c":          {"kind": "real", "key": "temps.0"},
    "temp2_c":          {"kind": "real", "key": "temps.1"},
    "temp3_c":          {"kind": "real", "key": "temps.2"},
    "temp4_c":          {"kind": "real", "key": "temps.3"},
    "temp_avg_c":       {"kind": "real", "key": _temp_avg, "hist": "temp_avg", "round": 1,
                         "hist_f": "temp_avg_f", "daily": {"avg": "temp_avg_c"}},
    # ── LBC groups 02 / 06 (cell pairs, balancing) ──
    "cell_min":         {"kind": "int", "hist": "cell_min", "round": 0},
    "cell_max":         {"kind": "int", "hist": "cell_max", "round": 0},
    "cell_avg":         {"kind": "int"},
    "cell_spread":      {"kind": "int", "hist": "spread", "round": 0,
                         "daily": {"avg": "cell_spread", "max": "cell_spread_max"}},
    "cell_min_idx":     {"kind": "int"},
    "cell_max_idx":     {"kind": "int"},
    "balancing_active": {"kind": "int"},
    # ── HVAC amp + Car-CAN: cheap aggregate queries ("A/C on-time", "avg rpm") ──
    "hvac_ac_on":          {"kind": "bool", "index": "idx_readings_ac"},
    "hvac_compressor_rpm": {"kind": "int"},
    "hvac_on":             {"kind": "bool"},
    "hvac_fan_on":         {"kind": "bool"},
    "hvac_fan_speed":      {"kind": "int"},
    "hvac_heater_level":   {"kind": "int"},
    "cabin_temp_c":        {"kind": "real"},
    "hvac_ambient_c":      {"kind": "real"},
    "hvac_evap_c":         {"kind": "real"},
    "gear":                {"kind": "text"},
    "speed_mph":           {"kind": "real"},
}

# raw/bulk keys not worth keeping in the `extra` JSON bag (per-cell voltages
# have their own table; the temp lists are already in columns)
EXTRA_SKIP = ("temps", "temps_c", "temps_f", "temps_raw", "balancing", "readings")

SIGNALS = {
    # ── LBC group 01 ──
    "soc":              {"label": "State of charge", "unit": "%",   "min": 0,   "max": 100, "dec": 1, "item": "lbc01", "hist": "soc",          "color": "soc"},
    "pack_v":           {"label": "Pack voltage",    "unit": "V",   "min": 300, "max": 410, "dec": 1, "item": "lbc01", "hist": "pack_v",       "color": "good-high"},
    "current_a":        {"label": "Pack current",    "unit": "A",   "min": -150, "max": 150, "dec": 1, "item": "lbc01", "hist": "current_a",   "color": "diverge"},
    "power_kw":         {"label": "Power",           "unit": "kW",  "min": -10, "max": 10,  "dec": 2, "item": "lbc01", "hist": "power_kw",     "color": "diverge"},
    "capacity_ah":      {"label": "Capacity",        "unit": "Ah",  "min": 0,   "max": 66,  "dec": 2, "item": "lbc01", "hist": "capacity_ah",  "color": "good-high"},
    "soh":              {"label": "SOH",             "unit": "%",   "min": 0,   "max": 100, "dec": 1, "item": "lbc01", "hist": "soh",          "color": "good-high"},
    "hx":               {"label": "HX",              "unit": "",    "min": 0,   "max": 100, "dec": 2, "item": "lbc01", "hist": "hx",           "color": "good-high"},
    "lv_volts":         {"label": "12 V battery",    "unit": "V",   "min": 10,  "max": 15,  "dec": 2, "item": "lbc01", "hist": "lv_volts",     "color": "band"},
    "insulation_kohm":  {"label": "Insulation",      "unit": "kΩ",  "min": 0,   "max": 1000, "dec": 0, "item": "lbc01", "hist": "insulation_kohm", "color": "good-high"},
    "hv_current1_a":    {"label": "HV current 1",    "unit": "A",   "min": -150, "max": 150, "dec": 2, "item": "lbc01", "color": "diverge"},
    "hv_current2_a":    {"label": "HV current 2",    "unit": "A",   "min": -150, "max": 150, "dec": 2, "item": "lbc01", "color": "diverge"},
    # ── LBC group 04 (temps always °F with °C alt) ──
    "temp_avg_f":       {"label": "Pack temp (avg)", "unit": "°F",  "min": 20,  "max": 130, "dec": 1, "item": "lbc04", "hist": "temp_avg_f",   "color": "heat", "alt": "temp_avg_c", "alt_unit": "°C"},
    "temps_f.0":        {"label": "Pack temp 1",     "unit": "°F",  "min": 20,  "max": 130, "dec": 0, "item": "lbc04", "color": "heat", "alt": "temps_c.0", "alt_unit": "°C"},
    "temps_f.1":        {"label": "Pack temp 2",     "unit": "°F",  "min": 20,  "max": 130, "dec": 0, "item": "lbc04", "color": "heat", "alt": "temps_c.1", "alt_unit": "°C"},
    "temps_f.2":        {"label": "Pack temp 3",     "unit": "°F",  "min": 20,  "max": 130, "dec": 0, "item": "lbc04", "color": "heat", "alt": "temps_c.2", "alt_unit": "°C"},
    "temps_f.3":        {"label": "Pack temp 4",     "unit": "°F",  "min": 20,  "max": 130, "dec": 0, "item": "lbc04", "color": "heat", "alt": "temps_c.3", "alt_unit": "°C"},
    # ── LBC group 02 / 06 ──
    "cell_min":         {"label": "Lowest cell pair", "unit": "mV", "min": 3000, "max": 4200, "dec": 0, "item": "lbc02", "hist": "cell_min", "color": "good-high"},
    "cell_max":         {"label": "Highest cell pair", "unit": "mV", "min": 3000, "max": 4200, "dec": 0, "item": "lbc02", "hist": "cell_max", "color": "good-high"},
    "cell_avg":         {"label": "Average cell pair", "unit": "mV", "min": 3000, "max": 4200, "dec": 0, "item": "lbc02", "color": "good-high"},
    "cell_spread":      {"label": "Cell spread",     "unit": "mV",  "min": 0,   "max": 100, "dec": 0, "item": "lbc02", "hist": "spread",       "color": "good-low"},
    "balancing_active": {"label": "Pairs balancing", "unit": "",    "min": 0,   "max": 96,  "dec": 0, "item": "lbc06", "color": "mono"},
    # ── HVAC amp (tentative decode) ──
    "cabin_temp_f":     {"label": "Cabin temp",      "unit": "°F",  "min": 20,  "max": 130, "dec": 0, "item": "hvac10", "color": "heat", "alt": "cabin_temp_c", "alt_unit": "°C"},
    "hvac_ambient_f":   {"label": "Ambient temp",    "unit": "°F",  "min": -10, "max": 120, "dec": 0, "item": "hvac10", "color": "heat", "alt": "hvac_ambient_c", "alt_unit": "°C"},
    "hvac_evap_f":      {"label": "Evaporator temp", "unit": "°F",  "min": 20,  "max": 100, "dec": 0, "item": "hvac10", "color": "heat", "alt": "hvac_evap_c", "alt_unit": "°C"},
    "hvac_sunload":     {"label": "Sunload",         "unit": "",    "min": 0,   "max": 255, "dec": 0, "item": "hvac10", "color": "mono"},
    "hvac_fan_speed":   {"label": "Fan speed",       "unit": "/7",  "min": 0,   "max": 7,   "dec": 0, "item": "hvac10", "color": "mono"},
    "hvac_blower_v":    {"label": "Blower voltage",  "unit": "V",   "min": 0,   "max": 13,  "dec": 0, "item": "hvac10", "color": "mono"},
    "hvac_fan_on":      {"label": "Fan running",     "kind": "bool", "item": "hvac10"},
    "hvac_on":          {"label": "HVAC on",         "kind": "bool", "item": "hvac10"},
    "hvac_ac_on":       {"label": "A/C compressor",  "kind": "bool", "item": "hvac10"},
    "hvac_compressor_rpm": {"label": "Compressor speed", "unit": "rpm", "min": 0, "max": 6000, "dec": 0, "item": "hvac10", "color": "mono"},
    "hvac_target_f":    {"label": "Climate setpoint (≈)", "unit": "°F", "min": 60, "max": 90, "dec": 0, "item": "hvac10", "color": "heat", "alt": "hvac_target_c", "alt_unit": "°C"},
    "hvac_heater_level": {"label": "Heater demand",   "unit": "",    "min": 0,   "max": 60,  "dec": 0, "item": "hvac10", "color": "heat"},
    # ── Car-CAN passive ──
    "speed_mph":        {"label": "Speed",           "unit": "mph", "min": 0,   "max": 100, "dec": 0, "item": "p284", "color": "mono"},
    "odometer_mi":      {"label": "Odometer",        "unit": "mi",  "min": 0,   "max": 200000, "dec": 0, "item": "p5C5", "color": "mono"},
    "range_mi":         {"label": "Range (dash)",    "unit": "mi",  "min": 0,   "max": 80,  "dec": 0, "item": "p5A9", "color": "good-high"},
    "soh_dash_pct":     {"label": "SOH (dash byte)", "unit": "%",   "min": 0,   "max": 100, "dec": 0, "item": "p5B3", "color": "good-high"},
    "tpms_psi.0":       {"label": "Tire FL",         "unit": "psi", "min": 20,  "max": 50,  "dec": 1, "item": "p385", "color": "band"},
    "tpms_psi.1":       {"label": "Tire FR",         "unit": "psi", "min": 20,  "max": 50,  "dec": 1, "item": "p385", "color": "band"},
    "tpms_psi.2":       {"label": "Tire RR",         "unit": "psi", "min": 20,  "max": 50,  "dec": 1, "item": "p385", "color": "band"},
    "tpms_psi.3":       {"label": "Tire RL",         "unit": "psi", "min": 20,  "max": 50,  "dec": 1, "item": "p385", "color": "band"},
    "gear":             {"label": "Gear",            "kind": "text", "item": "p421"},
    "turn_signal":      {"label": "Turn signal",     "kind": "text", "item": "p358"},
    "start_state_name": {"label": "Start state",     "kind": "text", "item": "p60D"},
    "handbrake":        {"label": "Parking brake",   "kind": "bool", "item": "p5C5"},
    "headlights":       {"label": "Headlights",      "kind": "bool", "item": "p60D"},
    "locked":           {"label": "Locked",          "kind": "bool", "item": "p60D"},
    "door_driver":      {"label": "Driver door",     "kind": "bool", "item": "p60D"},
    "door_pass":        {"label": "Passenger door",  "kind": "bool", "item": "p60D"},
    "door_rl":          {"label": "Rear-L door",     "kind": "bool", "item": "p60D"},
    "door_rr":          {"label": "Rear-R door",     "kind": "bool", "item": "p60D"},
    "door_hatch":       {"label": "Hatch",           "kind": "bool", "item": "p60D"},
    "door_any":         {"label": "Any door open",   "kind": "bool", "item": "p60D"},
    "brake_on":         {"label": "Brake pedal",     "kind": "bool", "item": "p292"},
    "parking_lights":   {"label": "Parking lights",  "kind": "bool", "item": "p60D"},
    "high_beam":        {"label": "High beam",       "kind": "bool", "item": "p60D"},
    "fog_lights":       {"label": "Fog lights",      "kind": "bool", "item": "p60D"},
}


async def configure(elm):
    await configure_leaf_bms(elm)


def decode(responses):
    """{item_id: raw lines} -> (flat record, lbc_alive)."""
    lbc, hvac, caps = {}, {}, {}
    for iid, lines in responses.items():
        it = ITEMS.get(iid)
        if not it:
            continue
        if it["kind"] == "lbc":
            lbc[it["cmd"]] = lines
        elif it["kind"] == "hvac":
            hvac[it["cmd"]] = lines
        else:
            caps[it["id"]] = lines
    rec, alive = {}, None
    if lbc:
        r = decode_reading(lbc)
        alive = bool(r)
        rec.update(r)
    if hvac:
        rec.update(decode_hvac(hvac))
    if caps:
        rec.update(decode_carcan(caps))
    return rec, alive


def apply_policy(cache, calib, state):
    """Leaf current-sensor policy. Zero-offset calibration (web/calibration.json)
    is applied to the pack current. Direction comes from the BMS discharge flag
    (group 05, cached between polls); a positive reading while the BMS says
    discharging is sensor offset, not charging. Raw values are kept.

    Sensor fusion: group 05 is the LBC's processed current and is right at
    idle (≈ −0.9 A in READY); group-01 sensor 2 tracks load changes every
    cycle but reads ~0 in a dead zone near zero. Learn the difference each
    time a fresh group-05 sample arrives and carry it between samples.
    `state` holds the learned offset across cycles.

    The offset is only ever learned from samples where group 05 can be
    believed. Its field is a signed 16-bit count ÷ 1024, so it **wraps** at
    ±32.0 A (resolved on the 2026-09-03 drive; see docs/SIGNALS.md). Above the
    rail `g05 - s2` is not a sensor offset at all, it is the 64 A fold — and
    feeding that to the EMA poisoned the fused current for every later cycle:
    over that drive the fused value strayed from sensor 2 by a median 5.9 A
    and up to 41 A while moving, and the discharging clamp then zeroed many
    driving rows outright. So learning requires both reads inside the band and
    a difference small enough to actually be an offset; the two are polled
    ~0.5-1 s apart, so a large difference means a transient or a wrap either
    way. Outside those conditions the last good offset is kept, not updated.
    """
    # Well inside the ±32.0 A rail, so a sample near it cannot be a fold.
    LEARN_BAND_A = 30.0
    # The dead-zone bias this corrects is under an amp; anything larger is a
    # wrap or a transient, not an offset.
    LEARN_MAX_DELTA_A = 5.0
    c = cache
    cur = c.get("current_a")
    if cur is None:
        return
    c["current_raw_a"] = cur
    g05 = c.get("g05_current_a")
    s2 = c.get("hv_current2_a")
    if g05 is not None and s2 is not None and g05 != state.get("last_g05"):
        state["last_g05"] = g05
        d = g05 - s2
        trustworthy = (abs(g05) <= LEARN_BAND_A
                       and abs(s2) <= LEARN_BAND_A
                       and abs(d) <= LEARN_MAX_DELTA_A)
        if trustworthy:
            prev = state.get("s2_offset")
            state["s2_offset"] = d if prev is None else round(0.7 * prev + 0.3 * d, 3)
            state["s2_offset_stale"] = False
        else:
            # Keep the last good offset and say so, rather than silently
            # carrying a number learned from a wrapped sample.
            state["s2_offset_stale"] = True
    if s2 is not None and state.get("s2_offset") is not None and cur == s2:
        cur = round(s2 + state["s2_offset"], 3)
        c["current_fused"] = True
    c["s2_offset_a"] = state.get("s2_offset")
    c["s2_offset_stale"] = bool(state.get("s2_offset_stale"))
    off = float(calib.get("current_offset_a", 0.0) or 0.0)
    c["current_offset_a"] = off
    cur = round(cur - off, 3)
    if "discharging" not in c:
        c["discharging"] = cur < 0
    if c["discharging"] and cur > 0:
        cur = 0.0
    c["current_a"] = cur
    if c.get("pack_v"):
        c["power_kw"] = round(c["pack_v"] * cur / 1000.0, 3)
