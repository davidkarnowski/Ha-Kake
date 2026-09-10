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
    # `signals`: the registry keys each tile actually shows — what its ⋯ menu
    # offers for audible alerts (vehicles/__init__.py validates them).
    {"id": "soc",         "name": "State of charge",        "items": ["lbc01"],
     "signals": ["soc", "pack_v"]},
    {"id": "health",      "name": "Battery health & state", "items": ["lbc01"],
     "signals": ["soh", "capacity_ah", "hx", "lv_volts", "insulation_kohm"]},
    {"id": "temps",       "name": "Pack temperature",       "items": ["lbc04"],
     "signals": ["temp_avg_f", "temps_f.0", "temps_f.1", "temps_f.2", "temps_f.3"]},
    {"id": "vehicle",     "name": "Vehicle & shifter",      "items": ["p421", "p358", "p284", "p60D", "p5C5", "p5B3", "p5A9", "p355"],
     "signals": ["speed_mph", "odometer_mi", "range_mi", "soh_dash_pct", "handbrake", "door_any", "headlights"]},
    {"id": "tires",       "name": "Tires (TPMS)",           "items": ["p385"],
     "signals": ["tpms_psi.0", "tpms_psi.1", "tpms_psi.2", "tpms_psi.3"]},
    {"id": "body",        "name": "Body (doors / lights)",  "items": ["p60D", "p358", "p421", "p292"],
     "signals": ["door_driver", "door_pass", "door_rl", "door_rr", "door_hatch", "door_any", "locked",
                 "headlights", "high_beam", "parking_lights", "fog_lights", "brake_on"]},
    {"id": "climate",     "name": "Climate (HVAC amp)",     "items": ["hvac10", "hvac11", "hvac00"],
     "signals": ["cabin_temp_f", "hvac_ambient_f", "hvac_evap_f", "hvac_sunload", "temp_avg_f", "hvac_target_f",
                 "hvac_ac_on", "hvac_compressor_rpm", "hvac_heater_level", "hvac_on", "hvac_fan_on",
                 "hvac_fan_speed", "hvac_blower_v"]},
    {"id": "power",       "name": "Power monitor",          "items": ["lbc01", "lbc05"],
     "signals": ["power_kw", "current_a", "pack_v", "power_adj_kw", "current_adj_a"]},
    {"id": "history",     "name": "SOC history",            "items": ["lbc01"],
     "signals": ["soc"]},
    {"id": "degradation", "name": "Capacity degradation",   "items": ["lbc01"],
     "signals": ["capacity_ah", "soh"]},
    {"id": "cells",       "name": "Cell pairs",             "items": ["lbc02", "lbc06"],
     "signals": ["cell_min", "cell_max", "cell_avg", "cell_spread"]},
    {"id": "pack3d",      "name": "Battery pack (3D)",      "items": ["lbc02", "lbc06", "lbc04"],
     "signals": ["cell_min", "cell_max", "cell_avg", "cell_spread", "temp_avg_f", "temps_f.0", "temps_f.1", "temps_f.2", "temps_f.3"]},
]
DEFAULT_SPAN = {"soc": 4, "health": 5, "temps": 3, "vehicle": 4, "tires": 4, "climate": 4,
                "body": 4, "power": 12, "history": 12, "degradation": 12, "cells": 12, "pack3d": 12}
# Tiles whose height cannot be measured from their markup (a WebGL canvas has no
# intrinsic height) ship a default in gridstack rows of 40 px; the rest auto-measure.
DEFAULT_H = {"pack3d": 12}
DEFAULT_TILES = [dict({"id": t["id"], "enabled": True, "span": DEFAULT_SPAN[t["id"]]},
                      **({"h": DEFAULT_H[t["id"]]} if t["id"] in DEFAULT_H else {}))
                 for t in TILES]

# keys produced by each item, dropped from the cache when the item is disabled
ITEM_KEYS = {
    "lbc02": ("cells", "cell_min", "cell_max", "cell_avg", "cell_spread", "cell_min_idx",
              "cell_max_idx", "cell_min_no", "cell_max_no", "cell_count", "pack_v_cells"),
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
    # `peak`: the reader keeps min / max / time-of-max between stored rows
    # (`<key>_min/_max/_tmax` in the row's extra) — a 5 s row still shows the
    # true peak of a pull, and the timeline strip can draw the envelope.
    "pack_v":           {"kind": "real", "hist": "pack_v", "round": 1, "peak": True},
    "current_a":        {"kind": "real", "hist": "current_a", "round": 3, "peak": True},
    "power_kw":         {"kind": "real", "hist": "power_kw", "round": 3, "peak": True},
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
    "cell_min":         {"kind": "int", "hist": "cell_min", "round": 0, "peak": True},
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

# ── Physical pack layout for the 3D tile ──────────────────────────────────
# Millimetres, car coordinates: x forward (+ toward the nose), y up, z toward
# the passenger side (the driver side is −z). Module 303 × 223 × 35 mm, 3.8 kg,
# four pouch cells as 2s2p, so two measured cell pairs per module.
#
# VERIFIED against the ZE0 service manual, page EVB-20 (November 2010 edition,
# April 2011 revision), as quoted by RegGuheert on mynissanleaf, 2013-04-29
# ("Which cell loses capacity fastest?"): modules MD1–MD24 stand in one stack
# under the rear seat with MD1 at the far passenger side and MD24 at the far
# driver side; MD25–MD28 sit under the rear driver's footwell, MD29–MD36 under
# the front driver's seat, MD37–MD44 under the front passenger's seat, MD45–MD48
# under the rear passenger's footwell; module n holds cells 2n−1 and 2n. In the
# dashboard's 0-based numbering module m (0..47) holds pairs 2m and 2m+1.
# The stack heights per group (two 2-high stacks in a footwell, two 4-high
# under a seat) come from a 2013 pack teardown (summet.com).
#
# Still ASSUMED (each entry says so in `verify`): the order of the two stacks
# inside a footwell or seat group, bottom → top within a stack, and which half
# of a module carries the odd pair. Correcting any of it is a data edit here.
# Indices are the dashboard's 0-based cell-pair numbers; LeafSpy shows +1.
PACK_MODULE = {"L": 303, "W": 223, "T": 35}
PACK_CASE = {"L": 1570, "W": 1188, "H": 190, "hump": {"L": 365, "W": 900, "H": 265}}
PACK_LAYOUT = [
    # kind "edge": modules on edge in one row across the car (stack axis = z);
    # kind "flat": modules lying flat, stacked upward (stack axis = y).
    # `first` is the index of the first cell pair; a stack holds n * 2 pairs.
    {"name": "Rear block (under rear seat)",    "kind": "edge", "x": -603, "z": 0,    "n": 24, "first": 0,
     "verify": ""},   # MD1 passenger end → MD24 driver end: service manual EVB-20
    # rear driver's footwell: MD25–MD28 (EVB-20); which of the two stacks is rearmost is assumed
    {"name": "Driver rear footwell, stack 1 (2-high)", "kind": "flat", "x": -268, "z": -300, "n": 2, "first": 48,
     "verify": "MD25–28 are here (EVB-20); order of the two stacks and bottom → top is assumed"},
    {"name": "Driver rear footwell, stack 2 (2-high)", "kind": "flat", "x": -15,  "z": -300, "n": 2, "first": 52,
     "verify": "stack order within the footwell assumed"},
    # under the front driver's seat: MD29–MD36 (EVB-20)
    {"name": "Under front driver seat, stack 1 (4-high)", "kind": "flat", "x": 238, "z": -300, "n": 4, "first": 56,
     "verify": "MD29–36 are here (EVB-20); order of the two stacks and bottom → top is assumed"},
    {"name": "Under front driver seat, stack 2 (4-high)", "kind": "flat", "x": 491, "z": -300, "n": 4, "first": 64,
     "verify": "stack order under the seat assumed"},
    # under the front passenger's seat: MD37–MD44 (EVB-20)
    {"name": "Under front passenger seat, stack 1 (4-high)", "kind": "flat", "x": 491, "z": 300, "n": 4, "first": 72,
     "verify": "MD37–44 are here (EVB-20); order of the two stacks and bottom → top is assumed"},
    {"name": "Under front passenger seat, stack 2 (4-high)", "kind": "flat", "x": 238, "z": 300, "n": 4, "first": 80,
     "verify": "stack order under the seat assumed"},
    # rear passenger's footwell: MD45–MD48 (EVB-20)
    {"name": "Passenger rear footwell, stack 1 (2-high)", "kind": "flat", "x": -15,  "z": 300, "n": 2, "first": 88,
     "verify": "MD45–48 are here (EVB-20); order of the two stacks and bottom → top is assumed"},
    {"name": "Passenger rear footwell, stack 2 (2-high)", "kind": "flat", "x": -268, "z": 300, "n": 2, "first": 92,
     "verify": "stack order within the footwell assumed"},
]
# The four 2011–2012 pack temperature sensors, at the locations LeafSpy's help
# table gives: 1 rear block centre-back, 2 right side under the front right
# seat, 3 left side under the rear left floor, 4 rear block right end.
# What the bodies show. One mode here: the 96 cell-pair voltages, two per module
# (`split: 2` is the layout default). A pack whose ECU reports temperatures per
# module would add a second mode with `key` naming that list (docs/PACK3D_GUIDE.md).
PACK_MODES = [
    {"id": "volt", "key": "cells", "name": "cell pair", "unit": "mV",
     "scales": ["abs", "dev", "drop"], "dev": 50, "drop": 300},
]
PACK_SENSORS = [
    {"n": "T1", "x": -770, "y": 131, "z": 0,    "where": "rear block, centre back — usually the hottest"},
    {"n": "T2", "x": 491,  "y": 172, "z": 431,  "where": "right side, under the front passenger seat"},
    {"n": "T3", "x": -268, "y": 102, "z": -431, "where": "left side, under the rear driver's footwell"},
    {"n": "T4", "x": -603, "y": 131, "z": 435,  "where": "rear block, passenger-side end"},
]

SIGNALS = {
    # ── LBC group 01 ──
    "soc":              {"label": "State of charge", "unit": "%",   "min": 0,   "max": 100, "dec": 1, "item": "lbc01", "hist": "soc",          "color": "soc"},
    "pack_v":           {"label": "Pack voltage",    "unit": "V",   "min": 300, "max": 410, "dec": 1, "item": "lbc01", "hist": "pack_v",       "color": "good-high"},
    "current_a":        {"label": "Pack current",    "unit": "A",   "min": -150, "max": 150, "dec": 1, "item": "lbc01", "hist": "current_a",   "color": "diverge"},
    "power_kw":         {"label": "Power",           "unit": "kW",  "min": -10, "max": 10,  "dec": 2, "item": "lbc01", "hist": "power_kw",     "color": "diverge"},
    # Derived by apply_policy (fusion offset, zero calibration, discharge clamp) — what the
    # power tile shows; the raw `current_a` / `power_kw` above are what the database keeps.
    "current_adj_a":    {"label": "Pack current (adjusted)", "unit": "A",  "min": -150, "max": 150, "dec": 1, "item": "lbc01", "color": "diverge"},
    "power_adj_kw":     {"label": "Power (adjusted)",        "unit": "kW", "min": -10,  "max": 10,  "dec": 2, "item": "lbc01", "color": "diverge"},
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
    # which pair, as people count them (1–96, the service manual's numbering); the record's
    # cell_min_idx / cell_max_idx are the 0-based positions in the `cells` list
    "cell_min_no":      {"label": "Lowest cell pair (No.)",  "unit": "", "min": 1, "max": 96, "dec": 0, "item": "lbc02", "color": "mono"},
    "cell_max_no":      {"label": "Highest cell pair (No.)", "unit": "", "min": 1, "max": 96, "dec": 0, "item": "lbc02", "color": "mono"},
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
    # Rule (2026-09-09, the owner's): the stored value is the value the car
    # reported. `current_a` and `power_kw` stay exactly as decode() left them;
    # everything this policy derives goes under its own key (`current_adj_a`,
    # `power_adj_kw`, `current_adj_src`). The dashboard shows the adjusted
    # value when it is there; the database keeps the raw one first-class.
    c["current_raw_a"] = cur          # alias of current_a, kept for the calibration endpoint
    src = []
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
        src.append("s2+g05_offset")
    else:
        c["current_fused"] = False
        src.append("s2" if cur == s2 else "g05")
    c["s2_offset_a"] = state.get("s2_offset")
    c["s2_offset_stale"] = bool(state.get("s2_offset_stale"))
    off = float(calib.get("current_offset_a", 0.0) or 0.0)
    c["current_offset_a"] = off
    if off:
        cur = round(cur - off, 3)
        src.append("zero_cal")
    if "discharging" not in c:
        c["discharging"] = cur < 0
    if c["discharging"] and cur > 0:
        cur = 0.0
        src.append("clamp")
    c["current_adj_a"] = cur
    c["current_adj_src"] = "+".join(src)
    if c.get("pack_v"):
        c["power_adj_kw"] = round(c["pack_v"] * cur / 1000.0, 3)
